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


def test_already_migrated_database_with_the_broken_key_self_repairs(tmp_path, monkeypatch):
    """The actual shape of the bug that shipped and broke sync in production: a database that already went
    through account migration (has a real admin account, activities has a user_id column, the old 'auth' table
    is long gone) but activities never got its primary key rebuilt to (user_id, id) - because the version that
    ran this migration only added the column via ALTER TABLE. This has no legacy 'auth' table to signal
    "migrate me", so the fix has to detect and repair it unconditionally, every startup - this proves it does,
    without losing or reassigning any already-correct data."""
    path = tmp_path / "already_migrated_but_broken.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE TABLE invites (token TEXT PRIMARY KEY, created_by INTEGER, created_at REAL, expires_at REAL,
            used_by INTEGER, used_at REAL);
        CREATE TABLE strava_auth (user_id INTEGER PRIMARY KEY, athlete_id INTEGER, athlete_name TEXT,
            access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, expires_at INTEGER NOT NULL, scope TEXT);
        CREATE TABLE activities (
            id INTEGER PRIMARY KEY, user_id INTEGER, date TEXT NOT NULL, start_epoch INTEGER NOT NULL, name TEXT,
            sport_type TEXT, sport_group TEXT, distance REAL, moving_time INTEGER, average_heartrate REAL,
            max_heartrate REAL, average_speed REAL, max_speed REAL, total_elevation_gain REAL, suffer_score REAL,
            workout_type INTEGER, detail_checked INTEGER NOT NULL DEFAULT 0
        );  -- the bug: user_id is just a plain column here, PRIMARY KEY is still `id` alone
        CREATE TABLE plan (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT NOT NULL,
            session_type TEXT, sport TEXT, sport_group TEXT NOT NULL, planned_distance_km REAL,
            planned_duration_min REAL, notes TEXT, position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE meta (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (user_id, key));
    """)
    conn.execute("INSERT INTO users (id, username, password_hash, is_admin, created_at) "
                "VALUES (1, 'pete', 'pbkdf2_sha256$1$aa$bb', 1, 0)")
    conn.execute("INSERT INTO strava_auth VALUES (1, 555, 'Pete Wheildon', 'tok-a', 'tok-r', 9999999999, 'read')")
    for i in range(5):
        conn.execute("INSERT INTO activities (id, user_id, date, start_epoch, name) VALUES (?, 1, ?, ?, ?)",
                     (2000 + i, "2026-09-%02d" % (i + 1), 1758000000 + i * 86400, "Real activity %d" % i))
    conn.execute("INSERT INTO plan (user_id, date, sport_group) VALUES (1, '2026-09-10', 'run')")
    conn.execute("INSERT INTO meta VALUES (1, 'last_sync', '2026-09-20T07:00:00')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("FITNESS_DB", str(path))
    # no APP_PASSWORD - this run must not depend on it; an admin already exists, migration is not "the first run"
    db.init_db()

    with db.connect() as conn:
        pk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)") if r["pk"]}
        assert pk_cols == {"user_id", "id"}
        assert users.count(conn) == 1   # not duplicated - still just pete
        acts = conn.execute("SELECT * FROM activities").fetchall()
        assert len(acts) == 5 and all(a["user_id"] == 1 for a in acts)   # preserved, not reset to LOCAL_USER_ID
        assert {a["id"] for a in acts} == {2000, 2001, 2002, 2003, 2004}
        assert conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0] == 1
        assert db.get_meta(conn, 1, "last_sync") == "2026-09-20T07:00:00"
        assert conn.execute("SELECT athlete_name FROM strava_auth WHERE user_id = 1").fetchone()[0] == "Pete Wheildon"

    # and now sync's upsert actually works against it - the whole point of the repair
    import httpx
    from app import strava
    from tests.test_core import FakeStrava, strava_activity
    with db.connect() as conn:
        conn.execute("UPDATE strava_auth SET access_token='access-1', refresh_token='refresh-0' WHERE user_id=1")
        # include every already-there activity, not just the ones this test cares about - anything omitted
        # falls inside the 14-day resync window and sync() correctly (and separately, see test_core.py) treats
        # a locally-stored activity missing from the response as deleted on Strava
        fake = FakeStrava([strava_activity(2000, "2026-09-01T07:00:00Z", name="Renamed"),
                           strava_activity(2001, "2026-09-02T07:00:00Z"), strava_activity(2002, "2026-09-03T07:00:00Z"),
                           strava_activity(2003, "2026-09-04T07:00:00Z"), strava_activity(2004, "2026-09-05T07:00:00Z"),
                           strava_activity(7777, "2026-09-21T07:00:00Z")])
        result = strava.sync(conn, httpx.Client(transport=httpx.MockTransport(fake)), 1)
        assert result["new"] == 1
        assert conn.execute("SELECT name FROM activities WHERE id = 2000").fetchone()[0] == "Renamed"

    db.init_db()   # idempotent: running the repair again changes nothing further
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 6
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'activities_old'").fetchone() is None


def test_sync_upsert_works_against_a_migrated_activities_table(legacy_db, monkeypatch):
    """The real bug this file exists to catch: the migrated `activities` table must end up with the composite
    (user_id, id) primary key sync's upsert relies on (`ON CONFLICT(user_id, id)`) - not just a user_id column
    added on the side. Every other sync test in this suite runs against a table CREATEd fresh with that key
    already correct, so none of them would have caught a migrated table quietly missing it. This one syncs
    against actual post-migration data, which is what surfaced the bug for real (on a live deployment)."""
    import httpx

    from app import strava
    from tests.test_core import FakeStrava, strava_activity

    monkeypatch.setenv("APP_PASSWORD", "correct horse battery staple")
    db.init_db()
    with db.connect() as conn:
        admin_id = conn.execute("SELECT id FROM users").fetchone()["id"]
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(activities)")}
        assert "user_id" in cols

        # a genuine upsert: one existing (migrated) row updated, one brand new row inserted - both go through
        # the exact ON CONFLICT(user_id, id) clause that broke on the live site
        # include every already-there activity (1000-1004, seeded by write_legacy_db) - see the comment on the
        # analogous line in test_already_migrated_database_with_the_broken_key_self_repairs for why
        fake = FakeStrava([strava_activity(1000, "2026-09-01T07:00:00Z", name="Renamed"),
                           strava_activity(1001, "2026-09-02T07:00:00Z"), strava_activity(1002, "2026-09-03T07:00:00Z"),
                           strava_activity(1003, "2026-09-04T07:00:00Z"), strava_activity(1004, "2026-09-05T07:00:00Z"),
                           strava_activity(9999, "2026-09-21T07:00:00Z")])
        http = httpx.Client(transport=httpx.MockTransport(fake))
        # the legacy fixture's Strava connection already migrated onto this account; give it a valid (matching
        # FakeStrava's own token-format expectations), unexpired token so this test exercises the upsert, not refresh
        conn.execute("UPDATE strava_auth SET access_token = 'access-1', refresh_token = 'refresh-0', "
                     "expires_at = ? WHERE user_id = ?", (int(time.time()) + 3600, admin_id))
        result = strava.sync(conn, http, admin_id)   # must not raise sqlite3.OperationalError
        assert result["new"] == 1

        row = conn.execute("SELECT name FROM activities WHERE id = 1000 AND user_id = ?", (admin_id,)).fetchone()
        assert row["name"] == "Renamed"
        assert conn.execute("SELECT 1 FROM activities WHERE id = 9999 AND user_id = ?", (admin_id,)).fetchone()

        # the primary key really is (user_id, id) now, not just "id" with a spare column
        pk_cols = [r["name"] for r in conn.execute("PRAGMA table_info(activities)") if r["pk"]]
        assert set(pk_cols) == {"user_id", "id"}


def test_short_app_password_leaves_no_partial_account(legacy_db, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="too short"):
        db.init_db()
    with db.connect() as conn:
        assert users.count(conn) == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'auth'").fetchone() is not None
