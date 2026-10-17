"""Fuelling: carb targets for long runs, the product list, logging what was had, and everything that has to
follow from it (export, deletion, the demo account, one account never seeing another's)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import auth, db, demo_data, main, nutrition, users

TODAY = "2026-10-17"
SECRET = "a-long-random-session-secret-value"
PASSWORD = "correct horse battery staple"

BAR = {"name": "Styrkr BAR50", "kind": "bar", "unit": "bar", "carbs_g": 50, "use_in_plan": True}
MIX = {"name": "Styrkr MIX60", "kind": "drink", "unit": "flask", "carbs_g": 60, "sodium_mg": 56, "use_in_plan": True}
GEL = {"name": "Gel", "kind": "gel", "unit": "gel", "carbs_g": 25, "caffeine_mg": 75, "use_in_plan": False}

# four Saturdays of long runs (3h30 each) plus a short Tuesday run that must never get a target
PLAN = ("date,session type,sport,distance,duration\n"
        "2026-10-17,Long run,Run,28,210\n2026-10-20,Easy,Run,8,50\n"
        "2026-10-24,Long run,Run,30,225\n2026-10-31,Long run,Run,32,240\n2026-11-07,Long run,Run,34,255\n")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "fuel.db"))
    monkeypatch.setenv("FITNESS_TODAY", TODAY)
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def goal(**over):
    return {"race_name": "Arc 50", "race_date": "2027-01-16", "race_hours": 6, "target_g_per_hr": 90,
            "current_g_per_hr": 60, "ramp_g_per_hr": 5, **over}


def setup(client, with_plan=True, products=(BAR, MIX), **goal_over):
    if with_plan:
        client.post("/api/plan/import", json={"text": PLAN})
    for p in products:
        assert client.post("/api/fuel/products", json=p).status_code == 200
    r = client.put("/api/fuel/goal", json=goal(**goal_over))
    assert r.status_code == 200, r.text
    return r.json()


def products_by_name(client):
    return {p["name"]: p["id"] for p in client.get("/api/fuel").json()["products"]}


def week_item(client, start="2026-10-12"):
    week = client.get("/api/week", params={"start": start}).json()
    return week, [i for d in week["days"] for i in d["items"] if i["kind"] == "planned" and "fuel" in i]


# ---- which sessions count ----------------------------------------------------------------------

@pytest.mark.parametrize("session_type,group,expected", [
    ("Long run", "run", True), ("LONG RUN", "run", True), ("Ultra long", "run", True), ("Easy", "run", False),
    ("Long ride", "ride", False), (None, "run", False), ("Longest day", "run", True),
])
def test_only_long_runs_are_fuelled(session_type, group, expected):
    assert nutrition.is_long_run(session_type, group) is expected


# ---- the progression rules ---------------------------------------------------------------------

G = {"current_g_per_hr": 60, "target_g_per_hr": 90, "ramp_g_per_hr": 5}
DATES = ["2026-10-17", "2026-10-24", "2026-10-31", "2026-11-07"]


def test_targets_start_one_step_above_where_you_are_and_climb_to_race_rate():
    t = nutrition.progression(G, DATES, {}, "2026-10-01")          # all in the future: assumed to go fine
    assert list(t.values()) == [65, 70, 75, 80]
    far = nutrition.progression(G, DATES * 1 + ["2026-11-14", "2026-11-21", "2026-11-28"], {}, "2026-10-01")
    assert max(far.values()) == 90                                  # capped at the race rate


def test_a_run_logged_fine_steps_up_some_holds_bad_steps_back():
    day = lambda *gut: dict(zip(DATES, gut))
    assert nutrition.progression(G, DATES, day("fine", "fine", "fine"), "2026-12-01") == dict(zip(DATES, [65, 70, 75, 80]))
    assert nutrition.progression(G, DATES, day("fine", "some", "fine"), "2026-12-01") == dict(zip(DATES, [65, 70, 70, 75]))
    assert nutrition.progression(G, DATES, day("fine", "fine", "bad"), "2026-12-01") == dict(zip(DATES, [65, 70, 75, 70]))


def test_a_bad_gut_never_steps_back_below_where_you_started():
    t = nutrition.progression(G, DATES, {"2026-10-17": "bad", "2026-10-24": "bad"}, "2026-12-01")
    assert t["2026-10-24"] == 60 and t["2026-10-31"] == 60          # back to current, not 55


def test_a_past_run_with_no_report_holds_but_a_future_one_is_projected():
    t = nutrition.progression(G, DATES, {}, "2026-10-25")           # the first two are past and unreported
    assert [t[d] for d in DATES[:3]] == [65, 65, 65]                 # no evidence the step was handled: hold
    assert t[DATES[3]] == 70                                          # ...but the road ahead is still shown


def test_current_above_target_just_sits_at_target():
    t = nutrition.progression({**G, "current_g_per_hr": 100}, DATES, {"2026-10-17": "bad"}, "2026-12-01")
    assert set(t.values()) == {90}


# ---- the suggested mix -------------------------------------------------------------------------

P = [{"id": 1, "name": "BAR50", "kind": "bar", "unit": "bar", "carbs_g": 50, "use_in_plan": True},
     {"id": 2, "name": "MIX60", "kind": "drink", "unit": "flask", "carbs_g": 60, "use_in_plan": True},
     {"id": 3, "name": "Gel", "kind": "gel", "unit": "gel", "carbs_g": 25, "use_in_plan": False}]


@pytest.mark.parametrize("grams,text,total", [
    (300, "3 bars + 2½ flasks", 300),        # exact, and the most even split between food and drink
    (130, "1½ bars + 1 flask", 135),         # a little over, but a proper mix beats 2 bars + ½ a flask
    (228, "2 bars + 2 flasks", 220),         # 8 g under beats "1 bar + 3 flasks" (230): that split is lopsided
    (50, "1 bar", 50),                       # a simple target gets a simple answer, not ½ bar + ½ flask
    (60, "1 flask", 60),
    (70, "1½ bars", 75),                     # nothing lands on 70: the nearest it can get
])
def test_mix_lands_near_the_target_in_halves_and_prefers_an_even_food_drink_split(grams, text, total):
    mix = nutrition.suggest_mix(P, grams)
    assert (mix["text"], mix["total_g"]) == (text, total)


def test_mix_never_uses_tracking_only_products_and_copes_with_nothing_to_plan_with():
    assert all(i["name"] != "Gel" for i in nutrition.suggest_mix(P, 200)["items"])
    assert nutrition.suggest_mix([P[2]], 100) is None
    assert nutrition.suggest_mix(P, 0) is None and nutrition.suggest_mix([], 100) is None


@pytest.mark.parametrize("qty,text", [(0.5, "½"), (1, "1"), (1.5, "1½"), (2, "2"), (7.5, "7½")])
def test_quantities_read_naturally(qty, text):
    assert nutrition.fmt_qty(qty) == text


def test_units_are_singular_up_to_one_and_plural_after():
    assert nutrition.describe([{"unit": "bar", "qty": 1}, {"unit": "flask", "qty": 1.5}]) == "1 bar + 1½ flasks"
    assert nutrition.describe([{"unit": "gel", "qty": 0.5}]) == "½ gel"
    assert nutrition.describe([{"unit": "sachet", "qty": 2}, {"unit": "box", "qty": 3}]) == "2 sachets + 3 boxes"


# ---- setting the goal --------------------------------------------------------------------------

def test_goal_roundtrip_and_each_long_run_gets_a_target_and_a_mix(client):
    out = setup(client)
    assert out["goal"]["race_name"] == "Arc 50" and out["goal"]["target_g_per_hr"] == 90
    assert [r["date"] for r in out["runs"]] == ["2026-10-17", "2026-10-24", "2026-10-31", "2026-11-07"]   # not the Tuesday easy run
    first = out["runs"][0]
    assert first["target_g_per_hr"] == 65 and first["planned_minutes"] == 210
    assert first["planned_g"] == round(65 * 3.5) == 228
    assert first["mix"]["total_g"] == 220 and first["mix"]["text"] == "2 bars + 2 flasks"
    assert out["next_target"] == 65
    assert out["race"]["total_g"] == 540 and out["race"]["days_to_go"] == 91 and out["race"]["mix"]["total_g"] == 545


@pytest.mark.parametrize("change,message", [
    ({"race_date": ""}, "race date"),
    ({"race_date": "2026-10-01"}, "in the past"),
    ({"target_g_per_hr": ""}, "needed"),
    ({"target_g_per_hr": 5}, "between"),
    ({"target_g_per_hr": "lots"}, "number"),
    ({"current_g_per_hr": -1}, "between"),
    ({"ramp_g_per_hr": 99}, "between"),
    ({"race_hours": 100}, "between"),
])
def test_bad_goals_are_refused_with_a_message_a_person_can_read(client, change, message):
    r = client.put("/api/fuel/goal", json=goal(**change))
    assert r.status_code == 400 and message in r.json()["detail"]


def test_editing_the_goal_keeps_when_the_ramp_started(client, monkeypatch):
    setup(client)
    monkeypatch.setenv("FITNESS_TODAY", "2026-10-20")
    out = client.put("/api/fuel/goal", json=goal(target_g_per_hr=100)).json()
    assert out["goal"]["start_date"] == TODAY and out["goal"]["target_g_per_hr"] == 100


def test_deleting_the_goal_leaves_products_and_logs(client):
    setup(client)
    ids = products_by_name(client)
    client.put("/api/fuel/log/2026-10-17", json={"items": [{"product_id": ids["Styrkr BAR50"], "qty": 2}], "gut": "fine"})
    out = client.delete("/api/fuel/goal").json()
    assert out["goal"] is None and out["runs"] == [] and len(out["products"]) == 2
    _, items = week_item(client, "2026-10-12")
    assert items[0]["fuel"]["log"]["carbs_g"] == 100 and items[0]["fuel"]["target_g_per_hr"] is None


# ---- products ----------------------------------------------------------------------------------

def test_product_validation_and_defaults(client):
    r = client.post("/api/fuel/products", json={"name": "Salt tab", "kind": "other", "carbs_g": 0, "sodium_mg": 250})
    assert r.status_code == 200
    p = r.json()["products"][0]
    assert p["unit"] == "serving" and p["sodium_mg"] == 250 and p["use_in_plan"] is False and "user_id" not in p
    for bad, msg in [({"name": ""}, "name"), ({"name": "x", "kind": "pizza", "carbs_g": 1}, "Type"),
                     ({"name": "x", "carbs_g": ""}, "needed"), ({"name": "x", "carbs_g": 999}, "between"),
                     ({"name": "x", "carbs_g": 1, "unit": "a very long unit name"}, "short word")]:
        r = client.post("/api/fuel/products", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"], bad


def test_drink_defaults_to_flasks(client):
    out = client.post("/api/fuel/products", json={"name": "Mix", "kind": "drink", "carbs_g": 60}).json()
    assert out["products"][0]["unit"] == "flask"


def test_product_list_is_kept_short(client):
    for n in range(nutrition.MAX_PRODUCTS):
        assert client.post("/api/fuel/products", json={"name": "P%d" % n, "kind": "gel", "carbs_g": 20}).status_code == 200
    r = client.post("/api/fuel/products", json={"name": "One too many", "kind": "gel", "carbs_g": 20})
    assert r.status_code == 400 and "most products" in r.json()["detail"]


def test_only_three_products_can_be_combined_in_suggestions(client):
    for n in range(nutrition.MAX_IN_PLAN):
        assert client.post("/api/fuel/products", json={"name": "P%d" % n, "kind": "gel", "carbs_g": 20, "use_in_plan": True}).status_code == 200
    r = client.post("/api/fuel/products", json={"name": "P9", "kind": "gel", "carbs_g": 20, "use_in_plan": True})
    assert r.status_code == 400 and "untick" in r.json()["detail"]
    assert client.post("/api/fuel/products", json={"name": "P9", "kind": "gel", "carbs_g": 20, "use_in_plan": False}).status_code == 200


def test_product_names_are_unique_per_person_ignoring_case(client):
    client.post("/api/fuel/products", json=BAR)
    r = client.post("/api/fuel/products", json={**BAR, "name": "styrkr bar50"})
    assert r.status_code == 400 and "already have" in r.json()["detail"]


def test_product_edit_and_delete(client):
    out = client.post("/api/fuel/products", json=BAR).json()
    pid = out["products"][0]["id"]
    out = client.put("/api/fuel/products/%d" % pid, json={**BAR, "carbs_g": 45}).json()
    assert out["products"][0]["carbs_g"] == 45
    assert client.put("/api/fuel/products/9999", json=BAR).status_code == 404
    assert client.delete("/api/fuel/products/%d" % pid).json()["products"] == []
    assert client.delete("/api/fuel/products/%d" % pid).status_code == 404


# ---- logging -----------------------------------------------------------------------------------

def log(client, day, items, gut="fine"):
    ids = products_by_name(client)
    body = {"items": [{"product_id": ids[n], "qty": q} for n, q in items], "gut": gut}
    return client.put("/api/fuel/log/" + day, json=body)


def test_logging_a_run_gives_totals_per_hour_and_compares_with_the_target(client):
    setup(client, products=(BAR, MIX, GEL))
    # an actual 3h30 run on the day, so g/h can be worked out
    with db.connect() as conn:
        conn.execute("INSERT INTO activities (id, user_id, date, start_epoch, name, sport_type, sport_group, distance, "
                     "moving_time) VALUES (1, 0, '2026-10-17', 0, 'Long run', 'Run', 'run', 28000, 12600)")
    assert log(client, "2026-10-17", [("Styrkr BAR50", 2), ("Styrkr MIX60", 2.5), ("Gel", 1)], "some").status_code == 200
    week, items = week_item(client)
    lg = items[0]["fuel"]["log"]
    assert lg["carbs_g"] == 100 + 150 + 25 == 275 and lg["text"] == "2 bars + 2½ flasks + 1 gel"
    assert lg["minutes"] == 210 and lg["carbs_g_per_hr"] == 79 and lg["vs_target_g_per_hr"] == 79 - 65
    assert lg["sodium_mg"] == 140 and lg["caffeine_mg"] == 75 and lg["sodium_mg_per_hr"] == 40
    assert lg["gut"] == "some" and lg["gut_label"] == "Some discomfort"
    assert week["fuel"]["has_goal"] and len(week["fuel"]["products"]) == 3


def test_without_a_synced_run_there_is_no_per_hour_figure_yet(client):
    setup(client)
    log(client, "2026-10-17", [("Styrkr BAR50", 1)])
    lg = week_item(client)[1][0]["fuel"]["log"]
    assert lg["carbs_g"] == 50 and lg["carbs_g_per_hr"] is None and lg["minutes"] is None


def test_the_next_target_responds_to_how_the_stomach_coped(client):
    setup(client)
    assert client.get("/api/fuel").json()["runs"][1]["target_g_per_hr"] == 70          # tomorrow's run is projected
    log(client, "2026-10-17", [("Styrkr BAR50", 2)], "bad")
    runs = client.get("/api/fuel").json()["runs"]
    assert [r["target_g_per_hr"] for r in runs] == [65, 60, 65, 70]                     # stepped back, then climbing again
    log(client, "2026-10-17", [("Styrkr BAR50", 2)], "some")
    assert client.get("/api/fuel").json()["runs"][1]["target_g_per_hr"] == 65          # held


def test_editing_a_log_replaces_it_and_clearing_removes_it(client):
    setup(client)
    log(client, "2026-10-17", [("Styrkr BAR50", 3)], "fine")
    log(client, "2026-10-17", [("Styrkr MIX60", 1)], None)
    lg = week_item(client)[1][0]["fuel"]["log"]
    assert lg["text"] == "1 flask" and lg["gut"] is None
    assert client.delete("/api/fuel/log/2026-10-17").status_code == 200
    assert week_item(client)[1][0]["fuel"]["log"] is None


def test_log_history_survives_the_product_being_edited_or_removed(client):
    setup(client)
    log(client, "2026-10-17", [("Styrkr BAR50", 2)])
    pid = products_by_name(client)["Styrkr BAR50"]
    client.put("/api/fuel/products/%d" % pid, json={**BAR, "carbs_g": 10})
    client.delete("/api/fuel/products/%d" % pid)
    lg = week_item(client)[1][0]["fuel"]["log"]
    assert lg["carbs_g"] == 100 and lg["items"][0]["name"] == "Styrkr BAR50"          # as it was on the day


@pytest.mark.parametrize("body,message", [
    ({"items": [{"product_id": 1, "qty": 0.3}]}, "halves"),
    ({"items": [{"product_id": 1, "qty": 99}]}, "halves"),
    ({"items": [{"product_id": 1, "qty": -1}]}, "halves"),
    ({"items": [{"product_id": 1, "qty": 1}, {"product_id": 1, "qty": 1}]}, "only appear once"),
    ({"items": [{"qty": 1}]}, "product and a quantity"),
    ({"items": [], "gut": "terrible"}, "stomach"),
])
def test_bad_logs_are_refused(client, body, message):
    setup(client)
    r = client.put("/api/fuel/log/2026-10-17", json=body)
    assert r.status_code == 400 and message in r.json()["detail"]


def test_cannot_log_the_future_or_a_nonsense_date_or_someone_elses_product(client):
    setup(client)
    assert client.put("/api/fuel/log/2026-10-24", json={"items": [], "gut": "fine"}).status_code == 400
    assert client.put("/api/fuel/log/yesterday", json={"items": []}).status_code == 400
    r = client.put("/api/fuel/log/2026-10-17", json={"items": [{"product_id": 424242, "qty": 1}]})
    assert r.status_code == 400 and "isn't in your list" in r.json()["detail"]


def test_an_empty_log_stores_nothing(client):
    setup(client)
    client.put("/api/fuel/log/2026-10-17", json={"items": [], "gut": None})
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fuel_logs").fetchone()[0] == 0


# ---- how it shows up in the week view ----------------------------------------------------------

def test_only_long_runs_carry_fuel_info_and_future_runs_cannot_be_logged(client):
    setup(client)
    week, items = week_item(client)
    assert [i["date"] for i in items] == ["2026-10-17"]                                   # not the Tuesday easy run
    assert items[0]["fuel"]["can_log"] is True and items[0]["fuel"]["available"] is True
    nxt = week_item(client, "2026-10-19")[1][0]
    assert nxt["fuel"]["can_log"] is False and nxt["fuel"]["target_g_per_hr"] == 70


def test_the_dashboard_week_carries_the_same_fuel_info(client):
    setup(client)
    d = client.get("/api/dashboard").json()
    long_run = [i for day in d["week"]["days"] for i in day["items"] if i.get("fuel")]
    assert long_run and long_run[0]["fuel"]["target_g_per_hr"] == 65


def test_with_no_goal_and_no_products_a_long_run_only_offers_to_set_one_up(client):
    client.post("/api/plan/import", json={"text": PLAN})
    _, items = week_item(client)
    assert items[0]["fuel"]["available"] is False and items[0]["fuel"]["target_g_per_hr"] is None


def test_a_week_with_no_long_run_still_returns_the_product_list(client):
    setup(client)
    week = client.get("/api/week", params={"start": "2026-11-23"}).json()
    assert [p["name"] for p in week["fuel"]["products"]] == ["Styrkr BAR50", "Styrkr MIX60"]


def test_runs_with_no_planned_duration_get_a_rate_but_no_total(client):
    client.post("/api/plan/import", json={"text": "date,session type,sport,distance\n2026-10-24,Long run,Run,30\n"})
    out = setup(client, with_plan=False)
    run = out["runs"][0]
    assert run["target_g_per_hr"] == 65 and run["planned_g"] is None and run["mix"] is None


# ---- privacy: your data stays yours, and goes when you do --------------------------------------

@pytest.fixture()
def two_users(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "two.db"))
    monkeypatch.setenv("FITNESS_TODAY", TODAY)
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with TestClient(main.app, follow_redirects=False) as c:
        with db.connect() as conn:
            users.create(conn, "alex", "friend-password", first_name="A", last_name="X", email="alex@example.com")
        pete = TestClient(main.app, follow_redirects=False)
        alex = TestClient(main.app, follow_redirects=False)
        assert pete.post("/login", data={"username": "pete", "password": PASSWORD}).status_code == 303
        assert alex.post("/login", data={"username": "alex", "password": "friend-password"}).status_code == 303
        yield pete, alex


def test_one_account_never_sees_or_touches_anothers_fuelling(two_users):
    pete, alex = two_users
    setup(pete)
    log(pete, "2026-10-17", [("Styrkr BAR50", 2)])
    other = alex.get("/api/fuel").json()
    assert other["goal"] is None and other["products"] == [] and other["runs"] == []
    pid = products_by_name(pete)["Styrkr BAR50"]
    assert alex.put("/api/fuel/products/%d" % pid, json=BAR).status_code == 404
    assert alex.delete("/api/fuel/products/%d" % pid).status_code == 404
    r = alex.put("/api/fuel/log/2026-10-17", json={"items": [{"product_id": pid, "qty": 1}]})
    assert r.status_code == 400                                                         # not alex's product
    assert pete.get("/api/fuel").json()["products"][0]["name"] == "Styrkr BAR50"


def test_export_includes_fuelling_without_ids_of_other_people(two_users):
    pete, alex = two_users
    setup(pete)
    log(pete, "2026-10-17", [("Styrkr BAR50", 2), ("Styrkr MIX60", 1)], "some")
    data = pete.get("/api/account/export").json()["fuelling"]
    assert data["goal"]["race_name"] == "Arc 50" and "user_id" not in data["goal"]
    assert [p["name"] for p in data["products"]] == ["Styrkr BAR50", "Styrkr MIX60"]
    assert {(r["product_name"], r["qty"], r["gut"]) for r in data["logs"]} == {("Styrkr BAR50", 2.0, "some"), ("Styrkr MIX60", 1.0, "some")}
    assert alex.get("/api/account/export").json()["fuelling"] == {"goal": None, "products": [], "logs": []}


def test_deleting_an_account_removes_its_fuelling_and_only_its_own(two_users):
    pete, alex = two_users
    setup(pete)
    setup(alex, with_plan=False)
    log(alex, "2026-10-17", [("Styrkr BAR50", 1)])
    log(pete, "2026-10-17", [("Styrkr BAR50", 1)])
    assert alex.post("/api/account/delete", json={"password": "friend-password"}).status_code == 200
    with db.connect() as conn:
        left = {t: conn.execute("SELECT COUNT(DISTINCT user_id) FROM %s" % t).fetchone()[0]
                for t in ("fuel_goal", "fuel_products", "fuel_logs", "fuel_log_items")}
    assert left == {"fuel_goal": 1, "fuel_products": 1, "fuel_logs": 1, "fuel_log_items": 1}   # only pete's remain


def test_disconnecting_strava_leaves_fuelling_alone(two_users, monkeypatch):
    import httpx
    from app import strava
    pete, _ = two_users
    setup(pete)
    log(pete, "2026-10-17", [("Styrkr BAR50", 2)])
    monkeypatch.setattr(strava, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    assert pete.post("/api/account/disconnect-strava").status_code == 200
    assert pete.get("/api/fuel").json()["runs"][0]["log"]["carbs_g"] == 100


# ---- the demo account --------------------------------------------------------------------------

def test_demo_account_shows_fuelling_but_cannot_change_it(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "demo.db"))
    monkeypatch.setenv("FITNESS_TODAY", TODAY)
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False) as c:
        assert c.post("/login", data={"username": "demotracker", "password": "demo"}).status_code == 303
        d = c.get("/api/fuel").json()
        assert d["goal"]["race_name"] == "Demo marathon" and len(d["products"]) == 3
        logged = [r for r in d["runs"] if r["log"]]
        assert logged and all(r["date"] < TODAY for r in logged)
        assert any(r["log"]["gut"] == "some" for r in logged)
        assert [r["target_g_per_hr"] for r in d["runs"]][0] == 50.0                       # one step above the 45 g/h start
        assert c.put("/api/fuel/goal", json=goal()).status_code == 403
        assert c.post("/api/fuel/products", json=BAR).status_code == 403
        assert c.put("/api/fuel/products/1", json=BAR).status_code == 403
        assert c.delete("/api/fuel/products/1").status_code == 403
        assert c.delete("/api/fuel/goal").status_code == 403
        assert c.put("/api/fuel/log/2026-10-10", json={"items": []}).status_code == 403
        assert c.delete("/api/fuel/log/2026-10-10").status_code == 403


def test_demo_regeneration_rebuilds_fuelling_without_piling_up(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "regen.db"))
    db.init_db()
    with db.connect() as conn:
        demo = users.create(conn, "demotracker", "demo", is_demo=True)
        for _ in range(2):
            demo_data.regenerate(conn, demo, date(2026, 10, 17))
        assert conn.execute("SELECT COUNT(*) FROM fuel_goal WHERE user_id = ?", (demo,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fuel_products WHERE user_id = ?", (demo,)).fetchone()[0] == 3


# ---- the policy and the page ------------------------------------------------------------------

def test_privacy_policy_mentions_fuelling_data(client):
    text = client.get("/privacy").text
    assert "fuelling" in text.lower() and "carbohydrate" in text.lower()


def test_fuel_page_is_wired_into_the_app_shell(client):
    index = client.get("/static/index.html").text
    assert 'href="#/fuel"' in index and 'id="view-fuel"' in index


def test_a_demo_made_before_fuelling_existed_is_rebuilt_on_the_next_start(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "old.db"))
    db.init_db()
    with db.connect() as conn:
        demo = users.create(conn, "demotracker", "demo", is_demo=True)
        demo_data.regenerate(conn, demo, date(2026, 10, 17))
        assert not demo_data.is_stale(conn, demo, date(2026, 10, 17))
        nutrition.delete_everything(conn, demo)                    # as a demo regenerated by the previous release
        assert demo_data.is_stale(conn, demo, date(2026, 10, 17))  # same day, but missing the new example


def test_numbers_typed_into_the_forms_arrive_as_text_and_are_still_understood(client):
    r = client.post("/api/fuel/products", json={"name": "Carb Power", "kind": "drink", "carbs_g": "60", "sodium_mg": "250.5", "caffeine_mg": ""})
    assert r.status_code == 200 and r.json()["products"][0]["carbs_g"] == 60 and r.json()["products"][0]["sodium_mg"] == 250.5
    bad = client.post("/api/fuel/products", json={"name": "Other", "kind": "drink", "carbs_g": "60g"})
    assert bad.status_code == 400 and "must be a number" in bad.json()["detail"]
    ok = client.put("/api/fuel/goal", json=goal(target_g_per_hr="90", current_g_per_hr="60", ramp_g_per_hr="5", race_hours=""))
    assert ok.status_code == 200 and ok.json()["goal"]["race_hours"] is None


def test_app_files_are_revalidated_so_a_deploy_is_picked_up_straight_away(client):
    for path in ("/static/app.js", "/static/style.css", "/static/index.html"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    assert "cache-control" not in client.get("/static/icons/favicon-32.png").headers   # images can be kept as-is
