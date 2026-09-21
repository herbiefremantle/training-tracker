import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_path():
    """FITNESS_DB if set (the Docker image sets /data/app.db, a Railway volume), else training.db next to the code."""
    return os.environ.get("FITNESS_DB") or str(ROOT / "training.db")


def volume_warning():
    """A message if the database is meant to live on the /data volume but nothing is mounted there.

    Without a volume the container's disk is thrown away on every deploy, taking the Strava tokens and the
    whole database with it - and nothing else would tell you, because the app runs fine until it restarts."""
    if Path(db_path()).is_relative_to("/data") and not os.path.ismount("/data"):
        return ("Database is %s but no volume is mounted at /data - everything will be LOST on the next "
                "deploy or restart. Attach a Railway volume with mount path /data." % db_path())
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS auth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    athlete_id    INTEGER,
    athlete_name  TEXT,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    scope         TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id                   INTEGER PRIMARY KEY,   -- Strava activity id
    date                 TEXT NOT NULL,         -- local calendar date (from start_date_local)
    start_epoch          INTEGER NOT NULL,      -- UTC start, for incremental sync
    name                 TEXT,
    sport_type           TEXT,
    sport_group          TEXT,
    distance             REAL,                  -- metres
    moving_time          INTEGER,               -- seconds
    average_heartrate    REAL,
    max_heartrate        REAL,
    average_speed        REAL,                  -- m/s
    max_speed            REAL,                  -- m/s
    total_elevation_gain REAL,                  -- metres
    suffer_score         REAL,
    workout_type         INTEGER,
    detail_checked       INTEGER NOT NULL DEFAULT 0  -- 1 once we've asked /activities/{id} for suffer_score
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS plan (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    date                 TEXT NOT NULL,
    session_type         TEXT,
    sport                TEXT,                  -- as written in the plan
    sport_group          TEXT NOT NULL,
    planned_distance_km  REAL,
    planned_duration_min REAL,
    notes                TEXT,
    position             INTEGER NOT NULL DEFAULT 0   -- row order within the upload
);
CREATE INDEX IF NOT EXISTS idx_plan_date ON plan(date);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def init_db():
    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


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


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
