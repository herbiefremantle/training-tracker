"""Carb-fuelling for long runs: train the gut to take the carbs per hour a race will need.

A person sets a race (date, the carbs-per-hour they want to reach, what they take comfortably now) and keeps a
short list of their own products. Every long run in their plan then gets a target that steps up towards the race
rate, shown as grams per hour, a total for the planned time and a mix of their products ("2 bars + 2½ flasks").
After the run they log what they actually had, plus how their stomach coped, and the next target responds to that.

Deliberately plain rules, not clever guessing - what a person's gut tolerates is theirs to judge:
  * the first long run is `ramp` g/h above the rate they take comfortably now (capped at the race target);
  * after a run logged "fine" the next target steps up by `ramp`; after "some discomfort" it holds; after a "bad
    gut" it steps back by `ramp` (never below where they started); a past run with no gut report holds, because
    there's no evidence the last step was handled;
  * runs still in the future are projected as if each goes fine, so the whole road to race day is visible.

Only runs whose session type says "long" get a target. None of it comes from Strava (so disconnecting Strava
leaves it alone) and none of it is medical or dietitian advice - the numbers are the person's own.
"""
import itertools
import math
from datetime import date as _date

MAX_PRODUCTS = 8       # "not too many": a short list is the point
MAX_IN_PLAN = 3        # products the planner may combine into a suggested mix (the rest are tracking-only)
MAX_QTY = 12           # most of one product that can be logged in one go
MIX_BALANCE_WEIGHT = 0.15      # how much an uneven split between food and drink matters, per gram, next to being
MIX_EXTRA_PRODUCT_G = 5        # a gram off target; and what carrying one more kind of product is worth, in grams
KINDS = ("bar", "drink", "gel", "other")
GUT = ("fine", "some", "bad")
GUT_LABELS = {
    "fine": "No problems",
    "some": "Some discomfort",
    "bad": "Bad gut",
}
LIQUID_KINDS = ("drink",)

# One-tap starting points the Fuelling page offers. The numbers are what each maker publishes for a single
# serving, but they're only a start - each product is editable, and people are told to check their own packet.
PRESETS = [
    {"key": "styrkr_bar50", "name": "Styrkr BAR50", "kind": "bar", "unit": "bar", "carbs_g": 50,
     "sodium_mg": 0, "caffeine_mg": 0, "use_in_plan": True,
     "note": "50 g carbs per bar. Sodium isn't filled in - add it from the packet if you track it."},
    {"key": "styrkr_mix60", "name": "Styrkr MIX60", "kind": "drink", "unit": "flask", "carbs_g": 60,
     "sodium_mg": 56, "caffeine_mg": 0, "use_in_plan": True,
     "note": "One 65 g sachet (60 g carbs) in a 500 ml flask. The sodium figure comes from retailer listings - "
             "check your sachet."},
    {"key": "gel", "name": "Energy gel", "kind": "gel", "unit": "gel", "carbs_g": 25,
     "sodium_mg": 0, "caffeine_mg": 0, "use_in_plan": False,
     "note": "A typical gel. Kept out of suggested mixes - it's here so you can log one if you take it. Check the packet."},
]


# ---- which sessions get a target -------------------------------------------------------------

def is_long_run(session_type, sport_group):
    return sport_group == "run" and "long" in (session_type or "").lower()


# ---- the goal --------------------------------------------------------------------------------

