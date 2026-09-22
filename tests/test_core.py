import time
from datetime import date, timedelta

import httpx
import pytest

from app import db, matching, metrics, planparse, strava


# ---- plan parsing ---------------------------------------------------------------------------

def test_parse_header_csv_any_order_and_units():
    text = ("Date,Sport,Session Type,Planned Distance,Planned Duration,Notes\n"
            "2026-09-21,Run,Easy,10km,1:00,\"steady, flat\"\n"
            "2026-09-21,Gym,Strength,,45min,legs\n"
            "22/09/2026,Trail run,Long run,6 mi,1h30,\n")
    r = planparse.parse_plan(text)
    assert not r["errors"]
    a, b, c = r["rows"]
    assert (a["sport_group"], a["planned_distance_km"], a["planned_duration_min"]) == ("run", 10.0, 60.0)
    assert a["notes"] == "steady, flat"
    assert (b["sport_group"], b["planned_distance_km"], b["planned_duration_min"]) == ("gym", None, 45.0)
    assert c["date"] == "2026-09-22" and c["planned_distance_km"] == 9.656 and c["planned_duration_min"] == 90.0


def test_parse_tsv_paste_without_header_positional():
    text = "2026-09-21\tEasy\tRun\t8\t50\tnotes here\n2026-09-21\tCore\tGym\t\t30\t\n"
    r = planparse.parse_plan(text)
    assert [x["sport_group"] for x in r["rows"]] == ["run", "gym"]
    assert r["rows"][0]["planned_distance_km"] == 8 and r["rows"][0]["planned_duration_min"] == 50


def test_parse_sport_inferred_from_session_type_and_rest():
    r = planparse.parse_plan("date,session type,sport\n2026-09-21,Long run,\n2026-09-22,Rest,\n")
    assert [x["sport_group"] for x in r["rows"]] == ["run", "rest"]


def test_parse_errors_are_per_row_and_month_first_option():
    text = "date,sport,distance\n2026-09-21,Run,abc\nnot a date,Run,5\n03/13/2026,Run,5\n"
    r = planparse.parse_plan(text, day_first=False)
    assert len(r["errors"]) == 2 and len(r["rows"]) == 1
    assert r["rows"][0]["date"] == "2026-03-13"
    assert planparse.parse_date("03/04/2026", day_first=True) == "2026-04-03"
    assert planparse.parse_date("03/04/2026", day_first=False) == "2026-03-04"


@pytest.mark.parametrize("text,expected", [
    ("90", 90), ("90 min", 90), ("1:30", 90), ("1:30:30", 90.5), ("1h30", 90), ("1h 30m", 90),
    ("1.5h", 90), ("2 hours", 120), ("45'", 45), ("-", None), ("", None)])
def test_duration_formats(text, expected):
    assert planparse.parse_duration_min(text) == expected


def test_unknown_sport_warns():
    r = planparse.parse_plan("date,sport\n2026-09-21,Kayak\n")
    assert r["rows"][0]["sport_group"] == "kayak" and r["warnings"]


# ---- matching -------------------------------------------------------------------------------

def plan(id, d, sport, dist=None, dur=None, pos=0):
    return {"id": id, "date": d, "sport_group": sport, "planned_distance_km": dist,
            "planned_duration_min": dur, "position": pos, "session_type": "", "sport": sport, "notes": ""}


def act(id, d, sport, dist_km=None, mins=None, t=0):
    return {"id": id, "date": d, "sport_group": sport, "sport_type": sport, "name": "a%d" % id,
            "distance": dist_km * 1000 if dist_km else None, "moving_time": mins * 60 if mins else None,
            "total_elevation_gain": 0, "average_heartrate": None, "start_epoch": t}


def status_by_id(res):
    return {s["id"]: s["status"] for s in res["sessions"]}


def test_same_day_different_sports_match_independently():
    # planned run + gym; only the run happened. Gym must be missed, not "done" because 1 activity existed.
    res = matching.match([plan(1, "2026-09-14", "run", 10), plan(2, "2026-09-14", "gym", None, 45)],
                         [act(10, "2026-09-14", "run", 10.2, 60)], "2026-09-20")
    assert status_by_id(res) == {1: "done", 2: "missed"}
    assert res["extras"] == []


