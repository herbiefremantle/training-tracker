import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, main, strava


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shhh")
    monkeypatch.setenv("FITNESS_TODAY", "2026-09-26")
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def fake_token_exchange(monkeypatch, seen):
    def handler(request):
        seen.append(request.content.decode())
        return httpx.Response(200, json={
            "access_token": "AT", "refresh_token": "RT", "expires_at": int(time.time()) + 21600,
            "athlete": {"id": 7, "firstname": "Pete", "lastname": "W"}})
    monkeypatch.setattr(strava, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_oauth_login_redirects_to_strava_with_state_and_scope(client):
    r = client.get("/auth/login")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.status_code == 307 and r.headers["location"].startswith(strava.AUTHORIZE_URL)
    assert q["client_id"] == ["12345"] and q["response_type"] == ["code"]
    assert "activity:read_all" in q["scope"][0] and q["state"][0]
    assert q["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    assert "shhh" not in r.headers["location"]          # secret never goes to the browser


def test_oauth_callback_happy_path_stores_tokens(client, monkeypatch):
    seen = []
    fake_token_exchange(monkeypatch, seen)
    state = parse_qs(urlparse(client.get("/auth/login").headers["location"]).query)["state"][0]
    r = client.get("/auth/callback", params={"code": "abc", "state": state, "scope": "read,activity:read_all"})
    assert r.headers["location"] == "/?connected=1"
    assert "grant_type=authorization_code" in seen[0] and "code=abc" in seen[0]
    s = client.get("/api/status").json()
    assert s["connected"] and s["athlete"] == "Pete W"
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM auth").fetchone()
    assert (row["access_token"], row["refresh_token"]) == ("AT", "RT")
    assert "AT" not in client.get("/api/status").text and "RT" not in client.get("/api/status").text


def test_oauth_callback_rejects_bad_state_and_replay(client, monkeypatch):
    fake_token_exchange(monkeypatch, [])
    client.get("/auth/login")
    r = client.get("/auth/callback", params={"code": "abc", "state": "forged", "scope": "read,activity:read_all"})
    assert "auth_error" in r.headers["location"] and not client.get("/api/status").json()["connected"]
    # the failed attempt consumed the state, so even a later "correct" replay is refused
    r = client.get("/auth/callback", params={"code": "abc", "state": "forged", "scope": "read,activity:read_all"})
    assert "auth_error" in r.headers["location"]
    # non-ASCII state must not crash
    assert client.get("/auth/callback", params={"code": "x", "state": "é", "scope": "read"}).status_code == 307


def test_oauth_callback_requires_activity_scope_and_handles_denial(client, monkeypatch):
    fake_token_exchange(monkeypatch, [])
    state = parse_qs(urlparse(client.get("/auth/login").headers["location"]).query)["state"][0]
    r = client.get("/auth/callback", params={"code": "abc", "state": state, "scope": "read"})
    assert "auth_error" in r.headers["location"] and not client.get("/api/status").json()["connected"]
    client.get("/auth/login")
    r = client.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert "cancelled" in r.headers["location"]


def test_sync_requires_connection(client):
    assert client.post("/api/sync").status_code == 400


@pytest.mark.parametrize("host,ok", [
    ("localhost:8000", True), ("127.0.0.1:8000", True), ("[::1]:8000", True),
    ("192.168.1.20:8000", True), ("10.0.0.5:8000", True), ("172.20.3.4", True),      # home / office LAN
    ("Petes-MBP.local:8000", True),                                                   # Bonjour name
    ("8.8.8.8:8000", False), ("172.32.0.1:8000", False),                              # public IPs
    ("evil.example.com", False), ("192.168.1.5.evil.com", False), ("localhost.evil.com", False),
    ("evil.local.example.com", False), ("", False), ("[bad", False)])
def test_host_allowlist(host, ok):
    assert main.host_allowed(host) is ok


def test_foreign_host_header_is_rejected_by_the_server(client):
    assert client.get("/api/status", headers={"host": "evil.example.com"}).status_code == 400
    assert client.get("/api/status", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/api/status", headers={"host": "192.168.1.20:8000"}).status_code == 200


def test_plan_import_modes_and_dry_run(client):
    csv1 = "date,sport,session type,distance,duration\n2026-09-28,Run,Easy,8,50\n2026-09-28,Gym,Core,,30\n2026-09-29,Run,Long,20,150\n"
    r = client.post("/api/plan/import", json={"text": csv1, "dry_run": True}).json()
    assert len(r["rows"]) == 3 and r["saved"] == 0 and client.get("/api/status").json()["plan_count"] == 0
    assert client.post("/api/plan/import", json={"text": csv1}).json()["saved"] == 3

    # re-uploading one date replaces just that date
    csv2 = "date,sport,session type\n2026-09-28,Run,Tempo\n"
    client.post("/api/plan/import", json={"text": csv2})
    sessions = client.get("/api/plan").json()["sessions"]
    assert [(s["date"], s["session_type"]) for s in sessions] == [("2026-09-28", "Tempo"), ("2026-09-29", "Long")]

    # an all-errors upload must not wipe the plan, even in replace_all mode
    bad = client.post("/api/plan/import", json={"text": "date,sport\nnope,Run\n", "mode": "replace_all"}).json()
    assert bad["saved"] == 0 and bad["errors"]
    assert client.get("/api/status").json()["plan_count"] == 2

    client.post("/api/plan/import", json={"text": csv1, "mode": "replace_all"})
    assert client.get("/api/status").json()["plan_count"] == 3
    assert client.delete("/api/plan").json()["deleted"] == 3


def add_activity(aid, day, sport="Run", km=10.0, mins=60, elev=100.0, hr=150.0):
    with db.connect() as conn:
        conn.execute(strava.UPSERT, strava.activity_row({
            "id": aid, "name": "%s %s" % (sport, day), "sport_type": sport, "distance": km * 1000, "moving_time": mins * 60,
            "start_date": day + "T07:00:00Z", "start_date_local": day + "T07:00:00Z", "average_speed": km * 1000 / (mins * 60),
            "max_speed": 4.0, "total_elevation_gain": elev, "average_heartrate": hr}))


def test_dashboard_end_to_end_with_same_day_double_session(client):
    plan = ("date,session type,sport,distance,duration\n"
            "2026-09-24,Tempo,Run,10,60\n2026-09-24,Strength,Gym,,45\n"   # Thu: run + gym planned
            "2026-09-25,Easy,Run,6,40\n"                                   # Fri: nothing done -> missed
            "2026-09-26,Long,Run,20,150\n"                                 # today: not done yet -> pending
            "2026-09-27,Hike,Hike,8,120\n")                                # future
    client.post("/api/plan/import", json={"text": plan})
    add_activity(1, "2026-09-24", "Run", 10, 60)
    add_activity(2, "2026-09-23", "Ride", 30, 60)
    d = client.get("/api/dashboard").json()
    by_day = {day["date"]: [(i["kind"], i["status"], i["sport_group"]) for i in day["items"]] for day in d["week"]["days"]}
    assert by_day["2026-09-24"] == [("planned", "done", "run"), ("planned", "missed", "gym")]   # gym NOT done
    assert by_day["2026-09-25"] == [("planned", "missed", "run")]
    assert by_day["2026-09-26"] == [("planned", "pending", "run")]
    assert by_day["2026-09-27"] == [("planned", "upcoming", "hike")]
    assert by_day["2026-09-23"] == [("extra", "extra", "ride")]
    assert d["week"]["counts"] == {"done": 1, "over": 0, "under": 0, "missed": 2, "pending": 1, "upcoming": 1,
                                   "extra": 1, "planned": 5, "completed": 1}
    assert d["week"]["tolerance_min"] == 10 and d["week"]["is_current"]
    # planned: 10+6+20+8 km, 60+45+40+150+120 min.  done so far: the 10 km run and the 30 km ride (extra), 60 min each
    assert d["week"]["totals"] == {"distance_km": 40.0, "minutes": 120.0, "planned_distance_km": 44.0, "planned_minutes": 415}
    assert [s["date"] for s in d["upcoming"]] == ["2026-09-26", "2026-09-27"]
    assert d["load"]["sources_28d"]["hr_fallback"] == 2 and len(d["load"]["series"]) == 90
    assert {o["value"] for o in d["sport_options"]} >= {"all", "run", "ride"} and d["default_sport"] == "run"


def test_over_and_under_are_flagged_but_count_as_completed(client):
    client.post("/api/plan/import", json={"text": "date,session type,sport,distance,duration\n"
                                                   "2026-09-21,A,Run,,50\n2026-09-22,B,Run,,50\n2026-09-23,C,Run,,50\n"})
    add_activity(1, "2026-09-21", mins=56)     # +6   -> on target
    add_activity(2, "2026-09-22", mins=70)     # +20  -> over
    add_activity(3, "2026-09-23", mins=30)     # -20  -> under
    w = client.get("/api/week?start=2026-09-21").json()
    got = [(i["session_type"], i["status"], i["duration_diff_min"]) for day in w["days"] for i in day["items"]]
    assert got == [("A", "done", 6), ("B", "over", 20), ("C", "under", -20)]
    assert {k: w["counts"][k] for k in ("done", "over", "under", "missed", "completed", "planned")} == \
        {"done": 1, "over": 1, "under": 1, "missed": 0, "completed": 3, "planned": 3}


def test_week_endpoint_navigates_to_any_week(client):
    client.post("/api/plan/import", json={"text": "date,sport\n2026-09-16,Run\n2026-09-29,Run\n"})
    this = client.get("/api/week").json()
    assert (this["start"], this["end"], this["is_current"]) == ("2026-09-21", "2026-09-27", True)
    last = client.get("/api/week?start=2026-09-16").json()          # any day in the week snaps to its Monday
    assert (last["start"], last["is_current"], last["next"], last["prev"]) == ("2026-09-14", False, "2026-09-21", "2026-09-07")
    assert [i["status"] for day in last["days"] for i in day["items"]] == ["missed"]
    nxt = client.get("/api/week?start=" + this["next"]).json()
    assert [i["status"] for day in nxt["days"] for i in day["items"]] == ["upcoming"]
    assert client.get("/api/week?start=nope").status_code == 422


def test_calendar_month_grid_with_statuses(client):
    client.post("/api/plan/import", json={"text": "date,session type,sport,duration\n"
        "2026-09-01,A,Run,60\n2026-09-02,B,Run,60\n2026-09-03,C,Run,60\n2026-09-03,D,Gym,45\n2026-09-29,E,Run,60\n"
        "2026-09-04,Rest,Rest,\n"})
    add_activity(1, "2026-09-01", mins=60)             # on target
    add_activity(2, "2026-09-03", mins=90)             # over
    add_activity(3, "2026-09-10", "Ride", 20, 60)      # extra
    c = client.get("/api/calendar?month=2026-09").json()
    days = {d["date"]: d for d in c["days"]}
    assert c["label"] == "September 2026" and (c["prev"], c["next"]) == ("2026-08", "2026-10") and c["is_current"]
    assert len(c["days"]) % 7 == 0 and c["days"][0]["date"] == "2026-08-31" and not c["days"][0]["in_month"]
    assert c["days"][-1]["date"] == "2026-10-04" and days["2026-09-15"]["in_month"]
    assert [s["status"] for s in days["2026-09-01"]["sessions"]] == ["done"]
    assert [s["status"] for s in days["2026-09-02"]["sessions"]] == ["missed"]
    assert [(s["status"], s["sport"]) for s in days["2026-09-03"]["sessions"]] == [("over", "Run"), ("missed", "Gym")]
    assert days["2026-09-04"]["sessions"] == []        # rest days show nothing
    assert [s["status"] for s in days["2026-09-29"]["sessions"]] == ["upcoming"]
    assert len(days["2026-09-10"]["extras"]) == 1
    assert c["counts"] == {"done": 1, "over": 1, "under": 0, "missed": 2, "extra": 1}
    assert client.get("/api/calendar?month=2026-13").status_code == 422
    assert client.get("/api/calendar").json()["month"] == "2026-09"


def test_explore_drill_down_endpoints(client):
    for i, (day, km, elev) in enumerate([("2026-09-21", 10, 100), ("2026-09-26", 25, 1200), ("2026-08-30", 8, 50)]):
        add_activity(10 + i, day, "Run", km, 60, elev)
    add_activity(20, "2026-09-22", "Ride", 40, 90, 300)

    wk = client.get("/api/explore?scope=week&anchor=2026-09-23&sport=run").json()
    assert (wk["start"], wk["end"], wk["sport"]) == ("2026-09-21", "2026-09-27", "run")
    assert wk["totals"] == {"distance_km": 35.0, "elevation_m": 1300, "hours": 2.0, "count": 2}
    assert len(wk["buckets"]) == 7 and len(wk["activities"]) == 2

    allsp = client.get("/api/explore?scope=week&anchor=2026-09-23&sport=all").json()
    assert allsp["totals"]["count"] == 3 and {a["sport_label"] for a in allsp["activities"]} == {"Run", "Ride"}

    mo = client.get("/api/explore?scope=month&anchor=2026-09-01&sport=run").json()
    assert len(mo["buckets"]) == 30 and mo["totals"]["distance_km"] == 35.0      # the 30 Aug run is last month
    yr = client.get("/api/explore?scope=year&sport=run").json()                  # anchor defaults to today
    assert yr["label"] == "2026" and yr["totals"]["count"] == 3 and yr["totals"]["distance_km"] == 43.0
    assert yr["buckets"][-1]["start"] == "2026-09-21" and yr["has_next"] is False

    assert client.get("/api/explore?scope=decade").status_code == 422
    assert client.get("/api/explore?sport=run;drop").status_code == 422
    assert client.get("/api/explore?scope=week&sport=kayak").json()["totals"]["count"] == 0


def test_plan_import_uses_selected_distance_unit(client):
    client.post("/api/plan/import", json={"text": "date,sport,distance\n2026-09-28,Run,10\n", "distance_unit": "mi"})
    assert round(client.get("/api/plan").json()["sessions"][0]["planned_distance_km"], 2) == 16.09


def test_week_totals_match_the_mid_week_example(client, monkeypatch):
    """5 miles Monday + 5 miles Tuesday against a planned 35-mile week, viewed on the Tuesday."""
    monkeypatch.setenv("FITNESS_TODAY", "2026-09-22")
    plan = ("date,session type,sport,distance,duration\n"
            "2026-09-21,Easy,Run,5,50\n2026-09-22,Easy,Run,5,50\n2026-09-24,Tempo,Run,10,90\n"
            "2026-09-26,Long,Run,15,150\n2026-09-23,Core,Gym,,30\n2026-09-27,Rest,Rest,,\n")
    client.post("/api/plan/import", json={"text": plan, "distance_unit": "mi"})
    add_activity(1, "2026-09-21", "Run", km=5 * 1.609344, mins=48)
    add_activity(2, "2026-09-22", "Run", km=5 * 1.609344, mins=52)
    t = client.get("/api/dashboard").json()["week"]["totals"]
    assert round(t["distance_km"] / 1.609344, 1) == 10.0 and t["minutes"] == 100.0
    assert round(t["planned_distance_km"] / 1.609344, 1) == 35.0 and t["planned_minutes"] == 50 + 50 + 90 + 150 + 30
    # a week with no plan at all: totals are zero, not an error
    empty = client.get("/api/week?start=2026-08-03").json()["totals"]
    assert empty == {"distance_km": 0, "minutes": 0, "planned_distance_km": 0, "planned_minutes": 0}
