"""Carb-fuelling for long runs: train the gut to take the carbs per hour a race will need.

A person sets a race (date, the carbs-per-hour they want to reach, what they take comfortably now) and keeps a
short list of their own products. Every long run in their plan then gets a target that steps up towards the race
rate, shown as grams per hour, a total for the planned time and a mix of their products ("2 bars + 2½ flasks").
After the run they log what they actually had - food, drink and water - plus how their stomach coped, and the next
target responds to that. Fluid and sodium get advice too, scaled to the temperature (see app/hydration.py and
app/weather.py), and the log flags too little, too much, and carbs far above what the stomach has been trained for.

Deliberately plain rules, not clever guessing - what a person's gut tolerates is theirs to judge:
  * the first long run is `ramp` g/h above the rate they take comfortably now (capped at the race target);
  * after a run logged "fine" the next target steps up by `ramp` - from the target, unless they actually took
    clearly more and were fine, in which case from that (credited up to two steps ahead, so one over-enthusiastic
    entry can't launch the plan) - and if they clearly took less than the target it repeats instead;
  * after "some discomfort" it holds; after a "bad gut" it steps back by `ramp` (never below where they started);
    a past run with no gut report holds, because there's no evidence the last step was handled;
  * runs still in the future are projected as if each goes fine, so the whole road to race day is visible.

Only runs whose session type says "long" get a target. None of it comes from Strava (so disconnecting Strava
leaves it alone) and none of it is medical or dietitian advice - the numbers are the person's own.
"""
import itertools
import math
from datetime import date as _date
from datetime import timedelta

from . import hydration, weather

MAX_PRODUCTS = 8       # "not too many": a short list is the point
MAX_IN_PLAN = 3        # products the planner may combine into a suggested mix (the rest are tracking-only)
MAX_QTY = 12           # most of one product that can be logged in one go
MIX_BALANCE_WEIGHT = 0.15      # how much an uneven split between food and drink matters, per gram, next to being
MIX_EXTRA_PRODUCT_G = 5        # a gram off target; and what carrying one more kind of product is worth, in grams
CARB_TOLERANCE_G_PER_HR = 10   # within this many g/h either side of the target counts as on target (green); outside is red
CARB_CEILING_G_PER_HR = 120    # beyond this, even gut-trained athletes are past what the research has looked at
KINDS = ("bar", "drink", "gel", "water", "other")
GUT = ("fine", "some", "bad")
GUT_LABELS = {
    "fine": "No problems",
    "some": "Some discomfort",
    "bad": "Bad gut",
}
LIQUID_KINDS = ("drink", "water")

# One-tap starting points the Fuelling page offers. The numbers are what each maker publishes for a single
# serving, but they're only a start - each product is editable, and people are told to check their own packet.
PRESETS = [
    {"key": "styrkr_bar50", "name": "Styrkr BAR50", "kind": "bar", "unit": "bar", "carbs_g": 50,
     "sodium_mg": 0, "caffeine_mg": 0, "use_in_plan": True,
     "note": "50 g carbs per bar. Sodium isn't filled in - add it from the packet if you track it."},
    {"key": "styrkr_mix60", "name": "Styrkr MIX60", "kind": "drink", "unit": "flask", "carbs_g": 60,
     "sodium_mg": 56, "caffeine_mg": 0, "fluid_ml": 500, "use_in_plan": True,
     "note": "One 65 g sachet (60 g carbs) in a 500 ml flask. The sodium figure comes from retailer listings - "
             "check your sachet."},
    {"key": "gel", "name": "Energy gel", "kind": "gel", "unit": "gel", "carbs_g": 25,
     "sodium_mg": 0, "caffeine_mg": 0, "use_in_plan": False,
     "note": "A typical gel. Kept out of suggested mixes - it's here so you can log one if you take it. Check the packet."},
    {"key": "electrolyte", "name": "Electrolyte tablet", "kind": "other", "unit": "tablet", "carbs_g": 0,
     "sodium_mg": 250, "caffeine_mg": 0, "fluid_ml": 0, "use_in_plan": False,
     "note": "A typical electrolyte tablet or capsule. Products range from about 200 to 1,000 mg of sodium each, so "
             "check your packet and edit this."},
    {"key": "water", "name": "Water", "kind": "water", "unit": "flask", "carbs_g": 0,
     "sodium_mg": 0, "caffeine_mg": 0, "fluid_ml": 500, "use_in_plan": False,
     "note": "Plain water in a 500 ml flask. It counts towards your fluid total, not your carbs."},
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
    unit = str(data.get("unit") or "").strip().lower() or {"drink": "flask", "water": "flask"}.get(kind, kind if kind != "other" else "serving")
    if len(unit) > 12:
        raise ValueError("Unit must be a short word, like bar, flask or gel.")
    fluid = _number(data.get("fluid_ml") or 0, "Fluid per unit", 0, 3000)
    if kind == "water" and not fluid:
        raise ValueError("Water needs a size in ml - 500 for a 500 ml flask.")
    return {
        "name": name, "kind": kind, "unit": unit,
        # water and "other" things (salt tablets, caffeine) have no carbs to speak of, so it needn't be filled in
        "carbs_g": _number(0 if data.get("carbs_g") in (None, "") and kind in ("water", "other") else data.get("carbs_g"),
                           "Carbs per unit", 0, 300),
        "sodium_mg": _number(data.get("sodium_mg") or 0, "Sodium per unit", 0, 5000),
        "caffeine_mg": _number(data.get("caffeine_mg") or 0, "Caffeine per unit", 0, 1000),
        "fluid_ml": fluid,
        "use_in_plan": bool(data.get("use_in_plan")) and kind != "water",
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
        "INSERT INTO fuel_products (user_id, name, kind, unit, carbs_g, sodium_mg, caffeine_mg, fluid_ml, use_in_plan, "
        "position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, clean["name"], clean["kind"], clean["unit"], clean["carbs_g"], clean["sodium_mg"],
         clean["caffeine_mg"], clean["fluid_ml"], int(clean["use_in_plan"]), position))
    return cur.lastrowid


