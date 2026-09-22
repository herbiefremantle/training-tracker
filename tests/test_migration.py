"""The one-time move from the pre-multi-user database (what's actually running on Railway right now) to the
new per-account schema. This is the highest-stakes code in the app - it runs once, automatically, against
real data - so it's tested against a hand-built copy of the *old* schema, independent of the current one.
"""
import sqlite3
import time

import pytest

from app import db, users

# The schema as it was before multi-user support - frozen here on purpose, not imported from app.db, so this
# test keeps checking the real old shape even if app.db's SCHEMA constant changes later.
LEGACY_SCHEMA = """
CREATE TABLE auth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    athlete_id    INTEGER,
    athlete_name  TEXT,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    scope         TEXT
);
CREATE TABLE activities (
    id INTEGER PRIMARY KEY, date TEXT NOT NULL, start_epoch INTEGER NOT NULL, name TEXT, sport_type TEXT,
    sport_group TEXT, distance REAL, moving_time INTEGER, average_heartrate REAL, max_heartrate REAL,
    average_speed REAL, max_speed REAL, total_elevation_gain REAL, suffer_score REAL, workout_type INTEGER,
    detail_checked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, session_type TEXT, sport TEXT,
    sport_group TEXT NOT NULL, planned_distance_km REAL, planned_duration_min REAL, notes TEXT,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def write_legacy_db(path, n_activities=5, n_plan=3, with_strava_connection=True):
    """A standalone legacy-shaped database with sample data, exactly like a real pre-migration install."""
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    if with_strava_connection:
        conn.execute("INSERT INTO auth VALUES (1, 555, 'Pete Wheildon', 'tok-access', 'tok-refresh', ?, "
                     "'read,activity:read_all')", (int(time.time()) + 21600,))
    for i in range(n_activities):
        conn.execute("INSERT INTO activities (id, date, start_epoch, name, sport_type, sport_group, distance, "
                     "moving_time) VALUES (?, ?, ?, ?, 'Run', 'run', 10000, 3600)",
                     (1000 + i, "2026-09-%02d" % (i + 1), 1758000000 + i * 86400, "Run %d" % i))
    for i in range(n_plan):
        conn.execute("INSERT INTO plan (date, session_type, sport, sport_group, planned_distance_km, position) "
                     "VALUES (?, 'Easy', 'Run', 'run', 8, ?)", ("2026-09-%02d" % (i + 1), i))
    conn.execute("INSERT INTO meta VALUES ('last_sync', '2026-09-21T07:00:00')")
    conn.commit()
    conn.close()


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    write_legacy_db(path)
    monkeypatch.setenv("FITNESS_DB", str(path))
    return path


def test_migration_moves_everything_onto_one_new_admin_account(legacy_db, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    db.init_db()
    with db.connect() as conn:
        accounts = conn.execute("SELECT * FROM users").fetchall()
        assert len(accounts) == 1
        admin = accounts[0]
        assert admin["username"] == "pete" and admin["is_admin"] == 1

        token = conn.execute("SELECT * FROM strava_auth WHERE user_id = ?", (admin["id"],)).fetchone()
        assert (token["athlete_id"], token["athlete_name"], token["access_token"], token["refresh_token"]) == \
            (555, "Pete Wheildon", "tok-access", "tok-refresh")

        acts = conn.execute("SELECT * FROM activities").fetchall()
        assert len(acts) == 5 and all(a["user_id"] == admin["id"] for a in acts)
        assert {a["id"] for a in acts} == {1000, 1001, 1002, 1003, 1004}   # every row survived, none duplicated

        plans = conn.execute("SELECT * FROM plan").fetchall()
        assert len(plans) == 3 and all(p["user_id"] == admin["id"] for p in plans)

        assert db.get_meta(conn, admin["id"], "last_sync") == "2026-09-21T07:00:00"

        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'auth'").fetchone() is None  # old table gone


def test_migration_respects_admin_username_override(legacy_db, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("ADMIN_USERNAME", "herbie")
    db.init_db()
    with db.connect() as conn:
        assert users.get_by_username(conn, "herbie") is not None
        assert users.get_by_username(conn, "pete") is None


def test_migrated_admin_can_log_in_with_the_original_app_password(legacy_db, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    monkeypatch.setenv("SESSION_SECRET", "a-long-random-session-secret-value")
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post("/login", data={"username": "pete", "password": "correct horse battery staple"})
        assert r.status_code == 303
        s = c.get("/api/status").json()
        assert s["connected"] and s["athlete"] == "Pete Wheildon" and s["activity_count"] == 5 and s["plan_count"] == 3


def test_migration_with_no_strava_connection_yet(tmp_path, monkeypatch):
    """A legacy install that was never actually connected to Strava (no row in the old auth table)."""
    path = tmp_path / "legacy.db"
    write_legacy_db(path, with_strava_connection=False)
    monkeypatch.setenv("FITNESS_DB", str(path))
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    db.init_db()
    with db.connect() as conn:
        admin = conn.execute("SELECT * FROM users").fetchone()
        assert conn.execute("SELECT * FROM strava_auth WHERE user_id = ?", (admin["id"],)).fetchone() is None
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 5   # still migrated


def test_migration_without_app_password_becomes_local_mode_data(legacy_db):
    """No APP_PASSWORD set: no admin account is created, but the schema still has to gain user_id (every query
    needs it to run at all) - existing rows become LOCAL_USER_ID data, i.e. exactly like local/no-login use.
    The old Strava connection is left alone (untouched, undropped) until a real admin account claims it."""
    db.init_db()
    with db.connect() as conn:
        assert users.count(conn) == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'auth'").fetchone() is not None
        acts = conn.execute("SELECT * FROM activities").fetchall()
        assert len(acts) == 5 and all(a["user_id"] == db.LOCAL_USER_ID for a in acts)
        assert conn.execute("SELECT COUNT(*) FROM strava_auth").fetchone()[0] == 0  # not migrated yet


def test_migration_is_idempotent(legacy_db, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    db.init_db()
    db.init_db()   # a second startup against the now-migrated database
    db.init_db()
    with db.connect() as conn:
        assert users.count(conn) == 1
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0] == 3


def test_a_failure_partway_through_can_be_retried_safely(legacy_db, monkeypatch):
    """If the migration dies partway (a crash, a bug, a full disk), a retry must converge to the fully correct
    state - not a duplicate admin account, not data lost or double-counted.

    Note this is *not* pure transactional atomicity: SQLite's ALTER TABLE (Python's sqlite3 module) commits
    itself immediately regardless of the enclosing transaction, so the user_id column can end up added even
    though a later step in the same run fails and rolls back. What matters is that every step here (the column
    add, the backfill, the account creation, the reassignment) is idempotent and safe to redo, so a retry from
    any interruption point still ends up correct - which is what this test actually proves."""
    # Patched by hand (not monkeypatch.setattr) and restored in a finally: monkeypatch.undo() would revert
    # every change made through this test's `monkeypatch` fixture, INCLUDING the `legacy_db` fixture's
    # FITNESS_DB - which would point db.init_db() at whatever real local database happens to be sitting next
    # to the code. That happened once while writing this test; verified harmless (already-migrated, so a
    # no-op), but the fix is to never let a DB-path env var be something `.undo()` can touch.
    original = users._reassign_legacy_data

    def boom(conn, admin_id):
        raise RuntimeError("simulated crash partway through migration")
    users._reassign_legacy_data = boom
    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            db.init_db()
        with db.connect() as conn:
            assert users.count(conn) == 0   # the admin INSERT itself (plain DML) rolled back correctly
    finally:
        users._reassign_legacy_data = original

    db.init_db()         # retried from wherever the crash left things - must now converge to fully correct
    with db.connect() as conn:
        assert users.count(conn) == 1
        acts = conn.execute("SELECT * FROM activities").fetchall()
        assert len(acts) == 5 and {a["id"] for a in acts} == {1000, 1001, 1002, 1003, 1004}
        admin_id = conn.execute("SELECT id FROM users").fetchone()["id"]
        assert all(a["user_id"] == admin_id for a in acts)   # not left on LOCAL_USER_ID, not duplicated
        assert conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0] == 3
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'auth'").fetchone() is None


def test_short_app_password_leaves_no_partial_account(legacy_db, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="too short"):
        db.init_db()
    with db.connect() as conn:
        assert users.count(conn) == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'auth'").fetchone() is not None
