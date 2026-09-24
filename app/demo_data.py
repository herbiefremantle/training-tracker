"""Self-updating data for the shared demo login (see app/users.py's DEMO_ACCOUNT bootstrap): a training plan
and activity history that always looks current, because it's rebuilt relative to "today" rather than stored
against fixed calendar dates. regenerate() is called at startup and on a daily schedule from app/main.py.

Deliberately isolated: regenerate() takes a user_id but re-checks it's really the demo account before touching
anything (see the assertion below) - a bug elsewhere in the caller can't turn this into a real user's data loss.
"""
import random
from datetime import timedelta

from . import plan_templates, strava, users
from .db import get_meta, set_meta

PLAN_ID = "marathon_beg"        # 16 weeks - "week 6 of a 16-week marathon plan" is the brief
WEEKS_INTO_PLAN = 6             # "today" always falls inside this week of the plan
ACTIVITY_WINDOW_DAYS = 28       # "past sessions (last ~4 weeks)"
MISSED_COUNT = 2                # "a couple of Missed" - the rest of the window's runs are "Done"
SUFFER_SCORE_BASE = 30          # suffer_score = SUFFER_SCORE_BASE + mins * SUFFER_SCORE_SLOPE. A shallow slope is
SUFFER_SCORE_SLOPE = 0.35       # deliberate: a marathon plan's own mileage ramp already means the most recent
                                 # week is bigger than three weeks ago, so scoring load tightly to duration (a
                                 # steep slope) pushed the 7-day/28-day ratio well past "on track" in testing.
                                 # This pair keeps it in the 0.9-1.3 band - see tests/test_demo_data.py, which
                                 # asserts the actual ratio, not just this comment.
DEMO_ACTIVITY_ID_BASE = 5_000_000_000   # comfortably outside any real Strava activity id, so the two can't collide

_RNG_SEED = 20260101   # fixed: the *dates* are already relative to today, so there's no need for the small
                       # jitter in pace/HR/elevation to differ from one regeneration to the next


def regenerate(conn, user_id, today):
    """Wipe this account's plan and activities and rebuild both relative to `today`. Raises ValueError if
    `user_id` isn't actually flagged is_demo - the one thing standing between this function and being able to
    wipe a real account's training history, so it's checked here, not just by the caller."""
    account = users.get_by_id(conn, user_id)
    if account is None or not account["is_demo"]:
        raise ValueError("demo_data.regenerate() refused: user_id %r is not the demo account." % (user_id,))
    rng = random.Random(_RNG_SEED)

    monday = plan_templates.monday_of(today) - timedelta(weeks=WEEKS_INTO_PLAN - 1)
    rows = plan_templates.build_rows(PLAN_ID, monday, user_id)

    conn.execute("DELETE FROM plan WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM activities WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO plan (user_id, date, session_type, sport, sport_group, planned_distance_km, "
        "planned_duration_min, notes, position) VALUES (:user_id, :date, :session_type, :sport, :sport_group, "
        ":planned_distance_km, :planned_duration_min, :notes, :position)", rows)

    today_iso = today.isoformat()
    window_start = (today - timedelta(days=ACTIVITY_WINDOW_DAYS - 1)).isoformat()
    # Every past run session gets a "Done" activity, not just the ones in the recent window - otherwise weeks
    # 1-2 of the plan (before the window, but still in the past) would show as a pile of unexplained "Missed"
    # sessions nobody asked for. The couple of *deliberate* misses are chosen only from "Run - easy" sessions
    # (never "Long run" - skipping the single biggest session in the window swings the load ratio far more
    # than a short easy run does) strictly before today (today itself would read as "pending", not "missed")
    # within the recent window, so they're the ones a visitor looking at "the last few weeks" actually sees.
    past_run_sessions = [r for r in rows if r["sport_group"] == "run" and r["date"] <= today_iso]
    missable = [i for i, r in enumerate(past_run_sessions)
               if window_start <= r["date"] < today_iso and r["session_type"] == "Run - easy"]
    skip = set(rng.sample(missable, min(MISSED_COUNT, len(missable))))

    aid = DEMO_ACTIVITY_ID_BASE
    activities = []
    for i, r in enumerate(past_run_sessions):
        if i in skip:
            continue   # left unmatched on purpose - shows up as "Missed"
        aid += 1
        planned_km, planned_min = r["planned_distance_km"] or 5.0, r["planned_duration_min"] or 40
        km = round(planned_km * rng.uniform(0.97, 1.03), 2)
        mins = round(planned_min * rng.uniform(0.96, 1.04))   # stays inside the ±10 min "Done" tolerance
        activities.append(_activity(aid, r["date"], "Run", km, mins, rng))

    # one unplanned "Extra" session on the most recent rest day, so all three statuses are visible at once
    recent_rest = next((r for r in reversed(rows) if r["sport_group"] == "rest" and r["date"] <= today_iso), None)
    if recent_rest:
        aid += 1
        activities.append(_activity(aid, recent_rest["date"], "Ride", 22.0, 55, rng))

    for a in activities:
        conn.execute(strava.UPSERT, strava.activity_row(a, user_id))

    # a fake but "connected" Strava link, so the dashboard never shows a "Connect your Strava" prompt for a
    # visitor who obviously can't - app/main.py separately blocks /api/sync and /auth/* for this account, since
    # these tokens don't work against the real Strava API.
    conn.execute(
        "INSERT INTO strava_auth (user_id, athlete_id, athlete_name, access_token, refresh_token, expires_at, "
        "scope) VALUES (?, 0, 'Demo Athlete', 'demo', 'demo', 0, 'read,activity:read_all') "
        "ON CONFLICT(user_id) DO UPDATE SET athlete_name = excluded.athlete_name", (user_id,))
    set_meta(conn, user_id, "last_sync", today_iso + "T07:00:00")
    set_meta(conn, user_id, "demo_regenerated_on", today_iso)


def is_stale(conn, user_id, today):
    return get_meta(conn, user_id, "demo_regenerated_on") != today.isoformat()


def _activity(aid, iso_date, sport_type, km, mins, rng):
    start = "%sT%02d:%02d:00Z" % (iso_date, rng.choice([6, 7, 17, 18]), rng.randint(0, 59))
    avg_hr = rng.randint(138, 152)
    speed = (km * 1000) / (mins * 60) if mins else 0
    return {
        "id": aid, "name": "Demo %s" % sport_type, "sport_type": sport_type,
        "distance": km * 1000.0, "moving_time": mins * 60,
        "start_date": start, "start_date_local": start,
        "average_heartrate": avg_hr, "max_heartrate": avg_hr + rng.randint(15, 25),
        "average_speed": round(speed, 2), "max_speed": round(speed * rng.uniform(1.3, 1.6), 2) if speed else 0,
        "total_elevation_gain": round(km * rng.uniform(8, 14)),
        "suffer_score": round(SUFFER_SCORE_BASE + mins * SUFFER_SCORE_SLOPE),
        "workout_type": 0,
    }
