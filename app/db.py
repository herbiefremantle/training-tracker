import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The implicit account used when no login is configured (local/dev use, or a legacy un-migrated install with
# REQUIRE_AUTH unset). 0, never a real user id (those start at 1 via AUTOINCREMENT), so it can't collide with one.
LOCAL_USER_ID = 0


def db_path():
    """FITNESS_DB if set (the Docker image sets /data/app.db, a Railway volume), else training.db next to the code."""
    return os.environ.get("FITNESS_DB") or str(ROOT / "training.db")


def volume_warning():
    """A message if the database is meant to live on the /data volume but nothing is mounted there.

    Without a volume the container's disk is thrown away on every deploy, taking every account's Strava tokens and
    data with it - and nothing else would tell you, because the app runs fine until it restarts."""
    if Path(db_path()).is_relative_to("/data") and not os.path.ismount("/data"):
        return ("Database is %s but no volume is mounted at /data - everything will be LOST on the next "
                "deploy or restart. Attach a Railway volume with mount path /data." % db_path())
    return None


# Tables are per-account (user_id) so one person's Strava connection, activities and plan never mix with another's.
# LOCAL_USER_ID is used throughout when there's no login (see app/auth.py).

# Its own constant because the legacy-database migration (app/users.py) has to rebuild this exact table (SQLite
# can't ALTER a PRIMARY KEY, and sync's upsert relies on the composite one) - sharing this string is what keeps
# the rebuilt table byte-for-byte identical to a fresh one, so they can never quietly drift apart.
ACTIVITIES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS activities (
    id                   INTEGER NOT NULL,   -- Strava activity id (globally unique on Strava)
    user_id              INTEGER NOT NULL REFERENCES users(id),
    date                 TEXT NOT NULL,      -- local calendar date (from start_date_local)
    start_epoch          INTEGER NOT NULL,   -- UTC start, for incremental sync
    name                 TEXT,
    sport_type           TEXT,
    sport_group          TEXT,
    distance             REAL,               -- metres
    moving_time          INTEGER,            -- seconds
    average_heartrate    REAL,
    max_heartrate        REAL,
    average_speed        REAL,               -- m/s
    max_speed            REAL,               -- m/s
    total_elevation_gain REAL,               -- metres
    suffer_score         REAL,
    workout_type         INTEGER,
    detail_checked       INTEGER NOT NULL DEFAULT 0,  -- 1 once we've asked /activities/{id} for suffer_score
    PRIMARY KEY (user_id, id)
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    first_name    TEXT,
    last_name     TEXT,
    email         TEXT,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    is_demo        INTEGER NOT NULL DEFAULT 0,   -- the one shared read-only demo login - see app/demo_data.py
    created_at     REAL NOT NULL,
    last_login_at  REAL,   -- an actual credential login (the /login form or redeeming an invite/reset link)
    last_active_at REAL    -- any authenticated request, at most once/day - see app/auth.py:resolve_user
);
-- No CREATE INDEX for email here: on a database that predates this column, that index would run before
-- users.py's migration adds the column (same class of bug _fix_activities_table exists to fix - see there).
-- app/users.py:_add_profile_columns creates it itself, always after the column is guaranteed to exist.

CREATE TABLE IF NOT EXISTS invites (
    token       TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_by     INTEGER REFERENCES users(id),
    used_at     REAL
);

-- An admin-generated link to set a new password for an *existing* account (no email sending yet - the admin
-- creates the link and sends it however they like, same as an invite). Deliberately separate from `invites`:
-- an invite creates a brand-new account, this only ever updates one that already exists.
CREATE TABLE IF NOT EXISTS password_resets (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);

CREATE TABLE IF NOT EXISTS strava_auth (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id),
    athlete_id    INTEGER,
    athlete_name  TEXT,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    scope         TEXT
);
""" + ACTIVITIES_TABLE_SQL + """
CREATE TABLE IF NOT EXISTS plan (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    date                 TEXT NOT NULL,
    session_type         TEXT,
    sport                TEXT,               -- as written in the plan
    sport_group          TEXT NOT NULL,
    planned_distance_km  REAL,
    planned_duration_min REAL,
    notes                TEXT,
    position             INTEGER NOT NULL DEFAULT 0   -- row order within the upload
);

CREATE TABLE IF NOT EXISTS meta (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (user_id, key));