def get_goal(conn, user_id):
    row = conn.execute("SELECT * FROM fuel_goal WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def _number(value, label, lo, hi, allow_none=False):
    if value is None or value == "":
        if allow_none:
            return None
        raise ValueError("%s is needed." % label)
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number." % label)
    if not math.isfinite(n) or n < lo or n > hi:
        raise ValueError("%s must be between %g and %g." % (label, lo, hi))
    return n


def validate_goal(data, today):
    """Cleaned goal dict, or ValueError with a message fit to show the person."""
    try:
        race_date = _date.fromisoformat(str(data.get("race_date") or ""))
    except ValueError:
        raise ValueError("Pick the race date.")
    if race_date < today:
        raise ValueError("The race date is in the past.")
    return {
        "race_name": str(data.get("race_name") or "").strip()[:80],
        "race_date": race_date.isoformat(),
        "race_hours": _number(data.get("race_hours"), "Expected race time", 0.25, 48, allow_none=True),
        "target_g_per_hr": _number(data.get("target_g_per_hr"), "Race carbs per hour", 10, 150),
        "current_g_per_hr": _number(data.get("current_g_per_hr"), "Current carbs per hour", 0, 150),
        "ramp_g_per_hr": _number(data.get("ramp_g_per_hr", 5), "Step per long run", 0, 30),
    }


def save_goal(conn, user_id, clean, today):
    """The ramp starts the day the goal is first saved; editing the goal later keeps that start."""
    existing = get_goal(conn, user_id)
    start = existing["start_date"] if existing else today.isoformat()
    conn.execute(
        "INSERT INTO fuel_goal (user_id, race_name, race_date, race_hours, target_g_per_hr, current_g_per_hr, "
        "ramp_g_per_hr, start_date) VALUES (:user_id, :race_name, :race_date, :race_hours, :target_g_per_hr, "
        ":current_g_per_hr, :ramp_g_per_hr, :start_date) "
        "ON CONFLICT(user_id) DO UPDATE SET race_name = excluded.race_name, race_date = excluded.race_date, "
        "race_hours = excluded.race_hours, target_g_per_hr = excluded.target_g_per_hr, "
        "current_g_per_hr = excluded.current_g_per_hr, ramp_g_per_hr = excluded.ramp_g_per_hr",
        {**clean, "user_id": user_id, "start_date": start})


def delete_goal(conn, user_id):
    conn.execute("DELETE FROM fuel_goal WHERE user_id = ?", (user_id,))


# ---- products --------------------------------------------------------------------------------

def list_products(conn, user_id):
    rows = conn.execute("SELECT * FROM fuel_products WHERE user_id = ? ORDER BY position, id", (user_id,))
    return [{k: (bool(r[k]) if k == "use_in_plan" else r[k]) for k in r.keys() if k != "user_id"} for r in rows]


def validate_product(data):
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 40:
        raise ValueError("Give the product a name (up to 40 characters).")
    kind = data.get("kind") or "other"
    if kind not in KINDS:
        raise ValueError("Type must be one of: %s." % ", ".join(KINDS))
    unit = str(data.get("unit") or "").strip().lower() or {"drink": "flask"}.get(kind, kind if kind != "other" else "serving")
    if len(unit) > 12:
        raise ValueError("Unit must be a short word, like bar, flask or gel.")
    return {
        "name": name, "kind": kind, "unit": unit,
        "carbs_g": _number(data.get("carbs_g"), "Carbs per unit", 0, 300),
        "sodium_mg": _number(data.get("sodium_mg") or 0, "Sodium per unit", 0, 5000),
        "caffeine_mg": _number(data.get("caffeine_mg") or 0, "Caffeine per unit", 0, 1000),
        "use_in_plan": bool(data.get("use_in_plan")),
    }


def _check_product_rules(conn, user_id, clean, product_id):
    others = [p for p in list_products(conn, user_id) if p["id"] != product_id]
    if any(p["name"].lower() == clean["name"].lower() for p in others):
        raise ValueError("You already have a product called %s." % clean["name"])
    if clean["use_in_plan"] and sum(1 for p in others if p["use_in_plan"]) >= MAX_IN_PLAN:
        raise ValueError("Only %d products can be used in suggested mixes - untick one first (the rest are still "
                         "there to log)." % MAX_IN_PLAN)
    return others


def add_product(conn, user_id, clean):
    others = _check_product_rules(conn, user_id, clean, None)
    if len(others) >= MAX_PRODUCTS:
        raise ValueError("That's the most products you can keep (%d) - remove one first." % MAX_PRODUCTS)
    position = max([p["position"] for p in others], default=-1) + 1
    cur = conn.execute(
        "INSERT INTO fuel_products (user_id, name, kind, unit, carbs_g, sodium_mg, caffeine_mg, use_in_plan, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, clean["name"], clean["kind"], clean["unit"], clean["carbs_g"], clean["sodium_mg"],
         clean["caffeine_mg"], int(clean["use_in_plan"]), position))
    return cur.lastrowid