def test_activity_of_other_sport_is_extra_not_a_match():
    res = matching.match([plan(1, "2026-09-14", "run", 10)], [act(10, "2026-09-14", "ride", 30, 90)], "2026-09-20")
    assert status_by_id(res) == {1: "missed"}
    assert [e["id"] for e in res["extras"]] == [10]


def test_two_runs_planned_two_done_paired_by_size_not_order():
    plans = [plan(1, "2026-09-14", "run", 5, pos=0), plan(2, "2026-09-14", "run", 20, pos=1)]
    # the long run happened first chronologically
    acts = [act(10, "2026-09-14", "run", 19, 150, t=1), act(11, "2026-09-14", "run", 5.2, 30, t=2)]
    res = matching.match(plans, acts, "2026-09-20")
    by = {s["id"]: s["activity"]["id"] for s in res["sessions"]}
    assert by == {1: 11, 2: 10}


def test_one_activity_cannot_satisfy_two_planned_sessions():
    plans = [plan(1, "2026-09-14", "run", 5, pos=0), plan(2, "2026-09-14", "run", 5, pos=1)]
    res = matching.match(plans, [act(10, "2026-09-14", "run", 5, 30)], "2026-09-20")
    assert sorted(status_by_id(res).values()) == ["done", "missed"]


def test_second_unplanned_run_is_extra():
    res = matching.match([plan(1, "2026-09-14", "run", 5)],
                         [act(10, "2026-09-14", "run", 5, 30, t=1), act(11, "2026-09-14", "run", 3, 20, t=2)],
                         "2026-09-20")
    assert status_by_id(res) == {1: "done"} and [e["id"] for e in res["extras"]] == [11]


def test_pending_upcoming_rest_and_mixed_group_names():
    plans = [plan(1, "2026-09-20", "run", 5), plan(2, "2026-09-21", "run", 5), plan(3, "2026-09-21", "rest")]
    res = matching.match(plans, [], "2026-09-20")
    assert status_by_id(res) == {1: "pending", 2: "upcoming", 3: "rest"}


def test_date_is_local_date():
    res = matching.match([plan(1, "2026-09-14", "run", 5)], [act(10, "2026-09-15", "run", 5, 30)], "2026-09-20")
    assert status_by_id(res) == {1: "missed"}


# ---- load metrics ---------------------------------------------------------------------------

def a(d, suffer=None, hr=None, mins=60, sport="run"):
    return {"id": 0, "date": d.isoformat(), "sport_group": sport, "suffer_score": suffer,
            "average_heartrate": hr, "moving_time": mins * 60, "distance": 10000, "total_elevation_gain": 100,
            "average_speed": 3.0, "max_speed": 4.0, "name": "x", "start_epoch": 0,
            "sport_type": sport.title(), "max_heartrate": None, "workout_type": None}


def test_load_prefers_suffer_score_then_hr_fallback_then_zero():
    assert metrics.activity_load(a(date.today(), suffer=80, hr=150)) == (80.0, "suffer_score")
    load, src = metrics.activity_load(a(date.today(), hr=150, mins=60))
    assert src == "hr_fallback" and load == pytest.approx(90.0)          # 60 * 150/100
    assert metrics.activity_load(a(date.today())) == (0.0, "none")


def test_ratio_and_flags():
    today = date(2026, 9, 20)
    # steady 50/day for 28 days -> ratio 1.0
    steady = [a(today - timedelta(days=i), suffer=50) for i in range(28)]
    s = metrics.load_summary(steady, today)
    assert s["ratio"] == 1.0 and s["flag"] == "ok" and s["avg7"] == 50 and s["avg28"] == 50
    # heavy last week: 7 days x 100, earlier 21 days x 20 -> avg7 100, avg28 = (700+420)/28 = 40 -> 2.5
    spike = [a(today - timedelta(days=i), suffer=100 if i < 7 else 20) for i in range(28)]
    assert metrics.load_summary(spike, today)["flag"] == "high"
    # light last week: ratio 0.2/… -> low
    taper = [a(today - timedelta(days=i), suffer=10 if i < 7 else 60) for i in range(28)]
    assert metrics.load_summary(taper, today)["flag"] == "low"
    assert metrics.load_summary([], today)["ratio"] is None
    assert metrics.risk_flag(1.5) == "ok" and metrics.risk_flag(1.51) == "high"
    assert metrics.risk_flag(0.8) == "ok" and metrics.risk_flag(0.79) == "low"


