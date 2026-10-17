"""Fluid and sodium advice for a long run, scaled to the conditions, plus the thresholds the log is judged against.

Plain, adjustable rules from published ranges - not medical advice, and people's sweat rates differ enormously
(roughly 400-2,500 ml an hour), so the *person's own usual rates* are the starting point and the temperature only
scales them: colder than 10 C less, 18-23 C a little more, hotter more still. The advice is never allowed above
about 800 ml an hour (unless they've set their own usual rate higher): drinking more than you sweat is how people
get hyponatraemia, so the log warns about over-drinking too, not only under-drinking.

Sources behind the defaults: ACSM puts sodium at roughly 300-600 mg an hour in prolonged exercise, and typical
guidance for runners is 400-800 ml of fluid an hour, more in heat.
"""
import math

# (upper bound in C exclusive, multiplier on the person's usual rate, label)
TEMP_BANDS = [(10, 0.8, "cold"), (18, 1.0, "mild"), (24, 1.2, "warm"), (30, 1.4, "hot"), (math.inf, 1.6, "very hot")]
FLUID_MAX_ML_PER_HR = 800
FLUID_TOLERANCE = 0.20         # within +-20% of the advice counts as on target
SODIUM_TOLERANCE = 0.25
START_HOUR_DEFAULT, FLUID_DEFAULT, SODIUM_DEFAULT = 8, 500, 400


def default_prefs():
    """What the plan uses until someone sets their own: the usual rates, and no location (so no forecast)."""
    return {"place": None, "lat": None, "lon": None, "start_hour": START_HOUR_DEFAULT, "fluid_ml_per_hr": FLUID_DEFAULT,
            "sodium_mg_per_hr": SODIUM_DEFAULT, "race_temp_c": None}


def set_place(conn, user_id, place, lat, lon):
    """Change only where they run - from the quick "add where you run" step on a run - keeping any other settings
    they've made (or the defaults, if they've made none yet)."""
    current = get_prefs(conn, user_id) or default_prefs()
    save_prefs(conn, user_id, validate_prefs({**current, "place": place, "lat": lat, "lon": lon}))


def get_prefs(conn, user_id):
    row = conn.execute("SELECT * FROM fuel_prefs WHERE user_id = ?", (user_id,)).fetchone()
    return {k: row[k] for k in row.keys() if k != "user_id"} if row else None


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


def validate_prefs(data):
    place = str(data.get("place") or "").strip()[:100]
    lat = _number(data.get("lat"), "Latitude", -90, 90, allow_none=True)
    lon = _number(data.get("lon"), "Longitude", -180, 180, allow_none=True)
    if not place or lat is None or lon is None:
        place, lat, lon = None, None, None            # no usable place: no location
    return {
        "place": place, "lat": round(lat, 2) if lat is not None else None, "lon": round(lon, 2) if lon is not None else None,
        "start_hour": int(_number(data.get("start_hour", START_HOUR_DEFAULT), "Start hour", 0, 23)),
        "fluid_ml_per_hr": _number(data.get("fluid_ml_per_hr", FLUID_DEFAULT), "Usual fluid per hour", 100, 1500),
        "sodium_mg_per_hr": _number(data.get("sodium_mg_per_hr", SODIUM_DEFAULT), "Usual sodium per hour", 0, 2000),
        "race_temp_c": _number(data.get("race_temp_c"), "Race-day temperature", -30, 50, allow_none=True),
    }


def save_prefs(conn, user_id, clean):
    conn.execute(
        "INSERT INTO fuel_prefs (user_id, place, lat, lon, start_hour, fluid_ml_per_hr, sodium_mg_per_hr, race_temp_c) "
        "VALUES (:user_id, :place, :lat, :lon, :start_hour, :fluid_ml_per_hr, :sodium_mg_per_hr, :race_temp_c) "
        "ON CONFLICT(user_id) DO UPDATE SET place = excluded.place, lat = excluded.lat, lon = excluded.lon, "
        "start_hour = excluded.start_hour, fluid_ml_per_hr = excluded.fluid_ml_per_hr, "
        "sodium_mg_per_hr = excluded.sodium_mg_per_hr, race_temp_c = excluded.race_temp_c",
        {**clean, "user_id": user_id})


def delete_prefs(conn, user_id):
    conn.execute("DELETE FROM fuel_prefs WHERE user_id = ?", (user_id,))


def band(temp_c):
    for upper, mult, label in TEMP_BANDS:
        if temp_c < upper:
            return mult, label


def _to_50(x):
    return int(round(x / 50.0)) * 50


def advice(prefs, temp_c, minutes, source=None):
    """What to aim for on this run: fluid and sodium per hour (and in total for the planned time), scaled from the
    person's usual rates by the temperature. `source` says where the temperature came from ("forecast" or
    "manual"); with no temperature the usual rates are returned as they are."""
    mult, label = band(temp_c) if temp_c is not None else (1.0, "usual")
    fluid = min(_to_50(prefs["fluid_ml_per_hr"] * mult), max(FLUID_MAX_ML_PER_HR, _to_50(prefs["fluid_ml_per_hr"])))
    sodium = _to_50(prefs["sodium_mg_per_hr"] * mult)
    hours = minutes / 60.0 if minutes else None
    return {
        "temp_c": temp_c, "band": label, "source": source if temp_c is not None else None,
        "fluid_ml_per_hr": fluid, "sodium_mg_per_hr": sodium,
        "fluid_total_ml": int(round(fluid * hours, -1)) if hours else None,
        "sodium_total_mg": int(round(sodium * hours, -1)) if hours else None,
    }


def status(actual, target, tolerance):
    """"ok" when within the tolerance of the target, "off" outside it, None if either is unknown."""
    if actual is None or not target:
        return None
    return "ok" if abs(actual - target) <= target * tolerance else "off"


def salt_verdict(minutes, band_label):
    """("yes" | "optional" | "no", why) - should extra salt be part of this run's plan? Under an hour: no. Up to two
    hours in cool or mild weather: optional, drink and food usually cover it. Anything longer, or warm and hotter:
    yes, because that's when real sodium goes out in sweat. A rough guide, not medical advice."""
    if minutes is not None and minutes < 60:
        return "no", "it's under an hour."
    if minutes is not None and minutes < 120 and band_label in ("cold", "mild", "usual"):
        return "optional", "under two hours in cool or mild weather, your drink and food usually cover it."
    if minutes is None:
        return ("yes", "it's hot enough that you'll lose real sodium in sweat.") if band_label in ("hot", "very hot") \
            else ("optional", "add a planned duration to this session for a firmer answer.")
    why = ("it's %s and %s" % (band_label, _hm(minutes))) if band_label != "usual" else ("it's %s long" % _hm(minutes))
    return "yes", why + " - long or warm enough that you'll lose real sodium in sweat."


def _hm(minutes):
    h, m = divmod(int(round(minutes)), 60)
    return "%dh%02d" % (h, m) if h else "%d min" % m