def update_product(conn, user_id, product_id, clean):
    if not conn.execute("SELECT 1 FROM fuel_products WHERE id = ? AND user_id = ?", (product_id, user_id)).fetchone():
        return False
    _check_product_rules(conn, user_id, clean, product_id)
    conn.execute(
        "UPDATE fuel_products SET name = ?, kind = ?, unit = ?, carbs_g = ?, sodium_mg = ?, caffeine_mg = ?, "
        "use_in_plan = ? WHERE id = ? AND user_id = ?",
        (clean["name"], clean["kind"], clean["unit"], clean["carbs_g"], clean["sodium_mg"], clean["caffeine_mg"],
         int(clean["use_in_plan"]), product_id, user_id))
    return True


def delete_product(conn, user_id, product_id):
    return conn.execute("DELETE FROM fuel_products WHERE id = ? AND user_id = ?", (product_id, user_id)).rowcount > 0


# ---- describing quantities -------------------------------------------------------------------

def fmt_qty(q):
    whole, half = int(q), (q - int(q)) >= 0.25
    if not whole:
        return "½"
    return "%d%s" % (whole, "½" if half else "")


def _unit_word(unit, qty):
    return unit if qty <= 1 else (unit + "es" if unit.endswith(("ch", "sh", "s", "x")) else unit + "s")


def describe(items):
    """[{unit, qty}, ...] -> "2 bars + 2½ flasks"."""
    return " + ".join("%s %s" % (fmt_qty(i["qty"]), _unit_word(i["unit"], i["qty"])) for i in items if i["qty"] > 0)


# ---- the targets -----------------------------------------------------------------------------

def progression(goal, dates, gut_by_date, today_iso):
    """Target g/h for each long-run date (ascending), following the rules in the module docstring."""
    cur, tgt, ramp = goal["current_g_per_hr"], goal["target_g_per_hr"], goal["ramp_g_per_hr"]
    floor = min(cur, tgt)
    out, prev = {}, None
    for d in dates:
        if prev is None:
            t = cur + ramp
        else:
            gut, last = gut_by_date.get(prev), out[prev]
            if gut == "fine":
                t = last + ramp
            elif gut == "some":
                t = last
            elif gut == "bad":
                t = max(last - ramp, floor)
            elif prev < today_iso:
                t = last           # a past run nobody reported on: no evidence the last step was handled
            else:
                t = last + ramp    # still to come: assume it goes fine, so the road to race day shows
        out[d] = round(min(t, tgt), 1)
        prev = d
    return out


