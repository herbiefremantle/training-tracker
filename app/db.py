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
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    token       TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_by     INTEGER REFERENCES users(id),
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
"""

# Indexed on user_id, which a legacy (pre-multi-user) activities/plan table won't have until migration adds it -
# so these run *after* bootstrap_and_migrate, never bundled into SCHEMA above.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(user_id, date);
CREATE INDEX IF NOT EXISTS idx_plan_date ON plan(user_id, date);
"""


def init_db():
    """Create the schema (new installs), migrate any pre-multi-user database in place (see app/users.py), then
    index - in that order, since a legacy table only gets its user_id column during the migration step."""
    from . import users   # deferred: users.py imports this module, so import here to avoid a cycle at load time

    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        users.bootstrap_and_migrate(conn)
        conn.executescript(INDEXES)


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
