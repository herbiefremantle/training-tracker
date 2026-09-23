"""Accounts and invite-only sign-up, plus the one-time move from the old single-user database to this schema.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib only - no extra dependency for a handful of accounts).
"""
import hashlib
import hmac
import os
import re
import secrets
import time

from .db import ACTIVITIES_TABLE_SQL, LOCAL_USER_ID

MIN_PASSWORD_LENGTH = 8
INVITE_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_MAX_USERS = 10          # matches Strava's self-serve "10 athletes" API app capacity - see README

USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")   # a sanity check, not full RFC 5322 - good enough for a signup form

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


def get_by_email(conn, email):
    return conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", ((email or "").strip(),)).fetchone()


def list_accounts(conn):
    return conn.execute("SELECT username, first_name, last_name, email, is_admin, created_at, last_login_at "
                        "FROM users ORDER BY created_at").fetchall()


def create(conn, username, password, is_admin=False, first_name=None, last_name=None, email=None):
    now = time.time()
    # account creation logs you straight in (see app/auth.py), so "last login" starts out matching "signed up"
    conn.execute("INSERT INTO users (username, password_hash, first_name, last_name, email, is_admin, "
                "created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (username.strip().lower(), hash_password(password), (first_name or "").strip() or None,
                 (last_name or "").strip() or None, (email or "").strip().lower() or None, int(is_admin), now, now))
    return conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()["id"]


def record_login(conn, user_id):
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), user_id))


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


def redeem_invite(conn, token, username, password, first_name, last_name, email):
    """Validate and consume the invite, creating the account. Everything runs on the caller's connection/transaction,
    so a failure here (or afterwards) leaves neither a used invite nor a half-made account. Returns the new user id."""
    row = get_invite(conn, token)
    if row is None:
        raise InviteError("This invite link isn't valid.")
    if row["used_by"] is not None:
        raise InviteError("This invite link has already been used.")
    if row["expires_at"] < time.time():
        raise InviteError("This invite link has expired - ask for a new one.")
    first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
    if not first_name or not last_name:
        raise InviteError("Enter your first and last name.")
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise InviteError("Enter a valid email address.")
    if get_by_email(conn, email):
        raise InviteError("An account already uses that email address.")
    username = (username or "").strip().lower()
    if not valid_username(username):
        raise InviteError("Usernames are 3-20 characters: lowercase letters, numbers, - or _.")
    if get_by_username(conn, username):
        raise InviteError("That username is taken.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise InviteError("Password must be at least %d characters." % MIN_PASSWORD_LENGTH)
    if count(conn) >= max_users():
        raise InviteError("This app is full (max %d accounts) - ask the admin to raise the limit." % max_users())
    new_id = create(conn, username, password, is_admin=False, first_name=first_name, last_name=last_name, email=email)
    conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE token = ?", (new_id, time.time(), token))
    return new_id


# ---- one-time migration from the pre-multi-user schema -------------------------------------------

def bootstrap_and_migrate(conn):
    """Runs on every startup (inside init_db, same transaction as the schema creation).

    Three independent steps:
    1. Always: make sure `activities` actually has the composite (user_id, id) primary key sync's upsert needs.
       Unconditional, not gated on anything else, because a database can be *fully account-migrated* and still
       have the wrong key here - that was a real shipped bug (see _fix_activities_table's docstring) that an
       ordinary "is this a legacy database?" check can't detect once the old 'auth' table is already gone.
    2. If this is a pre-multi-user database (it still has the old 'auth' table), give plan/meta the user_id
       column every query now expects - regardless of whether an account is created this run. Without this the
       app can't run at all against old data, not even in local/no-login mode. Existing rows are tagged
       LOCAL_USER_ID (the same id local/no-login use already writes under), so they're immediately usable,
       whether or not APP_PASSWORD is set.
    3. If APP_PASSWORD is set and there are no accounts yet, create the first (admin) account from it, and - if
       this was a legacy database - move that LOCAL_USER_ID data (Strava tokens included) onto the new account.

    Safe to interrupt and retry: every step here is guarded (an "already done?" check, or a WHERE clause that
    only matches what's left to do), so a crash partway through - however far it got - always converges to the
    fully correct state on the next startup, never a duplicate account or lost/miscounted data. This is not
    strict transactional atomicity: SQLite's ALTER/CREATE/DROP TABLE (via Python's sqlite3 module) don't reliably
    commit or roll back together with the surrounding transaction in this sqlite3/Python combination - verified
    inconsistent enough between statement types that relying on exactly where a retry resumes isn't safe. That's
    why every step here is instead independently idempotent, rather than relying on all-or-nothing rollback."""
    _add_profile_columns(conn)
    _fix_activities_table(conn, LOCAL_USER_ID)

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


def _add_profile_columns(conn):
    """Add first_name/last_name/email/last_login_at to `users` for a database that predates them - unconditional,
    every startup, same reasoning as _fix_activities_table. Unlike activities' primary key, none of these
    participate in a key, so a plain ALTER TABLE ADD COLUMN is safe here - no table rebuild needed."""
    cols = _table_columns(conn, "users")
    for col, coltype in (("first_name", "TEXT"), ("last_name", "TEXT"), ("email", "TEXT"), ("last_login_at", "REAL")):
        if col not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, coltype))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE)")