def test_rolling_series_length_and_values():
    today = date(2026, 9, 20)
    s = metrics.load_summary([a(today, suffer=70)], today)["series"]
    assert len(s) == 90 and s[-1]["date"] == "2026-09-20"
    assert s[-1]["avg7"] == 10.0 and s[-1]["avg28"] == 2.5 and s[-2]["avg7"] == 0


def test_explore_week_month_year_buckets_and_navigation():
    today = date(2026, 9, 26)   # a Saturday
    acts = [a(date(2026, 9, 21), sport="run"), a(date(2026, 9, 26), sport="run"), a(date(2026, 9, 14), sport="run"),
            a(date(2026, 9, 22), sport="ride"), a(date(2026, 1, 1), sport="run"),
            a(date(2025, 12, 30), sport="run")]                       # previous year: must not appear in 2026

    wk = metrics.explore(acts, "week", date(2026, 9, 23), today, {"run"})
    assert (wk["start"], wk["end"], wk["prev"], wk["next"], wk["has_next"]) == \
        ("2026-09-21", "2026-09-27", "2026-09-14", "2026-09-28", False)
    assert len(wk["buckets"]) == 7 and [b["count"] for b in wk["buckets"]] == [1, 0, 0, 0, 0, 1, 0]
    assert wk["totals"] == {"distance_km": 20.0, "elevation_m": 200, "hours": 2.0, "count": 2}
    assert wk["buckets"][5]["partial"] and wk["buckets"][6]["future"] and not wk["buckets"][0]["partial"]
    assert [x["date"] for x in wk["activities"]] == ["2026-09-21", "2026-09-26"]      # ride filtered out

    mo = metrics.explore(acts, "month", date(2026, 9, 10), today, {"run"})
    assert len(mo["buckets"]) == 30 and mo["totals"]["count"] == 3 and mo["totals"]["distance_km"] == 30.0
    assert (mo["prev"], mo["next"], mo["label"]) == ("2026-08-01", "2026-10-01", "September 2026")

    yr = metrics.explore(acts, "year", date(2026, 5, 1), today, {"run"})
    assert yr["label"] == "2026" and yr["totals"]["count"] == 4          # Jan 1, Sep 14, 21, 26 - not Dec 2025
    assert yr["buckets"][0]["start"] == "2026-01-01" and yr["buckets"][0]["end"] == "2026-01-04"   # clipped to the year
    assert yr["buckets"][0]["count"] == 1
    assert yr["buckets"][-1]["start"] == "2026-09-21" and yr["buckets"][-1]["partial"]   # stops at the current week
    assert yr["prev"] == "2025-01-01" and yr["next"] == "2027-01-01" and not yr["has_next"]
    assert yr["buckets"][-1]["avg_speed"] == pytest.approx(10000 / 3600)

    assert metrics.explore(acts, "week", date(2026, 9, 23), today, None)["totals"]["count"] == 3   # all sports


def test_explore_activity_detail_and_load_source():
    today = date(2026, 9, 26)
    r = metrics.explore([a(date(2026, 9, 21), suffer=70, hr=150), a(date(2026, 9, 22), hr=150)], "week", today, today)
    first, second = r["activities"]
    assert (first["load"], first["load_source"]) == (70, "suffer_score")
    assert (second["load"], second["load_source"]) == (90, "hr_fallback")
    assert first["sport_label"] == "Run" and first["distance_km"] == 10.0 and first["duration_min"] == 60.0


# ---- on target / over / under -----------------------------------------------------------------

@pytest.mark.parametrize("planned,actual,status,diff", [
    (50, 56, "done", 6),          # inside the leeway
    (50, 60, "done", 10),         # exactly 10 over is still on target
    (50, 40, "done", -10),        # exactly 10 under is still on target
    (50, 61, "over", 11),
    (50, 70, "over", 20),         # 1h10 vs 50 min
    (50, 110, "over", 60),
    (50, 39, "under", -11),
    (50, 30, "under", -20)])
def test_duration_tolerance(planned, actual, status, diff):
    res = matching.match([plan(1, "2026-09-14", "run", None, planned)],
                         [act(10, "2026-09-14", "run", None, actual)], "2026-09-20")
    s = res["sessions"][0]
    assert (s["status"], s["duration_diff_min"]) == (status, diff)
    assert s["activity"]["id"] == 10 and res["extras"] == []      # off-target is still a completed session


