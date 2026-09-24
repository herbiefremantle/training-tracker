"""Accounts and invite-only sign-up, plus the one-time move from the old single-user database to this schema.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib only - no extra dependency for a handful of accounts).
"""
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import date

from .db import ACTIVITIES_TABLE_SQL, LOCAL_USER_ID

MIN_PASSWORD_LENGTH = 8
INVITE_TTL_SECONDS = 7 * 24 * 3600
RESET_TTL_SECONDS = 24 * 3600   # shorter than an invite: this one grants control of an *existing* account
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
    return conn.execute("SELECT username, first_name, last_name, email, is_admin, is_demo, created_at, "
                        "last_login_at, last_active_at FROM users ORDER BY created_at").fetchall()


def get_demo_account(conn):
    """The one shared read-only demo login, or None if DEMO_ACCOUNT was never enabled - see demo_data.py."""
    return conn.execute("SELECT * FROM users WHERE is_demo = 1 LIMIT 1").fetchone()


def create(conn, username, password, is_admin=False, is_demo=False, first_name=None, last_name=None, email=None):
    now = time.time()
    # account creation logs you straight in (see app/auth.py), so "last login"/"last active" start out matching "signed up"
    conn.execute("INSERT INTO users (username, password_hash, first_name, last_name, email, is_admin, is_demo, "
                "created_at, last_login_at, last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username.strip().lower(), hash_password(password), (first_name or "").strip() or None,
                 (last_name or "").strip() or None, (email or "").strip().lower() or None, int(is_admin),
                 int(is_demo), now, now, now))
    return conn.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()["id"]


def record_login(conn, user_id):
    """An actual credential login: the /login form, or redeeming an invite/reset link."""
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (time.time(), user_id))


def record_activity(conn, user_id):
    """Any authenticated request - see app/auth.py:resolve_user, which only calls this at most once a day per
    account, so "last active" reflects someone actually opening the app (session cookies last 30 days, so most
    days nobody hits /login at all - last_login_at alone would look stale even for a daily user)."""
    conn.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (time.time(), user_id))


def is_new_day(last_active_at, today):
    """Whether `today` (a date) is later than the local calendar date last_active_at (an epoch seconds, or
    None) falls on - i.e. whether resolve_user should bother writing a fresh last_active_at."""
    if last_active_at is None:
        return True
    return date.fromtimestamp(last_active_at) < today


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


# ---- password resets (admin-generated link; no email sending yet) -------------------------------

def create_reset_link(conn, user_id, created_by_user_id):
    token = secrets.token_urlsafe(20)
    now = time.time()
    conn.execute("INSERT INTO password_resets (token, user_id, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)", (token, user_id, created_by_user_id, now, now + RESET_TTL_SECONDS))
    return token


def get_reset_link(conn, token):
    return conn.execute("SELECT * FROM password_resets WHERE token = ?", (token,)).fetchone()


class ResetError(ValueError):
    """A user-facing reason a password reset link couldn't be redeemed."""


def redeem_reset_link(conn, token, password, password2):
    """Validate the link and set the new password. Returns the account's user id (the caller logs them
    straight in with it, same as redeeming an invite does)."""
    row = get_reset_link(conn, token)
    if row is None:
        raise ResetError("This password reset link isn't valid.")
    if row["used_at"] is not None:
        raise ResetError("This password reset link has already been used.")
    if row["expires_at"] < time.time():
        raise ResetError("This password reset link has expired - ask an admin to send a new one.")
    if password != password2:
        raise ResetError("Passwords don't match.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ResetError("Password must be at least %d characters." % MIN_PASSWORD_LENGTH)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row["user_id"]))
    conn.execute("UPDATE password_resets SET used_at = ? WHERE token = ?", (time.time(), token))
    return row["user_id"]


# ---- one-time migration from the pre-multi-user schema -------------------------------------------

def bootstrap_and_migrate(conn):
    """Runs on every startup (inside init_db, same transaction as the schema creation).

    Four independent steps:
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
    4. If DEMO_ACCOUNT is set, create the fixed demo/DEMO_PASSWORD (default "demo") login if it doesn't exist
       yet, or update its password if DEMO_PASSWORD has changed since - see app/demo_data.py for what the
       account's for and who's allowed to touch it. Deliberately *after* step 3 and never gated on "no
       accounts yet" itself - creating the demo account must never be what makes step 3 above see
       count(conn) > 0 and skip bootstrapping the real admin account (that was a real bug here: the two were
       adjacent enough in an earlier version that this shipped broken - see tests/test_demo_account.py). Runs
       on every startup, not just the first, so enabling DEMO_ACCOUNT (or changing DEMO_PASSWORD) later, on an
       already-running deployment, still takes effect on the next restart.

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

    if count(conn) == 0:
        password = os.environ.get("APP_PASSWORD", "").strip()
        if not password:
            pass   # nothing to bootstrap yet - the app runs with zero accounts (login off) until one is created
        elif len(password) < MIN_PASSWORD_LENGTH:
            raise RuntimeError("APP_PASSWORD is too short: use at least %d characters." % MIN_PASSWORD_LENGTH)
        else:
            username = (os.environ.get("ADMIN_USERNAME", "").strip().lower() or "pete")
            if not valid_username(username):
                username = "admin"
            admin_id = create(conn, username, password, is_admin=True)
            if is_legacy:
                _reassign_legacy_data(conn, admin_id)

    if os.environ.get("DEMO_ACCOUNT", "").strip().lower() in ("1", "true", "yes"):
        # DEMO_PASSWORD defaults to "demo" (the point is a memorable, public credential), but browsers'
        # breached-password checkers (e.g. Chrome's) flag "demo" itself on sight, since it's such a common
        # password elsewhere - DEMO_PASSWORD lets that be swapped for something just as simple but less
        # likely to trip that specific warning, without a code change or losing the account's data.
        demo_password = os.environ.get("DEMO_PASSWORD", "").strip() or "demo"
        demo = get_demo_account(conn)
        if demo is None:
            create(conn, "demo", demo_password, is_demo=True, first_name="Demo", last_name="Account")
        elif not verify_password(demo_password, demo["password_hash"]):
            # picks up a changed DEMO_PASSWORD on the next restart, rather than only ever mattering on the
            # account's original creation - the same reasoning _add_profile_columns etc. already follow
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(demo_password), demo["id"]))


def _table_columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _add_profile_columns(conn):
    """Add first_name/last_name/email/last_login_at/last_active_at/is_demo to `users` for a database that
    predates them - unconditional, every startup, same reasoning as _fix_activities_table. Unlike activities'
    primary key, none of these participate in a key, so a plain ALTER TABLE ADD COLUMN is safe here - no table
    rebuild needed."""
    cols = _table_columns(conn, "users")
    for col, coltype in (("first_name", "TEXT"), ("last_name", "TEXT"), ("email", "TEXT"),
                          ("last_login_at", "REAL"), ("last_active_at", "REAL"),
                          ("is_demo", "INTEGER NOT NULL DEFAULT 0")):
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