-- Fuelling (app/nutrition.py): one race goal per account, a short list of the person's own products, and a log per
-- day. Logged items copy the product's numbers at the time, so editing or deleting a product later never rewrites
-- what someone actually had. None of this comes from Strava, so disconnecting Strava leaves it alone.
CREATE TABLE IF NOT EXISTS fuel_goal (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id),
    race_name        TEXT,
    race_date        TEXT NOT NULL,
    race_hours       REAL,               -- expected race time, optional: only used for the race-day total
    target_g_per_hr  REAL NOT NULL,      -- carbs per hour to be able to take on race day
    current_g_per_hr REAL NOT NULL,      -- what they take comfortably now: where the ramp starts
    ramp_g_per_hr    REAL NOT NULL DEFAULT 5,
    start_date       TEXT NOT NULL       -- first long run that gets a target
);

-- Hydration and conditions: where the person runs (for the weather forecast), when they usually start, and their
-- usual fluid and sodium per hour, which the advice scales up or down with the temperature. Optional - a row only
-- exists once they've filled it in. Location is kept to ~1 km, the most the forecast needs.
CREATE TABLE IF NOT EXISTS fuel_prefs (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id),
    place            TEXT,
    lat              REAL,
    lon              REAL,
    start_hour       INTEGER NOT NULL DEFAULT 8,
    fluid_ml_per_hr  REAL NOT NULL DEFAULT 500,
    sodium_mg_per_hr REAL NOT NULL DEFAULT 400,
    race_temp_c      REAL                -- expected race-day temperature, used until a forecast reaches that far
);

CREATE TABLE IF NOT EXISTS fuel_products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- bar | drink | gel | other
    unit        TEXT NOT NULL,           -- what one unit is called: bar, flask, gel...
    carbs_g     REAL NOT NULL,
    sodium_mg   REAL NOT NULL DEFAULT 0,
    caffeine_mg REAL NOT NULL DEFAULT 0,
    fluid_ml    REAL NOT NULL DEFAULT 0,      -- fluid in one unit: a 500 ml flask is 500, a bar 0
    use_in_plan INTEGER NOT NULL DEFAULT 1,   -- may the planner put it in a suggested mix (else tracking only)
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fuel_logs (
    user_id INTEGER NOT NULL REFERENCES users(id),
    date    TEXT NOT NULL,
    gut     TEXT,                        -- fine | some | bad (how the stomach coped), or NULL
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS fuel_log_items (
    user_id      INTEGER NOT NULL REFERENCES users(id),
    date         TEXT NOT NULL,
    product_name TEXT NOT NULL,
    kind         TEXT NOT NULL,
    unit         TEXT NOT NULL,
    carbs_g      REAL NOT NULL,          -- per unit, as it was when logged
    sodium_mg    REAL NOT NULL,
    caffeine_mg  REAL NOT NULL,
    fluid_ml     REAL NOT NULL DEFAULT 0,
    qty          REAL NOT NULL,          -- in halves
    PRIMARY KEY (user_id, date, product_name)
);
"""

# Indexed on user_id, which a legacy (pre-multi-user) activities/plan table won't have until migration adds it -
# so these run *after* bootstrap_and_migrate, never bundled into SCHEMA above.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(user_id, date);
CREATE INDEX IF NOT EXISTS idx_plan_date ON plan(user_id, date);
CREATE INDEX IF NOT EXISTS idx_fuel_products_user ON fuel_products(user_id);
"""


def init_db():
    """Create the schema (new installs), migrate any pre-multi-user database in place (see app/users.py), then
    index - in that order, since a legacy table only gets its user_id column during the migration step."""
    from . import users   # deferred: users.py imports this module, so import here to avoid a cycle at load time

    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _add_fuel_fluid_columns(conn)
        users.bootstrap_and_migrate(conn)
        conn.executescript(INDEXES)


def _add_fuel_fluid_columns(conn):
    """Fuelling products and logs gained a fluid column after the first release. Added once to an existing
    database; the Styrkr MIX60 preset (a sachet in a 500 ml flask) is given its 500 ml so a person who already
    added it doesn't have to go back and edit it."""
    for table in ("fuel_products", "fuel_log_items"):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if "fluid_ml" not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN fluid_ml REAL NOT NULL DEFAULT 0" % table)
            if table == "fuel_products":
                conn.execute("UPDATE fuel_products SET fluid_ml = 500 WHERE name = 'Styrkr MIX60' AND kind = 'drink'")


@contextmanager
def connect():
    """One short-lived connection per call; commits on success, rolls back on error."""
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(conn, user_id, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
    return row["value"] if row else default


def set_meta(conn, user_id, key, value):
    conn.execute(
        "INSERT INTO meta(user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (user_id, key, str(value)),
    )