def suggest_mix(products, grams):
    """A combination of the person's planner products (in halves) that lands close to `grams`, scored in
    grams-of-annoyance: every gram off the target costs 1, every gram by which food and drink are out of balance
    costs MIX_BALANCE_WEIGHT, and each extra kind of product costs MIX_EXTRA_PRODUCT_G. So "2 bars + 2 flasks"
    (220 g) beats "1 bar + 3 flasks" (230 g) for a 228 g target, but a plain "1 bar" still beats "½ bar +
    ½ flask" for 50 g. None if there's nothing to plan with."""
    cands = [p for p in products if p["use_in_plan"] and p["carbs_g"] > 0][:MAX_IN_PLAN]
    if not cands or not grams or grams <= 0:
        return None
    limits = [min(MAX_QTY * 2, math.ceil(grams / p["carbs_g"] * 2)) for p in cands]
    best, best_key = None, None
    for combo in itertools.product(*[range(n + 1) for n in limits]):
        if not any(combo):
            continue
        carbs = [c * 0.5 * p["carbs_g"] for c, p in zip(combo, cands)]
        total = sum(carbs)
        liquid = sum(g for g, p in zip(carbs, cands) if p["kind"] in LIQUID_KINDS)
        cost = (abs(total - grams) + MIX_BALANCE_WEIGHT * abs((total - liquid) - liquid)
                + MIX_EXTRA_PRODUCT_G * sum(1 for c in combo if c))
        key = (round(cost, 3), sum(combo))
        if best_key is None or key < best_key:
            best, best_key = combo, key
    items = [{"product_id": p["id"], "name": p["name"], "unit": p["unit"], "qty": c * 0.5, "carbs_g": p["carbs_g"]}
             for c, p in zip(best, cands) if c]
    return {"items": items, "total_g": round(sum(i["qty"] * i["carbs_g"] for i in items)), "text": describe(items)}


# ---- logging what was had --------------------------------------------------------------------

