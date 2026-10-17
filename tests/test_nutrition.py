"""Fuelling: carb targets for long runs, the product list, logging what was had, and everything that has to
follow from it (export, deletion, the demo account, one account never seeing another's)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import httpx

from app import auth, db, demo_data, hydration, main, nutrition, users, weather

TODAY = "2026-10-17"
SECRET = "a-long-random-session-secret-value"
PASSWORD = "correct horse battery staple"

BAR = {"name": "Styrkr BAR50", "kind": "bar", "unit": "bar", "carbs_g": 50, "use_in_plan": True}
MIX = {"name": "Styrkr MIX60", "kind": "drink", "unit": "flask", "carbs_g": 60, "sodium_mg": 56, "fluid_ml": 500, "use_in_plan": True}
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


def test_items_that_share_a_unit_are_named_so_the_text_is_not_a_riddle():
    items = [{"unit": "bar", "qty": 2, "name": "Styrkr BAR50"}, {"unit": "bar", "qty": 1.5, "name": "Rice cake"},
             {"unit": "flask", "qty": 1, "name": "MIX60"}]
    assert nutrition.describe(items) == "2 × Styrkr BAR50 + 1½ × Rice cake + 1 flask"


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
                     ({"name": "x", "kind": "bar", "carbs_g": ""}, "needed"), ({"name": "x", "carbs_g": 999}, "between"),
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
    ({"items": [{"preset": "styrkr_bar50", "product_id": 1, "qty": 1}]}, "product and a quantity"),
    ({"items": [{"preset": "no_such_thing", "qty": 1}]}, "isn't available"),
    ({"items": [{"new_product": "a bar", "qty": 1}]}, "product and a quantity"),
    ({"items": [{"product_id": "abc", "qty": 1}]}, "product and a quantity"),
    ({"items": ["bar"]}, "product and a quantity"),
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
    assert items[0]["fuel"]["can_log"] is True
    nxt = week_item(client, "2026-10-19")[1][0]
    assert nxt["fuel"]["can_log"] is False and nxt["fuel"]["target_g_per_hr"] == 70


def test_the_dashboard_week_carries_the_same_fuel_info(client):
    setup(client)
    d = client.get("/api/dashboard").json()
    long_run = [i for day in d["week"]["days"] for i in day["items"] if i.get("fuel")]
    assert long_run and long_run[0]["fuel"]["target_g_per_hr"] == 65


def test_with_no_goal_and_no_products_a_long_run_can_still_be_logged_against(client):
    client.post("/api/plan/import", json={"text": PLAN})
    week, items = week_item(client)
    assert items[0]["fuel"]["can_log"] is True and items[0]["fuel"]["target_g_per_hr"] is None
    assert week["fuel"]["has_goal"] is False and week["fuel"]["products"] == []


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
    assert alex.get("/api/account/export").json()["fuelling"] == {"goal": None, "hydration_settings": None, "products": [], "logs": []}


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
        assert d["goal"]["race_name"] == "Demo marathon" and len(d["products"]) == 5
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
        assert conn.execute("SELECT COUNT(*) FROM fuel_products WHERE user_id = ?", (demo,)).fetchone()[0] == 5


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


# ---- logging a bar, gel or anything else straight from the log, without visiting the product list ----------

def item_names(client):
    return {p["name"]: p for p in client.get("/api/fuel").json()["products"]}


def test_picking_a_quick_add_product_in_the_log_adds_it_to_your_list_and_logs_it(client):
    setup(client, products=())
    r = client.put("/api/fuel/log/2026-10-17", json={"items": [
        {"preset": "styrkr_bar50", "qty": 2}, {"preset": "gel", "qty": 1.5}], "gut": "fine"})
    assert r.status_code == 200, r.text
    mine = item_names(client)
    assert set(mine) == {"Styrkr BAR50", "Energy gel"} and mine["Energy gel"]["use_in_plan"] is False
    lg = week_item(client)[1][0]["fuel"]["log"]
    assert lg["text"] == "2 bars + 1½ gels" and lg["carbs_g"] == 138     # 137.5 g, shown to the nearest gram


def test_picking_the_same_quick_add_product_again_reuses_it(client):
    setup(client, products=())
    for _ in range(2):
        assert client.put("/api/fuel/log/2026-10-17", json={"items": [{"preset": "styrkr_bar50", "qty": 1}]}).status_code == 200
    assert len(client.get("/api/fuel").json()["products"]) == 1


def test_a_new_product_typed_into_the_log_is_saved_and_logged(client):
    setup(client, products=(BAR,))
    body = {"items": [{"new_product": {"name": "Maurten gel", "kind": "gel", "carbs_g": "25", "caffeine_mg": 100}, "qty": 1}]}
    assert client.put("/api/fuel/log/2026-10-17", json=body).status_code == 200
    p = item_names(client)["Maurten gel"]
    assert p["carbs_g"] == 25 and p["caffeine_mg"] == 100 and p["use_in_plan"] is False      # gels stay tracking-only
    lg = week_item(client)[1][0]["fuel"]["log"]
    assert lg["caffeine_mg"] == 100 and lg["text"] == "1 gel"


def test_a_new_bar_or_drink_joins_suggested_mixes_while_there_is_room(client):
    setup(client, products=(BAR,))
    client.put("/api/fuel/log/2026-10-17", json={"items": [
        {"new_product": {"name": "Rice cake", "kind": "bar", "carbs_g": 30}, "qty": 1}]})
    assert item_names(client)["Rice cake"]["use_in_plan"] is True


def test_a_bad_new_product_fails_the_whole_save_and_leaves_nothing_behind(client):
    setup(client, products=(BAR,))
    r = client.put("/api/fuel/log/2026-10-17", json={"gut": "fine", "items": [
        {"preset": "gel", "qty": 1}, {"new_product": {"name": "", "kind": "bar", "carbs_g": 30}, "qty": 1}]})
    assert r.status_code == 400 and "name" in r.json()["detail"]
    assert set(item_names(client)) == {"Styrkr BAR50"}                                    # the gel wasn't half-added
    assert week_item(client)[1][0]["fuel"]["log"] is None


def test_a_full_product_list_stops_logging_a_new_one_with_a_clear_message(client):
    for n in range(nutrition.MAX_PRODUCTS):
        client.post("/api/fuel/products", json={"name": "P%d" % n, "kind": "gel", "carbs_g": 20})
    r = client.put("/api/fuel/log/2026-10-17", json={"items": [{"preset": "styrkr_bar50", "qty": 1}]})
    assert r.status_code == 400 and "most products" in r.json()["detail"]


def test_the_same_product_in_two_rows_is_added_up(client):
    setup(client)
    pid = products_by_name(client)["Styrkr BAR50"]
    r = client.put("/api/fuel/log/2026-10-17", json={"items": [
        {"product_id": pid, "qty": 1}, {"product_id": pid, "qty": 1.5}]})
    assert r.status_code == 200
    assert week_item(client)[1][0]["fuel"]["log"]["text"] == "2½ bars"
    too_many = client.put("/api/fuel/log/2026-10-17", json={"items": [{"product_id": pid, "qty": 8}, {"product_id": pid, "qty": 8}]})
    assert too_many.status_code == 400 and "halves" in too_many.json()["detail"]


def test_the_week_view_offers_the_quick_add_list_to_the_log_dropdown(client):
    setup(client, products=())
    week = client.get("/api/week", params={"start": "2026-10-12"}).json()
    assert [p["key"] for p in week["fuel"]["presets"]] == ["styrkr_bar50", "styrkr_mix60", "gel", "electrolyte", "water"]


# ============================ colour coding, over-doing it, water, hydration and the weather ============================

def run_with_log(client, items, gut, minutes=210, day="2026-10-17"):
    """A long run on `day` that really took `minutes`, with the given items logged against it."""
    with db.connect() as conn:
        conn.execute("DELETE FROM activities")
        conn.execute("INSERT INTO activities (id, user_id, date, start_epoch, name, sport_type, sport_group, distance, "
                     "moving_time) VALUES (1, 0, ?, 0, 'Long run', 'Run', 'run', 28000, ?)", (day, minutes * 60))
    assert log(client, day, items, gut).status_code == 200
    return week_item(client)[1][0]["fuel"]


def test_carbs_are_green_within_ten_either_side_and_red_outside(client):
    setup(client)                                            # first target is 65 g/h over 3h30 = 227 g
    for items, status, delta in [([("Styrkr BAR50", 4), ("Styrkr MIX60", 0.5)], "ok", 230 / 3.5 - 65),
                                 ([("Styrkr BAR50", 1)], "off", 50 / 3.5 - 65),                  # way under
                                 ([("Styrkr BAR50", 6)], "off", 300 / 3.5 - 65)]:                # way over
        lg = run_with_log(client, items, "fine")["log"]
        assert lg["carb_status"] == status and lg["vs_target_g_per_hr"] == round(delta)
    edge = run_with_log(client, [("Styrkr BAR50", 4), ("Styrkr MIX60", 0.5)], "fine", minutes=210)["log"]
    assert abs(edge["vs_target_g_per_hr"]) <= nutrition.CARB_TOLERANCE_G_PER_HR and edge["carb_status"] == "ok"


def test_no_colour_when_the_run_has_not_synced_so_there_is_no_rate(client):
    setup(client)
    log(client, "2026-10-17", [("Styrkr BAR50", 2)])
    lg = week_item(client)[1][0]["fuel"]["log"]
    assert lg["carb_status"] is None and lg["alerts"] == []


def test_well_above_target_with_a_good_stomach_is_a_good_sign_not_a_scare(client):
    setup(client)
    lg = run_with_log(client, [("Styrkr BAR50", 6)], "fine")["log"]                 # 86 g/h against 65
    assert [a["level"] for a in lg["alerts"]] == ["info"] and "good sign" in lg["alerts"][0]["text"]


def test_well_above_target_with_a_bad_stomach_is_a_warning(client):
    setup(client)
    for gut in ("some", "bad"):
        lg = run_with_log(client, [("Styrkr BAR50", 6)], gut)["log"]
        assert lg["alerts"][0]["level"] == "warn" and "too much, too soon" in lg["alerts"][0]["text"]


def test_well_above_target_with_no_stomach_report_asks_for_one(client):
    setup(client)
    lg = run_with_log(client, [("Styrkr BAR50", 6)], None)["log"]
    assert "say how your stomach coped" in lg["alerts"][0]["text"].lower()


def test_taking_more_than_about_120_g_an_hour_always_warns_even_if_it_felt_fine(client):
    setup(client, target_g_per_hr=150, current_g_per_hr=100)
    lg = run_with_log(client, [("Styrkr BAR50", 9)], "fine")["log"]                 # 450 g in 3.5 h = 129 g/h
    warns = [a for a in lg["alerts"] if a["level"] == "warn"]
    assert warns and "129 g/h" in warns[0]["text"] and "stomach trouble" in warns[0]["text"]


def test_under_target_gets_no_scolding_just_the_red_number(client):
    setup(client)
    lg = run_with_log(client, [("Styrkr BAR50", 1)], "fine")["log"]
    assert lg["carb_status"] == "off" and lg["alerts"] == []


# ---- the targets respond to what was actually taken ------------------------------------------------

def test_a_fine_run_well_over_target_builds_from_what_was_managed_but_only_two_steps_ahead():
    last = {"2026-10-17": {"gut": "fine", "per_hr": 200}}
    t = nutrition.progression(G, DATES, last, "2026-12-01")
    assert t[DATES[1]] == 65 + 2 * 5 + 5                      # credited +10 at most, then the normal step
    mild = nutrition.progression(G, DATES, {"2026-10-17": {"gut": "fine", "per_hr": 70}}, "2026-12-01")
    assert mild[DATES[1]] == 70                               # within 10 either side counts as having hit it


def test_a_fine_run_well_under_target_repeats_the_target():
    t = nutrition.progression(G, DATES, {"2026-10-17": {"gut": "fine", "per_hr": 40}}, "2026-12-01")
    assert t[DATES[1]] == 65


def test_an_unknown_rate_falls_back_to_the_stomach_report_alone():
    t = nutrition.progression(G, DATES, {"2026-10-17": {"gut": "fine", "per_hr": None}}, "2026-12-01")
    assert t[DATES[1]] == 70


def test_the_api_applies_it_end_to_end(client):
    setup(client)
    run_with_log(client, [("Styrkr BAR50", 6)], "fine")                  # 86 g/h vs 65 target, fine
    assert client.get("/api/fuel").json()["runs"][1]["target_g_per_hr"] == 80
    run_with_log(client, [("Styrkr BAR50", 1)], "fine")                  # 14 g/h: didn't manage it
    assert client.get("/api/fuel").json()["runs"][1]["target_g_per_hr"] == 65


# ---- water and fluid ------------------------------------------------------------------------------

WATER = {"name": "Water", "kind": "water", "unit": "flask", "fluid_ml": 500}


def test_water_needs_a_size_and_never_joins_a_carb_mix(client):
    assert client.post("/api/fuel/products", json={"name": "Water", "kind": "water"}).status_code == 400
    r = client.post("/api/fuel/products", json={**WATER, "use_in_plan": True})
    assert r.status_code == 200
    p = r.json()["products"][0]
    assert p["carbs_g"] == 0 and p["fluid_ml"] == 500 and p["use_in_plan"] is False


def test_fluid_adds_up_from_water_and_drinks_and_gives_a_rate(client):
    setup(client, products=(BAR, MIX))
    client.post("/api/fuel/products", json=WATER)
    lg = run_with_log(client, [("Styrkr MIX60", 2), ("Water", 3)], "fine")["log"]
    assert lg["fluid_ml"] == 2500 and lg["fluid_ml_per_hr"] == round(2500 / 3.5) and lg["carbs_g"] == 120


def test_old_logs_without_fluid_still_read(client):
    setup(client)
    with db.connect() as conn:
        conn.execute("INSERT INTO fuel_logs (user_id, date, gut) VALUES (0, '2026-10-17', 'fine')")
        conn.execute("INSERT INTO fuel_log_items (user_id, date, product_name, kind, unit, carbs_g, sodium_mg, caffeine_mg, qty) "
                     "VALUES (0, '2026-10-17', 'Old bar', 'bar', 'bar', 50, 0, 0, 1)")
    assert week_item(client)[1][0]["fuel"]["log"]["fluid_ml"] == 0


def test_drinking_far_too_much_warns_about_hyponatraemia(client):
    setup(client, products=(BAR,))
    client.post("/api/fuel/products", json=WATER)
    lg = run_with_log(client, [("Water", 8)], "fine")["log"]                        # 4 L over 3.5 h = 1143 ml/h
    warn = [a for a in lg["alerts"] if "hyponatraemia" in a["text"]]
    assert warn and warn[0]["level"] == "warn" and "1143 ml/h" in warn[0]["text"]


# ---- hydration settings, place search, and the forecast ---------------------------------------------

def forecast_json(temps):
    """Hourly forecast for 15 Oct - 1 Nov: `temps` maps a date to its temperature; others are 14 C."""
    times, t, rh = [], [], []
    for offset in range(18):
        d = date(2026, 10, 15 + offset) if offset <= 16 else date(2026, 11, 1)
        for hour in range(24):
            times.append("%sT%02d:00" % (d.isoformat(), hour))
            t.append(temps.get(d.isoformat(), 14.0))
            rh.append(70)
    return {"hourly": {"time": times, "temperature_2m": t, "relative_humidity_2m": rh}}


@pytest.fixture()
def fake_weather(monkeypatch):
    weather._cache.clear()
    calls = {"forecast": 0, "geocode": 0, "temps": {"2026-10-24": 8.0, "2026-10-31": 31.0, "2026-10-17": 22.0}, "fail": False}

    def handler(request):
        url = str(request.url)
        if calls["fail"]:
            raise httpx.ConnectError("offline")
        if url.startswith(weather.GEOCODE_URL):
            calls["geocode"] += 1
            return httpx.Response(200, json={"results": [
                {"name": "Newquay", "admin1": "England", "country": "United Kingdom", "latitude": 50.4123, "longitude": -5.0743},
                {"name": "Newquay", "admin1": "Wales", "country": "United Kingdom", "latitude": 52.2, "longitude": -4.36}]})
        calls["forecast"] += 1
        calls["last_url"] = url
        return httpx.Response(200, json=forecast_json(calls["temps"]))

    monkeypatch.setattr(weather, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    yield calls
    weather._cache.clear()


PREFS = {"place": "Newquay, England", "lat": 50.4123, "lon": -5.0743, "start_hour": 8, "fluid_ml_per_hr": 500, "sodium_mg_per_hr": 400}


def test_place_search_returns_choices_and_sends_only_the_typed_name(client, fake_weather):
    r = client.get("/api/fuel/places", params={"q": "newq"})
    assert r.status_code == 200 and r.json()["places"][0] == {"label": "Newquay, England, United Kingdom", "lat": 50.41, "lon": -5.07}
    assert fake_weather["geocode"] == 1


def test_place_search_errors_are_readable(client, fake_weather):
    assert client.get("/api/fuel/places", params={"q": "n"}).status_code == 502
    fake_weather["fail"] = True
    r = client.get("/api/fuel/places", params={"q": "newquay"})
    assert r.status_code == 502 and "Couldn't reach" in r.json()["detail"]


def test_prefs_roundtrip_and_validation(client):
    r = client.put("/api/fuel/prefs", json=PREFS)
    assert r.status_code == 200 and r.json()["prefs"]["lat"] == 50.41 and r.json()["prefs"]["place"] == "Newquay, England"
    for bad, msg in [({"start_hour": 30}, "between"), ({"fluid_ml_per_hr": 5}, "between"), ({"sodium_mg_per_hr": "lots"}, "number"),
                     ({"race_temp_c": 99}, "between")]:
        r = client.put("/api/fuel/prefs", json={**PREFS, **bad})
        assert r.status_code == 400 and msg in r.json()["detail"], bad
    cleared = client.put("/api/fuel/prefs", json={**PREFS, "place": ""}).json()["prefs"]
    assert cleared["lat"] is None and cleared["place"] is None                       # no place, no location
    assert client.delete("/api/fuel/prefs").json()["prefs"] is None


def test_with_no_settings_drink_and_salt_are_still_in_the_plan_at_the_usual_rates_and_nothing_is_fetched(client, fake_weather):
    setup(client)
    f = week_item(client)[1][0]["fuel"]
    assert f["conditions"] is None and f["weather_state"] == "no_location" and fake_weather["forecast"] == 0
    a = f["advice"]
    assert (a["band"], a["fluid_ml_per_hr"], a["sodium_mg_per_hr"], a["fluid_total_ml"]) == ("usual", 500, 400, 1750)


def test_the_forecast_scales_fluid_and_sodium_to_the_temperature(client, fake_weather):
    setup(client)
    client.put("/api/fuel/prefs", json=PREFS)
    for start, day, temp, fluid, sodium, band in [("2026-10-12", "2026-10-17", 22.0, 600, 500, "warm"),
                                                  ("2026-10-19", "2026-10-24", 8.0, 400, 300, "cold"),
                                                  ("2026-10-26", "2026-10-31", 31.0, 800, 650, "very hot")]:
        f = [i for i in week_item(client, start)[1] if i["date"] == day][0]["fuel"]
        assert f["conditions"]["temp_c"] == temp and f["conditions"]["source"] == "forecast"
        assert (f["advice"]["fluid_ml_per_hr"], f["advice"]["sodium_mg_per_hr"], f["advice"]["band"]) == (fluid, sodium, band), day
    warm = week_item(client, "2026-10-12")[1][0]["fuel"]["advice"]
    assert warm["fluid_total_ml"] == 2100 and warm["sodium_total_mg"] == 1750        # 3h30 at 600 ml and 500 mg an hour


def test_one_forecast_request_covers_a_whole_page_of_runs(client, fake_weather):
    setup(client)
    client.put("/api/fuel/prefs", json=PREFS)
    client.get("/api/fuel")
    client.get("/api/fuel")
    assert fake_weather["forecast"] == 1 and "latitude=50.41" in fake_weather["last_url"] and "longitude=-5.07" in fake_weather["last_url"]
    assert "past_days=2" in fake_weather["last_url"] and "timezone=auto" in fake_weather["last_url"]


def test_runs_beyond_the_forecast_get_the_usual_rates_not_a_guess(client, fake_weather):
    setup(client)
    client.put("/api/fuel/prefs", json=PREFS)
    far = [r for r in client.get("/api/fuel").json()["runs"] if r["date"] == "2026-11-07"][0]
    assert far["conditions"] is None and far["advice"]["band"] == "usual" and far["advice"]["fluid_ml_per_hr"] == 500


def test_a_failed_forecast_just_means_no_weather(client, fake_weather):
    setup(client)
    client.put("/api/fuel/prefs", json=PREFS)
    weather._cache.clear()                    # forget the forecast fetched while saving, so the next look has to ask again
    fake_weather["fail"] = True
    out = client.get("/api/fuel")
    assert out.status_code == 200
    run = out.json()["runs"][0]
    assert run["conditions"] is None and run["advice"]["band"] == "usual"


def test_race_day_uses_the_temperature_you_entered_until_a_forecast_reaches_it(client, fake_weather):
    setup(client)
    client.put("/api/fuel/prefs", json={**PREFS, "race_temp_c": 6})
    race = client.get("/api/fuel").json()["race"]
    assert race["conditions"] is None and race["advice"]["temp_c"] == 6 and race["advice"]["source"] == "manual"
    assert race["advice"]["band"] == "cold" and race["advice"]["fluid_total_ml"] == 2400      # 6 h at 400 ml


def test_a_hot_run_with_too_little_to_drink_warns_and_colours_the_fluid_red(client, fake_weather):
    setup(client, products=(BAR,))
    client.post("/api/fuel/products", json=WATER)
    fake_weather["temps"]["2026-10-17"] = 31.0
    client.put("/api/fuel/prefs", json=PREFS)
    fuel = run_with_log(client, [("Water", 2)], "fine")                                        # 1000 ml over 3.5 h = 286 ml/h
    assert fuel["advice"]["fluid_ml_per_hr"] == 800
    assert fuel["log"]["fluid_status"] == "off" and any("dehydration" in a["text"] for a in fuel["log"]["alerts"])


def test_fluid_and_sodium_are_ok_inside_the_tolerance(client, fake_weather):
    setup(client, products=(BAR,))
    client.post("/api/fuel/products", json=WATER)
    client.post("/api/fuel/products", json={"name": "Salt cap", "kind": "other", "sodium_mg": 250})
    client.put("/api/fuel/prefs", json=PREFS)
    fuel = run_with_log(client, [("Water", 4), ("Salt cap", 6)], "fine")      # 2000 ml, 1500 mg over 3.5 h = 571 ml/h, 429 mg/h
    assert fuel["advice"]["fluid_ml_per_hr"] == 600 and fuel["log"]["fluid_status"] == "ok"
    assert fuel["advice"]["sodium_mg_per_hr"] == 500 and fuel["log"]["sodium_status"] == "ok"


def test_the_demo_login_cannot_search_places_or_change_hydration_settings(tmp_path, monkeypatch, fake_weather):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "demo2.db"))
    monkeypatch.setenv("FITNESS_TODAY", TODAY)
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False) as c:
        c.post("/login", data={"username": "demotracker", "password": "demo"})
        assert c.get("/api/fuel/places", params={"q": "newquay"}).status_code == 403
        assert c.put("/api/fuel/prefs", json=PREFS).status_code == 403
        assert c.delete("/api/fuel/prefs").status_code == 403
        assert fake_weather["geocode"] == 0
        d = c.get("/api/fuel").json()
        assert d["prefs"]["lat"] is None and fake_weather["forecast"] == 0              # the demo never fetches weather
        assert any(r["log"] and r["log"]["fluid_ml"] > 0 for r in d["runs"])             # but it shows water on its logs


def test_hydration_settings_travel_with_export_and_leave_with_the_account(two_users, fake_weather):
    pete, alex = two_users
    setup(alex, with_plan=False)
    alex.put("/api/fuel/prefs", json=PREFS)
    pete.put("/api/fuel/prefs", json={**PREFS, "place": "Elsewhere"})
    assert alex.get("/api/account/export").json()["fuelling"]["hydration_settings"]["place"] == "Newquay, England"
    assert alex.post("/api/account/delete", json={"password": "friend-password"}).status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT place FROM fuel_prefs").fetchall()[0]["place"] == "Elsewhere"
        assert conn.execute("SELECT COUNT(*) FROM fuel_prefs").fetchone()[0] == 1


def test_privacy_policy_names_the_weather_service_and_what_it_gets(client):
    text = client.get("/privacy").text
    assert "Open-Meteo" in text and "rounded coordinates" in text and "CC BY 4.0" in text


def test_adding_fluid_columns_to_an_existing_database_keeps_data_and_backfills_the_mix60_flask(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    monkeypatch.setenv("FITNESS_DB", str(path))
    db.init_db()
    with sqlite3.connect(path) as raw:                       # a database as the first fuelling release left it
        raw.execute("DROP TABLE fuel_products")
        raw.execute("DROP TABLE fuel_log_items")
        raw.execute("CREATE TABLE fuel_products (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, "
                    "kind TEXT NOT NULL, unit TEXT NOT NULL, carbs_g REAL NOT NULL, sodium_mg REAL NOT NULL DEFAULT 0, "
                    "caffeine_mg REAL NOT NULL DEFAULT 0, use_in_plan INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL DEFAULT 0)")
        raw.execute("CREATE TABLE fuel_log_items (user_id INTEGER NOT NULL, date TEXT NOT NULL, product_name TEXT NOT NULL, "
                    "kind TEXT NOT NULL, unit TEXT NOT NULL, carbs_g REAL NOT NULL, sodium_mg REAL NOT NULL, "
                    "caffeine_mg REAL NOT NULL, qty REAL NOT NULL, PRIMARY KEY (user_id, date, product_name))")
        raw.execute("INSERT INTO fuel_products (user_id, name, kind, unit, carbs_g) VALUES (0, 'Styrkr MIX60', 'drink', 'flask', 60)")
        raw.execute("INSERT INTO fuel_products (user_id, name, kind, unit, carbs_g) VALUES (0, 'Gel', 'gel', 'gel', 25)")
        raw.execute("INSERT INTO fuel_log_items VALUES (0, '2026-10-01', 'Gel', 'gel', 'gel', 25, 0, 0, 1)")
    db.init_db()
    db.init_db()                                             # and again: it only happens once
    with db.connect() as conn:
        fluids = {r["name"]: r["fluid_ml"] for r in conn.execute("SELECT name, fluid_ml FROM fuel_products")}
        assert fluids == {"Styrkr MIX60": 500, "Gel": 0}
        assert conn.execute("SELECT fluid_ml FROM fuel_log_items").fetchone()[0] == 0


def test_low_sodium_gets_a_plain_note_but_only_when_there_is_advice_to_compare_with(client, fake_weather):
    setup(client, products=(BAR,))
    client.put("/api/fuel/prefs", json=PREFS)
    fuel = run_with_log(client, [("Styrkr BAR50", 3)], "fine")            # bars carry no sodium: 0 mg/h vs about 500
    assert fuel["log"]["sodium_status"] is None or fuel["log"]["sodium_status"] == "off"
    client.delete("/api/fuel/prefs")
    none = run_with_log(client, [("Styrkr BAR50", 3)], "fine")["log"]
    assert not any("Sodium" in a["text"] for a in none["alerts"])


def test_sodium_below_advice_is_flagged_once_sodium_was_logged(client, fake_weather):
    setup(client, products=(BAR, MIX))
    client.put("/api/fuel/prefs", json=PREFS)
    fuel = run_with_log(client, [("Styrkr MIX60", 4)], "fine")           # 4 x 56 mg over 3.5 h = 64 mg/h vs about 500
    assert fuel["log"]["sodium_status"] == "off"
    assert any("Sodium was well below" in a["text"] for a in fuel["log"]["alerts"])


# ---- drink in all, salt, and weather as part of the plan -------------------------------------------

ELECTROLYTE = {"name": "Electrolyte tablet", "kind": "other", "unit": "tablet", "sodium_mg": 250}


def test_the_plan_says_how_much_to_drink_in_all_and_how_much_of_it_is_water(client):
    setup(client)
    client.post("/api/fuel/products", json=WATER)
    a = client.get("/api/fuel").json()["runs"][0]["advice"]
    d = a["drink"]
    assert d["total_ml"] == 1750 and d["from_mix_ml"] == 1000                  # 2 flasks of drink mix = 1 L of it
    assert d["water_ml"] == 750 and d["water_text"] == "1½ flasks"             # the rest, in the person's own flasks


def test_with_no_water_product_the_remainder_is_still_given_in_ml(client):
    setup(client)
    d = client.get("/api/fuel").json()["runs"][0]["advice"]["drink"]
    assert d["water_ml"] == 750 and d["water_text"] is None


def test_when_the_drinks_already_cover_it_no_extra_water_is_asked_for(client):
    setup(client, products=(BAR, {**MIX, "fluid_ml": 1200}))
    d = client.get("/api/fuel").json()["runs"][0]["advice"]["drink"]
    assert d["from_mix_ml"] >= d["total_ml"] and d["water_ml"] == 0


def test_the_plan_says_how_much_salt_to_add_after_what_the_mix_already_holds(client):
    setup(client)
    client.post("/api/fuel/products", json=ELECTROLYTE)
    salt = client.get("/api/fuel").json()["runs"][0]["advice"]["salt"]
    assert salt["verdict"] == "yes" and salt["total_mg"] == 1400 and salt["from_mix_mg"] == 112   # 2 flasks x 56 mg
    assert salt["add_mg"] == 1290 and salt["product_text"] == "5½ tablets"
    assert "3h30" in salt["why"] and "usual" not in salt["why"]


def test_without_an_electrolyte_product_it_still_says_how_many_mg_to_add(client):
    setup(client)
    salt = client.get("/api/fuel").json()["runs"][0]["advice"]["salt"]
    assert salt["add_mg"] == 1290 and salt["product_text"] is None


@pytest.mark.parametrize("minutes,band,verdict", [
    (45, "hot", "no"), (59, "usual", "no"),
    (90, "mild", "optional"), (90, "cold", "optional"), (90, "usual", "optional"),
    (90, "warm", "yes"), (90, "hot", "yes"), (125, "mild", "yes"), (215, "usual", "yes"),
    (None, "hot", "yes"), (None, "mild", "optional"),
])
def test_salt_verdict(minutes, band, verdict):
    assert hydration.salt_verdict(minutes, band)[0] == verdict


def test_a_run_too_short_to_need_salt_says_so_and_asks_for_none(client):
    client.post("/api/plan/import", json={"text": "date,session type,sport,distance,duration\n2026-10-17,Long run,Run,8,50\n"})
    setup(client, with_plan=False)
    salt = client.get("/api/fuel").json()["runs"][0]["advice"]["salt"]
    assert salt["verdict"] == "no" and salt["add_mg"] == 0 and "under an hour" in salt["why"]


def test_the_race_day_plan_carries_drink_and_salt_too(client):
    setup(client)
    race = client.get("/api/fuel").json()["race"]
    assert race["advice"]["drink"]["total_ml"] == 3000 and race["advice"]["salt"]["verdict"] == "yes"   # 6 h at the usual 500 ml/h
    assert race["weather_state"] == "no_location"


def test_weather_state_says_why_there_is_no_forecast(client, fake_weather):
    setup(client)
    assert client.get("/api/fuel").json()["runs"][0]["weather_state"] == "no_location"
    client.put("/api/fuel/prefs", json=PREFS)
    runs = {r["date"]: r["weather_state"] for r in client.get("/api/fuel").json()["runs"]}
    assert runs["2026-10-17"] == "forecast" and runs["2026-11-07"] == "out_of_range"
    weather._cache.clear()
    fake_weather["fail"] = True
    assert client.get("/api/fuel").json()["runs"][0]["weather_state"] == "unavailable"


def test_picking_a_place_on_a_run_keeps_the_other_hydration_settings(client, fake_weather):
    setup(client)
    client.put("/api/fuel/prefs", json={**PREFS, "place": "", "fluid_ml_per_hr": 650, "sodium_mg_per_hr": 700, "race_temp_c": 7})
    out = client.put("/api/fuel/place", json={"place": "Newquay, England", "lat": 50.41, "lon": -5.07}).json()
    assert out["prefs"]["place"] == "Newquay, England" and out["prefs"]["fluid_ml_per_hr"] == 650
    assert out["prefs"]["sodium_mg_per_hr"] == 700 and out["prefs"]["race_temp_c"] == 7


def test_picking_a_place_with_no_settings_yet_starts_from_the_defaults(client, fake_weather):
    out = client.put("/api/fuel/place", json={"place": "Newquay, England", "lat": 50.41, "lon": -5.07}).json()
    assert out["prefs"]["fluid_ml_per_hr"] == 500 and out["prefs"]["sodium_mg_per_hr"] == 400 and out["prefs"]["start_hour"] == 8


def test_place_endpoint_rejects_nonsense_and_the_demo(client, tmp_path, monkeypatch):
    assert client.put("/api/fuel/place", json={"place": "x", "lat": 999, "lon": 0}).status_code == 422
    assert client.put("/api/fuel/place", json={"place": "x"}).status_code == 422
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "demo3.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False) as c:
        c.post("/login", data={"username": "demotracker", "password": "demo"})
        assert c.put("/api/fuel/place", json={"place": "x", "lat": 50.4, "lon": -5.0}).status_code == 403


def test_not_logging_water_or_salt_is_not_scolded(client):
    setup(client)
    lg = run_with_log(client, [("Styrkr BAR50", 4)], "fine")["log"]
    assert lg["fluid_status"] is None and lg["sodium_status"] is None and lg["fluid_ml_per_hr"] is None
    assert not any("odium" in a["text"] or "drank" in a["text"] for a in lg["alerts"])
