"""Accounts and invite-only sign-up, plus the one-time move from the old single-user database to this schema.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib only - no extra dependency for a handful of accounts).
"""
import hashlib
import hmac
import os
import re
import secrets
import time

from .db import LOCAL_USER_ID

MIN_PASSWORD_LENGTH = 8
INVITE_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_MAX_USERS = 10          # matches Strava's self-serve "10 athletes" API app capacity - see README

USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,20}$")

PBKDF2_ITERATIONS = 200_000


def max_users():
    try:
        return max(1, int(os.environ.get("MAX_USERS", DEFAULT_MAX_USERS)))
    except ValueError:
        return DEFAULT_MAX_USERS


def valid_username(name):
    return bool(USERNAME_RE.match(name or ""))


# ---- passwords --------------------------------------------------------------------------------

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, salt.hex(), digest.hex())


def verify_password(password, stored):
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    return hmac.compare_digest(candidate, expected)


# ---- accounts -----------------------------------------------------------------------------------

def get_by_username(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", ((username or "").strip(),)).fetchone()


def get_by_id(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_accounts(conn):
    return conn.execute("SELECT username, is_admin, created_at FROM users ORDER BY created_at").fetchall()


def create(conn, username, password, is_admin=False):
    conn.execute("INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (username.strip().lower(), hash_password(password), int(is_admin), time.time()))
    return conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()["id"]


# ---- invites ------------------------------------------------------------------------------------

def create_invite(conn, created_by_user_id):
    token = secrets.token_urlsafe(20)
    now = time.time()
    conn.execute("INSERT INTO invites (token, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, created_by_user_id, now, now + INVITE_TTL_SECONDS))
    return token


def get_invite(conn, token):
    return conn.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()


def pending_invites(conn):
    return conn.execute("SELECT token, created_at, expires_at FROM invites WHERE used_by IS NULL AND expires_at > ? "
                        "ORDER BY created_at DESC", (time.time(),)).fetchall()


class InviteError(ValueError):
    """A user-facing reason an invite couldn't be redeemed."""


def redeem_invite(conn, token, username, password):
    """Validate and consume the invite, creating the account. Everything runs on the caller's connection/transaction,
    so a failure here (or afterwards) leaves neither a used invite nor a half-made account. Returns the new user id."""
    row = get_invite(conn, token)
    if row is None:
        raise InviteError("This invite link isn't valid.")
    if row["used_by"] is not None:
        raise InviteError("This invite link has already been used.")
    if row["expires_at"] < time.time():
        raise InviteError("This invite link has expired - ask for a new one.")
    username = (username or "").strip().lower()
    if not valid_username(username):
        raise InviteError("Usernames are 3-20 characters: lowercase letters, numbers, - or _.")
    if get_by_username(conn, username):
        raise InviteError("That username is taken.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise InviteError("Password must be at least %d characters." % MIN_PASSWORD_LENGTH)
    if count(conn) >= max_users():
        raise InviteError("This app is full (max %d accounts) - ask the admin to raise the limit." % max_users())
    new_id = create(conn, username, password, is_admin=False)
    conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE token = ?", (new_id, time.time(), token))
    return new_id


# ---- one-time migration from the pre-multi-user schema -------------------------------------------

def bootstrap_and_migrate(conn):
    """Runs on every startup (inside init_db, same transaction as the schema creation).

    Two independent steps:
    1. If this is a pre-multi-user database (it still has the old 'auth' table), give activities/plan/meta the
       user_id column every query now expects - regardless of whether an account is created this run. Without
       this the app can't run at all against old data, not even in local/no-login mode. Existing rows are
       tagged LOCAL_USER_ID (the same id local/no-login use already writes under), so they're immediately
       usable, invite APP_PASSWORD or not.
    2. If APP_PASSWORD is set and there are no accounts yet, create the first (admin) account from it, and - if
       this was a legacy database - move that LOCAL_USER_ID data (Strava tokens included) onto the new account.

    Safe to interrupt and retry: every step here is guarded (an "already done?" check, or a WHERE clause that
    only matches what's left to do), so a crash partway through - however far it got - always converges to the
    fully correct state on the next startup, never a duplicate account or lost/miscounted data. This is not
    strict transactional atomicity: SQLite's ALTER TABLE (via Python's sqlite3 module) commits itself
    immediately regardless of the surrounding transaction, so the schema change can survive even when a later
    step in the same run fails and its own INSERT/UPDATE statements roll back - which is exactly why every step
    is written to be idempotent rather than relying on all-or-nothing rollback."""
    is_legacy = bool(_table_columns(conn, "auth"))
    if is_legacy:
        _add_user_id_columns(conn, LOCAL_USER_ID)

    if count(conn) > 0:
        return
    password = os.environ.get("APP_PASSWORD", "").strip()
    if not password:
        return   # nothing to bootstrap - the app runs with zero accounts (login off) until one is created
    if len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError("APP_PASSWORD is too short: use at least %d characters." % MIN_PASSWORD_LENGTH)
    username = (os.environ.get("ADMIN_USERNAME", "").strip().lower() or "pete")
    if not valid_username(username):
        username = "admin"
    admin_id = create(conn, username, password, is_admin=True)
    if is_legacy:
        _reassign_legacy_data(conn, admin_id)


def _table_columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _add_user_id_columns(conn, default_user_id):
    """Give every pre-multi-user table its user_id column, tagging existing rows with `default_user_id`.
    Idempotent, and safe whether or not an account ends up being bootstrapped this run."""
    for table in ("activities", "plan"):
        if "user_id" not in _table_columns(conn, table):
            conn.execute("ALTER TABLE %s ADD COLUMN user_id INTEGER" % table)
        conn.execute("UPDATE %s SET user_id = ? WHERE user_id IS NULL" % table, (default_user_id,))
    if "user_id" not in _table_columns(conn, "meta"):   # old meta had PRIMARY KEY(key) only; needs a new shape
        conn.execute("ALTER TABLE meta RENAME TO meta_old")
        conn.execute("CREATE TABLE meta (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, "
                     "PRIMARY KEY (user_id, key))")
        conn.execute("INSERT INTO meta (user_id, key, value) SELECT ?, key, value FROM meta_old", (default_user_id,))
        conn.execute("DROP TABLE meta_old")


def _reassign_legacy_data(conn, admin_id):
    """Move data that _add_user_id_columns just tagged LOCAL_USER_ID onto the freshly created admin account,
    and migrate the old single-row Strava-token table ('auth') into strava_auth."""
    old_auth = conn.execute("SELECT * FROM auth WHERE id = 1").fetchone()
    if old_auth:
        conn.execute("""INSERT INTO strava_auth (user_id, athlete_id, athlete_name, access_token,
                         refresh_token, expires_at, scope) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (admin_id, old_auth["athlete_id"], old_auth["athlete_name"], old_auth["access_token"],
                     old_auth["refresh_token"], old_auth["expires_at"], old_auth["scope"]))
    conn.execute("DROP TABLE auth")
    for table in ("activities", "plan", "meta"):
        conn.execute("UPDATE %s SET user_id = ? WHERE user_id = ?" % table, (admin_id, LOCAL_USER_ID))