def test_no_planned_duration_cannot_be_judged():
    res = matching.match([plan(1, "2026-09-14", "run", 10, None)], [act(10, "2026-09-14", "run", 3, 20)], "2026-09-20")
    assert (res["sessions"][0]["status"], res["sessions"][0]["duration_diff_min"]) == ("done", None)


def test_tolerance_applies_per_session_when_two_runs_a_day():
    plans = [plan(1, "2026-09-14", "run", 5, 30, pos=0), plan(2, "2026-09-14", "run", 20, 150, pos=1)]
    acts = [act(10, "2026-09-14", "run", 5.1, 33, t=1), act(11, "2026-09-14", "run", 18, 190, t=2)]
    by = {s["id"]: (s["status"], s["duration_diff_min"]) for s in matching.match(plans, acts, "2026-09-20")["sessions"]}
    assert by == {1: ("done", 3), 2: ("over", 40)}


# ---- Strava sync + tokens -------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "s")
    db.init_db()
    with db.connect() as c:
        yield c


def strava_activity(id, start, **kw):
    base = {"id": id, "name": "Run %d" % id, "sport_type": "Run", "distance": 10000.0, "moving_time": 3600,
            "start_date": start, "start_date_local": start.replace("T0", "T1"), "average_speed": 2.8,
            "max_speed": 4.5, "total_elevation_gain": 120.0, "average_heartrate": 150.0,
            "max_heartrate": 175.0, "workout_type": 0}
    base.update(kw)
    return base


class FakeStrava:
    def __init__(self, activities, details=None, token_expires_in=3600):
        self.activities, self.details = activities, details or {}
        self.refreshes, self.token_expires_in, self.calls = 0, token_expires_in, []

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(url)
        if url.startswith(strava.TOKEN_URL):
            body = dict(x.split("=") for x in request.content.decode().split("&"))
            if body["grant_type"] == "refresh_token":
                self.refreshes += 1
                assert body["refresh_token"] == ("refresh-%d" % self.refreshes if self.refreshes > 1 else "refresh-0")
            tok = {"access_token": "access-%d" % (self.refreshes + 1),
                   "refresh_token": "refresh-%d" % (self.refreshes + 1),
                   "expires_at": int(time.time()) + self.token_expires_in}
            if body["grant_type"] == "authorization_code":   # real refresh responses carry no athlete
                tok["athlete"] = {"id": 42, "firstname": "Pete", "lastname": "W"}
            return httpx.Response(200, json=tok)
        assert request.headers["authorization"].startswith("Bearer access-")
        if "/athlete/activities" in url:
            page = int(request.url.params["page"])
            after = int(request.url.params["after"])
            items = [x for x in self.activities if strava._epoch(x["start_date"]) > after]
            return httpx.Response(200, json=items[(page - 1) * 200: page * 200])
        aid = int(url.rsplit("/", 1)[1])
        return httpx.Response(200, json=self.details.get(aid, {"id": aid}))


UID = db.LOCAL_USER_ID   # these tests run for a single account; which id doesn't matter


def connect_with(conn, fake, uid=UID):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    conn.execute("INSERT INTO strava_auth VALUES (?, 42, 'Pete', 'access-1', 'refresh-0', ?, 'read,activity:read_all')",
                 (uid, int(time.time()) + 3600))
    return http


def test_sync_stores_fields_and_local_date(conn):
    fake = FakeStrava([strava_activity(1, "2026-09-14T06:30:00Z", start_date_local="2026-09-14T22:30:00Z",
                                       suffer_score=77.0, workout_type=2)])
    http = connect_with(conn, fake)
    res = strava.sync(conn, http, UID)
    assert res["new"] == 1 and res["full_history"]
    r = conn.execute("SELECT * FROM activities WHERE id = 1 AND user_id = ?", (UID,)).fetchone()
    assert r["date"] == "2026-09-14" and r["sport_group"] == "run" and r["suffer_score"] == 77
    assert (r["max_speed"], r["total_elevation_gain"], r["max_heartrate"], r["workout_type"]) == (4.5, 120, 175, 2)