def validate_log(data, today):
    """(date iso, [(product_id, qty)], gut) from a request body, or ValueError."""
    gut = data.get("gut") or None
    if gut is not None and gut not in GUT:
        raise ValueError("Say how your stomach coped: %s." % ", ".join(GUT_LABELS[g].lower() for g in GUT))
    items, seen = [], set()
    for raw in data.get("items") or []:
        try:
            pid, qty = int(raw["product_id"]), float(raw["qty"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Each item needs a product and a quantity.")
        if pid in seen:
            raise ValueError("Each product can only appear once.")
        seen.add(pid)
        if qty == 0:
            continue
        if not math.isfinite(qty) or qty < 0 or qty > MAX_QTY or (qty * 2) != int(qty * 2):
            raise ValueError("Quantities go up in halves, from ½ to %d." % MAX_QTY)
        items.append((pid, qty))
    return items, gut


def save_log(conn, user_id, day_iso, items, gut):
    """Replace the day's log. Returns False (and stores nothing) if a product isn't this person's."""
    products = {p["id"]: p for p in list_products(conn, user_id)}
    if any(pid not in products for pid, _ in items):
        return False
    conn.execute("DELETE FROM fuel_log_items WHERE user_id = ? AND date = ?", (user_id, day_iso))
    conn.execute("DELETE FROM fuel_logs WHERE user_id = ? AND date = ?", (user_id, day_iso))
    if not items and not gut:
        return True
    conn.execute("INSERT INTO fuel_logs (user_id, date, gut) VALUES (?, ?, ?)", (user_id, day_iso, gut))
    for pid, qty in items:
        p = products[pid]
        conn.execute(
            "INSERT INTO fuel_log_items (user_id, date, product_name, kind, unit, carbs_g, sodium_mg, caffeine_mg, qty) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, day_iso, p["name"], p["kind"], p["unit"], p["carbs_g"], p["sodium_mg"], p["caffeine_mg"], qty))
    return True


def delete_everything(conn, user_id):
    for table in ("fuel_goal", "fuel_products", "fuel_logs", "fuel_log_items"):
        conn.execute("DELETE FROM %s WHERE user_id = ?" % table, (user_id,))


def _logs_between(conn, user_id, lo, hi):
    """{date: {"gut", "items": [...]}} for lo..hi inclusive."""
    logs = {r["date"]: {"gut": r["gut"], "items": []} for r in conn.execute(
        "SELECT date, gut FROM fuel_logs WHERE user_id = ? AND date BETWEEN ? AND ?", (user_id, lo, hi))}
    for r in conn.execute("SELECT * FROM fuel_log_items WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY rowid",
                          (user_id, lo, hi)):
        logs.setdefault(r["date"], {"gut": None, "items": []})["items"].append(dict(r))
    return logs


def _minutes_between(conn, user_id, lo, hi):
    """{date: minutes of the longest run that day} - what g/h is measured over."""
    return {r["date"]: round(r["secs"] / 60.0, 1) for r in conn.execute(
        "SELECT date, MAX(moving_time) AS secs FROM activities WHERE user_id = ? AND sport_group = 'run' "
        "AND date BETWEEN ? AND ? GROUP BY date", (user_id, lo, hi)) if r["secs"]}


def _log_view(log, minutes, target):
    items = [{"name": i["product_name"], "unit": i["unit"], "qty": i["qty"], "carbs_g": i["carbs_g"],
              "sodium_mg": i["sodium_mg"], "caffeine_mg": i["caffeine_mg"]} for i in log["items"]]
    carbs = sum(i["qty"] * i["carbs_g"] for i in items)
    sodium = sum(i["qty"] * i["sodium_mg"] for i in items)
    caffeine = sum(i["qty"] * i["caffeine_mg"] for i in items)
    hours = minutes / 60.0 if minutes else None
    per_hr = round(carbs / hours) if hours and items else None
    return {
        "items": items, "text": describe(items), "gut": log["gut"], "gut_label": GUT_LABELS.get(log["gut"]),
        "carbs_g": round(carbs), "sodium_mg": round(sodium), "caffeine_mg": round(caffeine),
        "minutes": minutes, "carbs_g_per_hr": per_hr,
        "sodium_mg_per_hr": round(sodium / hours) if hours and items else None,
        "vs_target_g_per_hr": round(per_hr - target) if per_hr is not None and target else None,
    }


# ---- putting it together ---------------------------------------------------------------------

def _long_runs(conn, user_id, lo, hi):
    """{date: plan row} of the long runs planned between lo and hi (inclusive); one per day."""
    out = {}
    for r in conn.execute("SELECT date, session_type, sport_group, planned_duration_min FROM plan "
                          "WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date, position, id", (user_id, lo, hi)):
        if is_long_run(r["session_type"], r["sport_group"]) and r["date"] not in out:
            out[r["date"]] = dict(r)
    return out


def _entry(day_iso, planned_min, target, products, logs, minutes, today_iso):
    planned_g = round(target * planned_min / 60.0) if target and planned_min else None
    return {
        "date": day_iso, "planned_minutes": planned_min, "target_g_per_hr": target, "planned_g": planned_g,
        "mix": suggest_mix(products, planned_g) if planned_g else None,
        "log": _log_view(logs[day_iso], minutes.get(day_iso), target) if day_iso in logs else None,
        "can_log": day_iso <= today_iso,
    }


def _context(conn, user_id, lo, hi):
    return _logs_between(conn, user_id, lo, hi), _minutes_between(conn, user_id, lo, hi)


def overview(conn, user_id, today):
    """Everything the Fuelling page needs."""
    today_iso = today.isoformat()
    goal, products = get_goal(conn, user_id), list_products(conn, user_id)
    out = {"goal": {k: v for k, v in goal.items() if k != "user_id"} if goal else None, "products": products, "presets": PRESETS, "limits": {
        "max_products": MAX_PRODUCTS, "max_in_plan": MAX_IN_PLAN, "max_qty": MAX_QTY},
        "gut_options": [{"value": g, "label": GUT_LABELS[g]} for g in GUT], "runs": [], "race": None, "next_target": None}
    if not goal:
        return out
    runs = _long_runs(conn, user_id, goal["start_date"], goal["race_date"])
    dates = sorted(runs)
    logs, minutes = _context(conn, user_id, goal["start_date"], goal["race_date"])
    targets = progression(goal, dates, {d: l["gut"] for d, l in logs.items()}, today_iso)
    out["runs"] = [_entry(d, runs[d]["planned_duration_min"], targets[d], products, logs, minutes, today_iso)
                   for d in dates]
    upcoming = next((r for r in out["runs"] if r["date"] >= today_iso), None)
    out["next_target"] = upcoming["target_g_per_hr"] if upcoming else None
    for r in out["runs"]:
        r["is_next"] = r is upcoming
    hours = goal["race_hours"]
    race_g = round(goal["target_g_per_hr"] * hours) if hours else None
    out["race"] = {
        "date": goal["race_date"], "name": goal["race_name"], "days_to_go": (_date.fromisoformat(goal["race_date"]) - today).days,
        "target_g_per_hr": goal["target_g_per_hr"], "hours": hours, "total_g": race_g,
        "mix": suggest_mix(products, race_g) if race_g else None,
    }
    return out


def annotate_week(conn, user_id, week, today):
    """Add a `fuel` entry to each planned long run in a week_view() result, and the product list the log
    dropdowns need. Runs without a goal still get the log box if the person has products."""
    today_iso = today.isoformat()
    goal, products = get_goal(conn, user_id), list_products(conn, user_id)
    items = [i for d in week["days"] for i in d["items"]
             if i["kind"] == "planned" and is_long_run(i["session_type"], i["sport_group"])]
    week["fuel"] = {"has_goal": goal is not None, "products": products,
                    "gut_options": [{"value": g, "label": GUT_LABELS[g]} for g in GUT]}
    if not items:
        return
    targets = {}
    if goal:
        runs = _long_runs(conn, user_id, goal["start_date"], goal["race_date"])
        dates = sorted(runs)
        glogs, _ = _context(conn, user_id, goal["start_date"], goal["race_date"])
        targets = progression(goal, dates, {d: l["gut"] for d, l in glogs.items()}, today_iso)
    logs, minutes = _context(conn, user_id, week["start"], week["end"])
    for item in items:
        target = targets.get(item["date"]) if goal and goal["start_date"] <= item["date"] <= goal["race_date"] else None
        entry = _entry(item["date"], item["planned_duration_min"], target, products, logs, minutes, today_iso)
        entry["available"] = bool(goal or products)
        item["fuel"] = entry


# ---- the shared demo account -----------------------------------------------------------------

def seed_demo(conn, user_id, today):
    """Give the demo login a believable fuelling set-up: a goal at the end of its plan, the three preset
    products, and a log for each past long run (mostly fine, one with a little discomfort), so visitors can see
    the targets climb and how a logged run reads. Rebuilt with the rest of the demo data (see demo_data.py)."""
    delete_everything(conn, user_id)
    plan_dates = [r["date"] for r in conn.execute("SELECT date FROM plan WHERE user_id = ?", (user_id,))]
    if not plan_dates:
        return
    start, end = min(plan_dates), max(plan_dates)
    goal = {"race_name": "Demo marathon", "race_date": max(end, today.isoformat()), "race_hours": 4.5,
            "target_g_per_hr": 75.0, "current_g_per_hr": 45.0, "ramp_g_per_hr": 5.0}
    conn.execute("INSERT INTO fuel_goal (user_id, race_name, race_date, race_hours, target_g_per_hr, "
                 "current_g_per_hr, ramp_g_per_hr, start_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (user_id, goal["race_name"], goal["race_date"], goal["race_hours"], goal["target_g_per_hr"],
                  goal["current_g_per_hr"], goal["ramp_g_per_hr"], start))
    for preset in PRESETS:
        add_product(conn, user_id, validate_product(preset))
    products = list_products(conn, user_id)
    today_iso = today.isoformat()
    runs = _long_runs(conn, user_id, start, goal["race_date"])
    goal_row = get_goal(conn, user_id)
    gut_by_date, past = {}, [d for d in sorted(runs) if d < today_iso]
    for n, d in enumerate(past):
        target = progression(goal_row, past[:n + 1], gut_by_date, today_iso)[d]
        minutes = runs[d]["planned_duration_min"]
        mix = suggest_mix(products, round(target * minutes / 60.0)) if minutes else None
        gut = "some" if n == 2 else "fine"
        gut_by_date[d] = gut
        save_log(conn, user_id, d, [(i["product_id"], i["qty"]) for i in mix["items"]] if mix else [], gut)