def update_product(conn, user_id, product_id, clean):
    if not conn.execute("SELECT 1 FROM fuel_products WHERE id = ? AND user_id = ?", (product_id, user_id)).fetchone():
        return False
    _check_product_rules(conn, user_id, clean, product_id)
    conn.execute(
        "UPDATE fuel_products SET name = ?, kind = ?, unit = ?, carbs_g = ?, sodium_mg = ?, caffeine_mg = ?, "
        "fluid_ml = ?, use_in_plan = ? WHERE id = ? AND user_id = ?",
        (clean["name"], clean["kind"], clean["unit"], clean["carbs_g"], clean["sodium_mg"], clean["caffeine_mg"],
         clean["fluid_ml"], int(clean["use_in_plan"]), product_id, user_id))
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
    """[{unit, qty, name}, ...] -> "2 bars + 2½ flasks". When two items share a unit ("2 bars + 1½ bars" would be
    a riddle) those items are named instead: "2 × Styrkr BAR50 + 1½ × Rice cake"."""
    items = [i for i in items if i["qty"] > 0]
    clash = {u for u in (i["unit"] for i in items) if sum(1 for i in items if i["unit"] == u) > 1}
    return " + ".join(
        "%s × %s" % (fmt_qty(i["qty"]), i.get("name") or i["unit"]) if i["unit"] in clash
        else "%s %s" % (fmt_qty(i["qty"]), _unit_word(i["unit"], i["qty"])) for i in items)


# ---- the targets -----------------------------------------------------------------------------

def progression(goal, dates, outcomes, today_iso):
    """Target g/h for each long-run date (ascending), following the rules in the module docstring. `outcomes` maps
    a date to how that run went: a gut string ("fine"/"some"/"bad"), or {"gut": ..., "per_hr": carbs actually taken
    per hour, if it's known}."""
    cur, tgt, ramp = goal["current_g_per_hr"], goal["target_g_per_hr"], goal["ramp_g_per_hr"]
    floor, tol = min(cur, tgt), CARB_TOLERANCE_G_PER_HR
    out, prev = {}, None
    for d in dates:
        if prev is None:
            t = cur + ramp
        else:
            o, last = outcomes.get(prev), out[prev]
            gut, per_hr = (o.get("gut"), o.get("per_hr")) if isinstance(o, dict) else (o, None)
            if gut == "fine":
                if per_hr is not None and per_hr < last - tol:
                    t = last                                              # didn't manage the target: go again
                else:
                    shown = last if per_hr is None or per_hr <= last + tol else min(per_hr, last + 2 * ramp)
                    t = shown + ramp
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