def _fix_activities_table(conn, default_user_id):
    """Make sure `activities` has the composite (user_id, id) PRIMARY KEY sync's upsert relies on
    (`ON CONFLICT(user_id, id)`) - unconditionally, every startup, regardless of migration state elsewhere.

    This covers three states found in the wild, all through the same idempotent rebuild (rename, recreate from
    ACTIVITIES_TABLE_SQL, copy, drop - tolerant of being interrupted and resumed, using INSERT OR IGNORE so a
    retry can never duplicate a row already copied):
      1. Already correct: left alone (bar backfilling any stray NULL user_id, normally a no-op).
      2. A genuinely un-migrated legacy table: no user_id column at all yet - added and backfilled to
         `default_user_id` as part of the rebuild.
      3. **Already account-migrated, but still with the broken key** - this is the bug that actually shipped:
         an earlier version added the user_id column with a plain ALTER TABLE (which SQLite allows) instead of
         rebuilding the table, so it kept its old single-column `id` primary key. Every row already has the
         correct real user_id from that migration, so this rebuild preserves those values as-is - it never
         overwrites a user_id that's already set, only supplies `default_user_id` for rows that never had one.
    Because the database can be in state 3 with no other trace of "used to be legacy" left (the old 'auth' table
    is long gone, accounts already exist), this function cannot be gated behind an is-this-legacy check the way
    the rest of the migration is - it has to run and check for itself, every time."""
    pk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)") if r["pk"]}
    if pk_cols != {"user_id", "id"} and not _table_columns(conn, "activities_old"):
        # not yet rebuilt, and no in-progress rebuild left over to resume - start one
        conn.execute("ALTER TABLE activities RENAME TO activities_old")
        conn.execute(ACTIVITIES_TABLE_SQL)

    if _table_columns(conn, "activities_old"):
        # either just started above, or a leftover from an earlier interrupted attempt (crucially: even when
        # `activities` itself ALREADY has the correct new (but then possibly still-empty) schema - CREATE TABLE
        # can survive a crash/rollback independently of the INSERT+DROP that were meant to follow it, so "the
        # PK already looks right" alone is not proof the copy actually finished; only an absent activities_old
        # is proof of that, which is what the branch above and this `if` are both really checking for)
        old_cols = [r["name"] for r in conn.execute("PRAGMA table_info(activities_old)")]
        collist = ", ".join(old_cols)
        if "user_id" in old_cols:   # state 3: keep each row's already-correct user_id, don't overwrite it
            conn.execute("INSERT OR IGNORE INTO activities (%s) SELECT %s FROM activities_old" % (collist, collist))
        else:                        # state 2: no user_id existed - every row gets the same default
            conn.execute("INSERT OR IGNORE INTO activities (%s, user_id) SELECT %s, ? FROM activities_old"
                         % (collist, collist), (default_user_id,))
        conn.execute("DROP TABLE activities_old")
    else:
        conn.execute("UPDATE activities SET user_id = ? WHERE user_id IS NULL", (default_user_id,))


def _add_user_id_columns(conn, default_user_id):
    """Give plan/meta their user_id column, tagging existing rows with `default_user_id`. Idempotent, and safe
    whether or not an account ends up being bootstrapped this run. (`activities` is handled separately and
    unconditionally by _fix_activities_table - see bootstrap_and_migrate.)"""
    if "user_id" not in _table_columns(conn, "plan"):
        conn.execute("ALTER TABLE plan ADD COLUMN user_id INTEGER")
    conn.execute("UPDATE plan SET user_id = ? WHERE user_id IS NULL", (default_user_id,))

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
