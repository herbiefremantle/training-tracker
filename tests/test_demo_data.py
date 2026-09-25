"""app/demo_data.py: the self-updating plan/activities behind the shared demo login."""
from collections import Counter
from datetime import date, timedelta

import pytest

from app import db, demo_data, matching, metrics, plan_templates, users


@pytest.fixture()
def conn_and_demo_id(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "demo.db"))
    db.init_db()
    with db.connect() as conn:
        demo_id = users.create(conn, "demotracker", "demo", is_demo=True)
    with db.connect() as conn:
        yield conn, demo_id


def test_regenerate_refuses_a_non_demo_account(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "real.db"))
    db.init_db()
    with db.connect() as conn:
        real_id = users.create(conn, "pete", "a-real-password", is_admin=True)
    with db.connect() as conn:
        with pytest.raises(ValueError):
            demo_data.regenerate(conn, real_id, date(2026, 9, 24))


def test_regenerate_refuses_an_unknown_user_id(conn_and_demo_id):
    conn, _demo_id = conn_and_demo_id
    with pytest.raises(ValueError):
        demo_data.regenerate(conn, 99999, date(2026, 9, 24))


@pytest.mark.parametrize("today", [date(2026, 9, 24), date(2026, 1, 5), date(2026, 12, 30), date(2026, 6, 15)])
def test_regenerate_produces_the_requested_status_mix(conn_and_demo_id, today):
    conn, demo_id = conn_and_demo_id
    demo_data.regenerate(conn, demo_id, today)

    plan = conn.execute("SELECT * FROM plan WHERE user_id = ?", (demo_id,)).fetchall()
    activities = conn.execute("SELECT * FROM activities WHERE user_id = ?", (demo_id,)).fetchall()
    result = matching.match(plan, activities, today.isoformat())
    counts = Counter(s["status"] for s in result["sessions"])

    assert counts["missed"] == demo_data.MISSED_COUNT
    assert counts["done"] > 0
    assert len(result["extras"]) == 1
    # next 7 days: real planned sessions, genuinely unmatched
    next_week = [s for s in result["sessions"] if today.isoformat() < s["date"]][:7]
    assert next_week and all(s["status"] in ("upcoming", "rest") for s in next_week)


@pytest.mark.parametrize("today", [date(2026, 9, 24), date(2026, 1, 5), date(2026, 12, 30), date(2026, 6, 15)])
def test_regenerate_produces_a_believable_on_track_load_ratio(conn_and_demo_id, today):
    conn, demo_id = conn_and_demo_id
    demo_data.regenerate(conn, demo_id, today)
    activities = conn.execute("SELECT * FROM activities WHERE user_id = ?", (demo_id,)).fetchall()
    ratio = metrics.load_summary(activities, today)["ratio"]
    assert 0.9 <= ratio <= 1.3


def test_regenerate_anchors_today_in_week_6_of_the_16_week_plan(conn_and_demo_id):
    conn, demo_id = conn_and_demo_id
    today = date(2026, 9, 24)
    demo_data.regenerate(conn, demo_id, today)
    rows = conn.execute("SELECT * FROM plan WHERE user_id = ?", (demo_id,)).fetchall()

    weeks = plan_templates._TEMPLATES[demo_data.PLAN_ID]["weeks"]
    assert weeks == 16
    # the plan's first row is the Monday 5 weeks before today's own Monday, so today falls in week 6
    expected_start = plan_templates.monday_of(today) - timedelta(weeks=demo_data.WEEKS_INTO_PLAN - 1)
    assert min(r["date"] for r in rows) == expected_start.isoformat()
    race_day = max(r["date"] for r in rows)
    assert race_day == (expected_start + timedelta(weeks=weeks - 1, days=6)).isoformat()
    # today itself must fall within the plan's date range (i.e. genuinely "in week 6", not before/after it)
    assert min(r["date"] for r in rows) <= today.isoformat() <= race_day


def test_regenerate_wipes_previous_visitors_edits(conn_and_demo_id):
    """A demo visitor's session shouldn't leak into the next one's - regenerate() always rebuilds from scratch."""
    conn, demo_id = conn_and_demo_id
    demo_data.regenerate(conn, demo_id, date(2026, 9, 24))
    conn.execute("INSERT INTO plan (user_id, date, session_type, sport, sport_group, position) "
                "VALUES (?, '2020-01-01', 'Clutter', 'Run', 'run', 999)", (demo_id,))
    demo_data.regenerate(conn, demo_id, date(2026, 9, 24))
    dates = {r["date"] for r in conn.execute("SELECT date FROM plan WHERE user_id = ?", (demo_id,)).fetchall()}
    assert "2020-01-01" not in dates


def test_is_stale(conn_and_demo_id):
    conn, demo_id = conn_and_demo_id
    assert demo_data.is_stale(conn, demo_id, date(2026, 9, 24)) is True
    demo_data.regenerate(conn, demo_id, date(2026, 9, 24))
    assert demo_data.is_stale(conn, demo_id, date(2026, 9, 24)) is False
    assert demo_data.is_stale(conn, demo_id, date(2026, 9, 25)) is True   # a new day