def test_sync_is_scoped_to_its_own_account(conn):
    other = UID + 1
    fake = FakeStrava([strava_activity(1, "2026-09-14T06:30:00Z")])
    http = connect_with(conn, fake, uid=other)
    with pytest.raises(strava.StravaError, match="Not connected"):
        strava.sync(conn, http, UID)          # UID has no strava_auth row, `other` does
    strava.sync(conn, http, other)
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE user_id = ?", (UID,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE user_id = ?", (other,)).fetchone()[0] == 1


def test_sync_incremental_upsert_and_delete_reconcile(conn):
    now = time.time()
    iso = lambda days: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days * 86400))
    fake = FakeStrava([strava_activity(1, iso(40)), strava_activity(2, iso(5)), strava_activity(3, iso(2))])
    http = connect_with(conn, fake)
    strava.sync(conn, http, UID)
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 3

    fake.activities = [strava_activity(1, iso(40)), strava_activity(3, iso(2), name="Renamed"),
                       strava_activity(4, iso(1))]      # #2 deleted on Strava, #4 new
    res = strava.sync(conn, http, UID)
    assert res["new"] == 1 and res["removed"] == 1 and not res["full_history"]
    ids = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM activities")}
    assert set(ids) == {1, 3, 4} and ids[3] == "Renamed"   # old (#1, outside window) untouched


def test_sync_paginates(conn):
    base = int(time.time()) - 400 * 86400
    acts = [strava_activity(i, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + i * 3600)),
                            average_heartrate=None) for i in range(1, 451)]
    strava.sync(conn, connect_with(conn, FakeStrava(acts)), UID)
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 450


def test_expired_token_is_refreshed_and_rotated_refresh_token_saved(conn):
    fake = FakeStrava([strava_activity(1, "2026-09-14T06:30:00Z", average_heartrate=None)])
    http = connect_with(conn, fake)
    conn.execute("UPDATE strava_auth SET expires_at = ?", (int(time.time()) - 10,))
    strava.sync(conn, http, UID)
    row = conn.execute("SELECT * FROM strava_auth WHERE user_id = ?", (UID,)).fetchone()
    assert fake.refreshes == 1 and row["refresh_token"] == "refresh-2" and row["access_token"] == "access-2"
    assert row["athlete_name"] == "Pete" and row["athlete_id"] == 42 and row["scope"] == "read,activity:read_all"  # not wiped by refresh


def test_suffer_score_backfilled_from_detail_and_not_wiped_by_resync(conn):
    now = time.time()
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 2 * 86400))
    fake = FakeStrava([strava_activity(1, start)], details={1: {"id": 1, "suffer_score": 91}})
    http = connect_with(conn, fake)
    res = strava.sync(conn, http, UID)
    assert res["suffer_lookups"] == 1
    assert conn.execute("SELECT suffer_score FROM activities").fetchone()[0] == 91
    strava.sync(conn, http, UID)   # list has no suffer_score; must keep 91 and not re-query
    assert conn.execute("SELECT suffer_score FROM activities").fetchone()[0] == 91
    assert sum(1 for c in fake.calls if c.endswith("/activities/1")) == 1


def test_rate_limit_and_revoked_errors(conn):
    def handler(request):
        return httpx.Response(429)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    conn.execute("INSERT INTO strava_auth VALUES (?, 42, 'P', 'a', 'r', ?, 's')", (UID, int(time.time()) + 3600))
    with pytest.raises(strava.StravaError, match="rate limit"):
        strava.sync(conn, http, UID)


def test_unquoted_comma_in_trailing_notes_is_kept():
    r = planparse.parse_plan("date,sport,notes\n2026-09-21,Run,hilly, keep it steady\n")
    assert r["rows"][0]["notes"] == "hilly, keep it steady"


def test_distance_unit_default_and_header_override():
    def km(text, unit):
        return planparse.parse_plan(text, True, unit)["rows"][0]["planned_distance_km"]
    assert km("date,sport,distance\n2026-09-21,Run,10\n", "mi") == pytest.approx(16.093)
    assert km("date,sport,distance\n2026-09-21,Run,10km\n", "mi") == 10.0          # explicit unit wins
    assert km("date,sport,distance (km)\n2026-09-21,Run,10\n", "mi") == 10.0       # so does the header
    assert km("date,sport,distance (mi)\n2026-09-21,Run,10\n", "km") == pytest.approx(16.093)