def validate_log(data, today=None):
    """([item, ...], gut) from a request body, or ValueError. Each item says what was had in exactly one way -
    `product_id` (one of their products), `preset` (a quick-add product they haven't listed yet) or `new_product`
    (typed in on the spot) - plus `qty` in halves. See resolve_log_items for what happens next."""
    gut = data.get("gut") or None
    if gut is not None and gut not in GUT:
        raise ValueError("Say how your stomach coped: %s." % ", ".join(GUT_LABELS[g].lower() for g in GUT))
    items = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            raise ValueError("Each item needs a product and a quantity.")
        try:
            qty = float(raw["qty"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Each item needs a product and a quantity.")
        if not math.isfinite(qty) or qty < 0 or qty > MAX_QTY or (qty * 2) != int(qty * 2):
            raise ValueError("Quantities go up in halves, from ½ to %d." % MAX_QTY)
        if qty == 0:
            continue
        refs = [k for k in ("product_id", "preset", "new_product") if raw.get(k) not in (None, "")]
        if len(refs) != 1:
            raise ValueError("Each item needs a product and a quantity.")
        items.append({"ref": refs[0], "value": raw[refs[0]], "qty": qty})
    return items, gut


def resolve_log_items(conn, user_id, items):
    """Turn what the person picked into [(product_id, qty)], adding any product they picked from the quick-add
    list or typed in on the spot - so logging a bar or gel never needs a trip to the product list first. The same
    product picked twice is added up. Raises ValueError (and the caller's transaction rolls back, so nothing is
    half-added) if a product can't be added."""
    totals = {}
    for it in items:
        products = list_products(conn, user_id)
        by_id = {p["id"]: p for p in products}
        by_name = {p["name"].lower(): p for p in products}
        if it["ref"] == "product_id":
            try:
                pid = int(it["value"])
            except (TypeError, ValueError):
                raise ValueError("Each item needs a product and a quantity.")
            if pid not in by_id:
                raise ValueError("One of those products isn't in your list any more - refresh and try again.")
        elif it["ref"] == "preset":
            preset = next((p for p in PRESETS if p["key"] == it["value"]), None)
            if preset is None:
                raise ValueError("That quick-add product isn't available.")
            existing = by_name.get(preset["name"].lower())
            pid = existing["id"] if existing else add_product(conn, user_id, validate_product(preset))
        else:
            if not isinstance(it["value"], dict):
                raise ValueError("Each item needs a product and a quantity.")
            raw = dict(it["value"])
            if "use_in_plan" not in raw:   # bars and drinks go into suggested mixes while there's room; the rest don't
                room = sum(1 for p in products if p["use_in_plan"]) < MAX_IN_PLAN
                raw["use_in_plan"] = room and raw.get("kind") in ("bar", "drink")
            pid = add_product(conn, user_id, validate_product(raw))
        totals[pid] = totals.get(pid, 0) + it["qty"]
    if any(q > MAX_QTY for q in totals.values()):
        raise ValueError("Quantities go up in halves, from ½ to %d." % MAX_QTY)
    return list(totals.items())


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
            "INSERT INTO fuel_log_items (user_id, date, product_name, kind, unit, carbs_g, sodium_mg, caffeine_mg, "
            "fluid_ml, qty) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, day_iso, p["name"], p["kind"], p["unit"], p["carbs_g"], p["sodium_mg"], p["caffeine_mg"],
             p["fluid_ml"], qty))
    return True


def delete_everything(conn, user_id):
    for table in ("fuel_goal", "fuel_products", "fuel_logs", "fuel_log_items", "fuel_prefs"):
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


def _per_hr(log, minutes):
    if not log["items"] or not minutes:
        return None
    return sum(i["qty"] * i["carbs_g"] for i in log["items"]) / (minutes / 60.0)


def _outcomes(logs, minutes):
    """{date: {"gut", "per_hr"}} - how each logged run went, for the targets to respond to."""
    return {d: {"gut": l["gut"], "per_hr": _per_hr(l, minutes.get(d))} for d, l in logs.items()}


