"""Ready-made training plans (app/plan_templates.py) and the /api/plan-templates endpoints."""
import csv
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import main, plan_templates


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("FITNESS_TODAY", "2026-09-26")   # a Saturday
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


# ---- the template data itself, independent of the API -------------------------------------

def test_every_session_type_in_the_csv_is_mapped_to_a_sport():
    """Guards against the CSV growing a new session_type that SESSION_TYPE_SPORT doesn't know about -
    unmapped rows would silently become import errors (empty sport) instead of failing this test."""
    with plan_templates.DATA_PATH.open(newline="") as f:
        types = {r["session_type"] for r in csv.DictReader(f)}
    assert types == set(plan_templates.SESSION_TYPE_SPORT)


def test_ten_plans_load_with_the_documented_shape():
    plans = plan_templates._TEMPLATES
    assert len(plans) == 10
    weeks = {pid: p["weeks"] for pid, p in plans.items()}
    assert weeks == {
        "10k_beg": 8, "10k_int": 8,
        "half_beg": 12, "half_int": 10,
        "marathon_beg": 16, "marathon_int": 16,
        "50k_beg": 20, "50k_int": 16,
        "100k_beg": 24, "100k_int": 20,
    }
    for pid, p in plans.items():
        assert len(p["rows"]) == p["weeks"] * 7, pid
        assert p["race_day_offset"] == 6, pid   # every race lands on a Sunday


def test_list_templates_is_ordered_by_race_distance_then_level():
    order = [(p["race"], p["level"]) for p in plan_templates.list_templates(date(2026, 9, 26))]
    assert order == [
        ("10K", "Beginner"), ("10K", "Intermediate"),
        ("Half Marathon", "Beginner"), ("Half Marathon", "Intermediate"),
        ("Marathon", "Beginner"), ("Marathon", "Intermediate"),
        ("50K", "Beginner"), ("50K", "Intermediate"),
        ("100K", "Beginner"), ("100K", "Intermediate"),
    ]


@pytest.mark.parametrize("today,expected_monday", [
    ("2026-09-28", "2026-09-28"),   # a Monday: starts today, not a week later
    ("2026-09-26", "2026-09-28"),   # a Saturday: the coming Monday
    ("2026-09-27", "2026-09-28"),   # a Sunday: the coming Monday
])
def test_default_start_monday(today, expected_monday):
    assert plan_templates.default_start_monday(date.fromisoformat(today)).isoformat() == expected_monday


def test_build_rows_snaps_a_non_monday_start_date_to_that_weeks_monday():
    rows = plan_templates.build_rows("10k_beg", date(2026, 9, 30), user_id=0)   # a Wednesday
    assert rows[0]["date"] == "2026-09-28"   # that week's Monday


@pytest.mark.parametrize("plan_id", list(plan_templates._TEMPLATES))
def test_every_template_round_trips_with_no_unknown_or_unmatched_sports(plan_id):
    """The real regression this feature had to avoid: a session_type whose sport_group comes back
    empty (an import error) or an unrecognised-sport warning, for every single one of the 10 plans."""
    rows = plan_templates.build_rows(plan_id, date(2026, 9, 28), user_id=7)
    assert rows and all(r["sport_group"] for r in rows)
    assert all(r["user_id"] == 7 for r in rows)
    assert rows[0]["date"] == "2026-09-28"
    assert rows[-1]["session_type"] == "RACE DAY"
    # dates strictly increase and land on the documented weekday for each row's day_offset
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
    assert len(rows) == len({r["date"] for r in rows})   # one session per day - no double-booking


def test_optional_cross_training_rest_days_are_never_missed_but_a_real_cross_training_session_is_gym():
    # beginner plans use the optional "Cross-training / rest"; intermediate plans the mandatory "Cross-training"
    beg = plan_templates.build_rows("10k_beg", date(2026, 9, 28), user_id=0)
    intr = plan_templates.build_rows("10k_int", date(2026, 9, 28), user_id=0)
    assert {r["sport_group"] for r in beg if r["session_type"] == "Cross-training / rest"} == {"rest"}
    assert {r["sport_group"] for r in intr if r["session_type"] == "Cross-training"} == {"gym"}


def test_unknown_plan_id_raises_keyerror():
    with pytest.raises(KeyError):
        plan_templates.build_rows("does-not-exist", date(2026, 9, 28), user_id=0)


# ---- the API --------------------------------------------------------------------------------

def test_list_endpoint_returns_the_disclaimer_and_all_plans(client):
    r = client.get("/api/plan-templates").json()
    assert "personalised coaching" in r["disclaimer"]
    assert len(r["plans"]) == 10
    first = r["plans"][0]
    assert first["plan_id"] == "10k_beg"
    assert first["default_start_date"] == "2026-09-28"   # today (fixture) is a Saturday
    assert first["default_race_date"] == "2026-11-22"


def test_apply_saves_the_plan_and_reports_saved_count(client):
    r = client.post("/api/plan-templates/10k_beg/apply", json={}).json()
    assert r["saved"] == 56
    assert r["errors"] == [] and r["warnings"] == []
    assert r["start_date"] == "2026-09-28"
    plan = client.get("/api/plan").json()["sessions"]
    assert len(plan) == 56


def test_apply_dry_run_does_not_save(client):
    r = client.post("/api/plan-templates/10k_beg/apply", json={"dry_run": True}).json()
    assert r["saved"] == 0 and len(r["rows"]) == 56
    assert client.get("/api/plan").json()["sessions"] == []


def test_apply_replace_all_wipes_an_existing_plan_first(client):
    client.post("/api/plan/import", json={"text": "date,sport\n2020-01-01,Run\n"})
    client.post("/api/plan-templates/10k_beg/apply", json={"mode": "replace_all"})
    dates = {s["date"] for s in client.get("/api/plan").json()["sessions"]}
    assert "2020-01-01" not in dates
    assert len(dates) == 56


def test_apply_replace_dates_only_touches_overlapping_dates(client):
    client.post("/api/plan/import", json={"text": "date,sport\n2020-01-01,Run\n"})
    client.post("/api/plan-templates/10k_beg/apply", json={"mode": "replace_dates"})
    dates = {s["date"] for s in client.get("/api/plan").json()["sessions"]}
    assert "2020-01-01" in dates          # untouched - no date overlap with the template
    assert len(dates) == 57


def test_apply_honours_a_custom_start_date(client):
    r = client.post("/api/plan-templates/10k_beg/apply", json={"start_date": "2026-10-05"}).json()
    assert r["start_date"] == "2026-10-05"   # already a Monday


def test_apply_rejects_a_bad_start_date(client):
    assert client.post("/api/plan-templates/10k_beg/apply", json={"start_date": "not-a-date"}).status_code == 400


def test_apply_rejects_an_unknown_plan_id(client):
    assert client.post("/api/plan-templates/nope/apply", json={}).status_code == 404


def test_applied_plan_matches_against_strava_activities_like_any_other_plan(client):
    """The optional cross-training/rest day must never show as Missed, same as a manually-typed
    Rest day would - matching.py gives every rest-group session status "rest" unconditionally."""
    client.post("/api/plan-templates/10k_beg/apply", json={"mode": "replace_all"})
    sessions = client.get("/api/plan").json()["sessions"]
    rest_days = [s for s in sessions if s["session_type"] == "Cross-training / rest"]
    assert rest_days and all(s["status"] == "rest" for s in rest_days)