def _log_view(log, minutes, target, advice=None):
    items = [{"name": i["product_name"], "unit": i["unit"], "qty": i["qty"], "carbs_g": i["carbs_g"],
              "sodium_mg": i["sodium_mg"], "caffeine_mg": i["caffeine_mg"], "fluid_ml": i.get("fluid_ml") or 0}
             for i in log["items"]]
    total = lambda key: sum(i["qty"] * i[key] for i in items)
    carbs, sodium, caffeine, fluid = total("carbs_g"), total("sodium_mg"), total("caffeine_mg"), total("fluid_ml")
    hours = minutes / 60.0 if minutes else None
    per = lambda x: round(x / hours) if hours and items else None
    # nothing logged for fluid or sodium means "not tracked", not "had none" - so no red mark and no scolding for it
    per_hr, fluid_hr, sodium_hr = per(carbs), per(fluid) if fluid else None, per(sodium) if sodium else None
    delta = per_hr - target if per_hr is not None and target else None
    gut = log["gut"]
    alerts = []
    if delta is not None and delta > CARB_TOLERANCE_G_PER_HR:
        if gut == "fine":
            alerts.append({"level": "info", "text": "Well above target and your stomach coped - a good sign. The next target builds "
                           "from what you managed (a little ahead of the plan); to catch up faster, raise your "
                           "\"comfortable now\" rate on the Fuelling page."})
        elif gut in ("some", "bad"):
            alerts.append({"level": "warn", "text": "Well above target and your stomach didn't cope - probably too much, "
                           "too soon. The next target holds or steps back."})
        else:
            alerts.append({"level": "info", "text": "Well above target - say how your stomach coped so the next target can respond."})
    if per_hr is not None and per_hr > CARB_CEILING_G_PER_HR:
        alerts.append({"level": "warn", "text": "%d g/h is above the ~%d g/h that even gut-trained athletes have been studied "
                       "at. Beyond that, extra carbs mostly add stomach trouble, not energy." % (per_hr, CARB_CEILING_G_PER_HR)})
    if fluid_hr is not None and fluid_hr > hydration.FLUID_MAX_ML_PER_HR:
        alerts.append({"level": "warn", "text": "%d ml/h is a lot. Drinking more than you sweat can dilute your blood sodium "
                       "(hyponatraemia): drink to thirst, and weigh yourself before and after a run - gaining weight means "
                       "you drank too much." % fluid_hr})
    if (advice and fluid_hr is not None and advice["fluid_ml_per_hr"] and advice["band"] in ("hot", "very hot")
            and fluid_hr < advice["fluid_ml_per_hr"] * (1 - hydration.FLUID_TOLERANCE)):
        alerts.append({"level": "warn", "text": "It was hot and you drank less than advised (%d ml/h vs about %d) - watch for "
                       "dehydration, especially on the next hot run." % (fluid_hr, advice["fluid_ml_per_hr"])})
    if (advice and sodium_hr is not None and advice["sodium_mg_per_hr"]
            and sodium_hr < advice["sodium_mg_per_hr"] * (1 - hydration.SODIUM_TOLERANCE)):
        alerts.append({"level": "info", "text": "Sodium was well below your advice (%d vs about %d mg/h). If you took electrolytes "
                       "you haven't logged, add them; if not, consider some on long or hot runs." % (sodium_hr, advice["sodium_mg_per_hr"])})
    return {
        "items": items, "text": describe(items), "gut": gut, "gut_label": GUT_LABELS.get(gut),
        "carbs_g": round(carbs), "sodium_mg": round(sodium), "caffeine_mg": round(caffeine), "fluid_ml": round(fluid),
        "minutes": minutes, "carbs_g_per_hr": per_hr, "sodium_mg_per_hr": sodium_hr, "fluid_ml_per_hr": fluid_hr,
        "vs_target_g_per_hr": round(delta) if delta is not None else None,
        "carb_status": None if delta is None else "ok" if abs(delta) <= CARB_TOLERANCE_G_PER_HR else "off",
        "fluid_status": hydration.status(fluid_hr, advice and advice["fluid_ml_per_hr"], hydration.FLUID_TOLERANCE),
        "sodium_status": hydration.status(sodium_hr, advice and advice["sodium_mg_per_hr"], hydration.SODIUM_TOLERANCE),
        "alerts": alerts,
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


class _Conditions:
    """Forecast conditions and fluid/sodium advice for runs, fetched lazily: nothing is requested unless the person
    has set a location and a run falls inside the forecast window. Advice is always given - from their own usual
    rates if they've set them, else the defaults - because drink and salt are part of the plan, not an opt-in."""

    def __init__(self, prefs, today):
        self.prefs, self.today, self._table = prefs or hydration.default_prefs(), today, None

    def _forecast(self):
        if self._table is None:
            p = self.prefs
            self._table = weather.hourly_table(p["lat"], p["lon"]) if p["lat"] is not None else {}
        return self._table

    def for_run(self, day_iso, minutes, manual_temp=None):
        """(conditions or None, advice, weather_state) - the state says why there's no forecast when there isn't one:
        "no_location", "out_of_range" (further ahead than forecasts reach) or "unavailable" (the service didn't answer)."""
        p = self.prefs
        if p["lat"] is None:
            return None, hydration.advice(p, manual_temp, minutes, "manual"), "no_location"
        day = _date.fromisoformat(day_iso)
        if not (self.today - timedelta(days=weather.PAST_DAYS) <= day <= self.today + timedelta(days=weather.FORECAST_DAYS - 1)):
            return None, hydration.advice(p, manual_temp, minutes, "manual"), "out_of_range"
        found = weather.conditions(self._forecast(), day_iso, p["start_hour"], minutes / 60.0 if minutes else None)
        if not found:
            return None, hydration.advice(p, manual_temp, minutes, "manual"), "unavailable"
        return {**found, "source": "forecast"}, hydration.advice(p, found["temp_c"], minutes, "forecast"), "forecast"


def _plan_extras(advice, mix, products, minutes):
    """Put the drink total and the salt answer into the run's advice: how much to drink in all and how much of that
    the planned drinks already supply (the rest is water), and whether to take salt - how much sodium the run
    calls for, how much the planned mix already holds, and so how much to add (as the person's own electrolyte
    product, if they've listed one)."""
    by_id = {p["id"]: p for p in products}
    items = mix["items"] if mix else []
    mix_fluid = sum(i["qty"] * by_id[i["product_id"]]["fluid_ml"] for i in items if i["product_id"] in by_id)
    mix_sodium = sum(i["qty"] * by_id[i["product_id"]]["sodium_mg"] for i in items if i["product_id"] in by_id)
    total_fluid, total_sodium = advice["fluid_total_ml"], advice["sodium_total_mg"]

    water_ml = max(0, total_fluid - mix_fluid) if total_fluid else None
    water = next((p for p in products if p["kind"] == "water" and p["fluid_ml"] > 0), None)
    flasks = None
    if water and water_ml and water_ml >= 100:
        q = max(0.5, round(water_ml / water["fluid_ml"] * 2) / 2)
        flasks = describe([{"unit": water["unit"], "qty": q, "name": water["name"]}])
    verdict, why = hydration.salt_verdict(minutes, advice["band"])
    add_mg = None
    if verdict != "no" and total_sodium:
        add_mg = max(0, total_sodium - mix_sodium)
    elif verdict == "no":
        add_mg = 0
    tablet_text = None
    salt_product = next((p for p in products if p["sodium_mg"] >= 100 and p["carbs_g"] <= 5 and p["kind"] != "water"), None)
    if salt_product and add_mg and add_mg >= 50:
        q = max(0.5, math.ceil(add_mg / salt_product["sodium_mg"] * 2) / 2)
        tablet_text = describe([{"unit": salt_product["unit"], "qty": q, "name": salt_product["name"]}])
    return {**advice,
            "drink": {"total_ml": total_fluid, "from_mix_ml": round(mix_fluid), "water_ml": round(water_ml, -1) if water_ml is not None else None,
                      "water_text": flasks},
            "salt": {"verdict": verdict, "why": why, "total_mg": total_sodium, "from_mix_mg": round(mix_sodium),
                     "add_mg": round(add_mg, -1) if add_mg is not None else None, "product_text": tablet_text}}


def _entry(day_iso, planned_min, target, products, logs, minutes, today_iso, conditions):
    planned_g = round(target * planned_min / 60.0) if target and planned_min else None
    mix = suggest_mix(products, planned_g) if planned_g else None
    cond, advice, weather_state = conditions.for_run(day_iso, planned_min)
    advice = _plan_extras(advice, mix, products, planned_min)
    return {
        "date": day_iso, "planned_minutes": planned_min, "target_g_per_hr": target, "planned_g": planned_g,
        "mix": mix, "conditions": cond, "advice": advice, "weather_state": weather_state,
        "log": _log_view(logs[day_iso], minutes.get(day_iso), target, advice) if day_iso in logs else None,
        "can_log": day_iso <= today_iso,
    }


def _context(conn, user_id, lo, hi):
    return _logs_between(conn, user_id, lo, hi), _minutes_between(conn, user_id, lo, hi)


def overview(conn, user_id, today):
    """Everything the Fuelling page needs."""
    today_iso = today.isoformat()
    goal, products, prefs = get_goal(conn, user_id), list_products(conn, user_id), hydration.get_prefs(conn, user_id)
    out = {"goal": {k: v for k, v in goal.items() if k != "user_id"} if goal else None, "products": products,
           "presets": PRESETS, "prefs": prefs, "limits": {
               "max_products": MAX_PRODUCTS, "max_in_plan": MAX_IN_PLAN, "max_qty": MAX_QTY},
           "gut_options": [{"value": g, "label": GUT_LABELS[g]} for g in GUT], "runs": [], "race": None, "next_target": None}
    if not goal:
        return out
    conditions = _Conditions(prefs, today)
    runs = _long_runs(conn, user_id, goal["start_date"], goal["race_date"])
    dates = sorted(runs)
    logs, minutes = _context(conn, user_id, goal["start_date"], goal["race_date"])
    targets = progression(goal, dates, _outcomes(logs, minutes), today_iso)
    out["runs"] = [_entry(d, runs[d]["planned_duration_min"], targets[d], products, logs, minutes, today_iso, conditions)
                   for d in dates]
    upcoming = next((r for r in out["runs"] if r["date"] >= today_iso), None)
    out["next_target"] = upcoming["target_g_per_hr"] if upcoming else None
    for r in out["runs"]:
        r["is_next"] = r is upcoming
    hours = goal["race_hours"]
    race_g = round(goal["target_g_per_hr"] * hours) if hours else None
    race_cond, race_advice, race_state = conditions.for_run(goal["race_date"], hours * 60 if hours else None,
                                                            prefs["race_temp_c"] if prefs else None)
    race_mix = suggest_mix(products, race_g) if race_g else None
    race_advice = _plan_extras(race_advice, race_mix, products, hours * 60 if hours else None)
    out["race"] = {
        "date": goal["race_date"], "name": goal["race_name"], "days_to_go": (_date.fromisoformat(goal["race_date"]) - today).days,
        "target_g_per_hr": goal["target_g_per_hr"], "hours": hours, "total_g": race_g,
        "mix": race_mix, "conditions": race_cond, "advice": race_advice, "weather_state": race_state,
    }
    return out


def annotate_week(conn, user_id, week, today):
    """Add a `fuel` entry to each planned long run in a week_view() result, and the product list the log
    dropdowns need. Runs without a goal still get the log box - products can be added from the log itself."""
    today_iso = today.isoformat()
    goal, products, prefs = get_goal(conn, user_id), list_products(conn, user_id), hydration.get_prefs(conn, user_id)
    items = [i for d in week["days"] for i in d["items"]
             if i["kind"] == "planned" and is_long_run(i["session_type"], i["sport_group"])]
    week["fuel"] = {"has_goal": goal is not None, "products": products, "presets": PRESETS,
                    "gut_options": [{"value": g, "label": GUT_LABELS[g]} for g in GUT]}
    if not items:
        return
    targets = {}
    if goal:
        runs = _long_runs(conn, user_id, goal["start_date"], goal["race_date"])
        dates = sorted(runs)
        glogs, gminutes = _context(conn, user_id, goal["start_date"], goal["race_date"])
        targets = progression(goal, dates, _outcomes(glogs, gminutes), today_iso)
    logs, minutes = _context(conn, user_id, week["start"], week["end"])
    conditions = _Conditions(prefs, today)
    for item in items:
        target = targets.get(item["date"]) if goal and goal["start_date"] <= item["date"] <= goal["race_date"] else None
        item["fuel"] = _entry(item["date"], item["planned_duration_min"], target, products, logs, minutes, today_iso,
                              conditions)


# ---- the shared demo account -----------------------------------------------------------------

def seed_demo(conn, user_id, today):
    """Give the demo login a believable fuelling set-up: a goal at the end of its plan, the preset
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
    water = next(p for p in products if p["kind"] == "water")
    hydration.save_prefs(conn, user_id, hydration.validate_prefs({}))    # usual rates, no location: nothing is fetched
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
        items = [(i["product_id"], i["qty"]) for i in mix["items"]] if mix else []
        if minutes:                                  # about a flask of plain water an hour, on top of the drink
            items.append((water["id"], max(0.5, round(minutes / 60.0 * 2) / 2)))
        save_log(conn, user_id, d, items, gut)
