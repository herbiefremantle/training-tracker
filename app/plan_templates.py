"""Ready-made training plans: a shared, built-in library (not per-user) that a user can drop
straight onto their own plan, instead of writing one from scratch.

Source data is app/data/training_plans.csv - a template per (race distance, level), with a
*relative* day (day_offset 0=Mon..6=Sun) rather than a real date, since the same template is
reused by everyone who picks it, each starting from their own Monday. build_rows() turns a
template into real, dated plan rows the same way a pasted CSV becomes rows in planparse.py -
same fields, same sport-grouping - so from that point on these behave exactly like any other
imported plan (matching, status, editing).

This is general fitness advice, not coaching: see DISCLAIMER, always shown alongside the picker.
"""
import csv
from datetime import date, timedelta
from pathlib import Path

from . import sports

DATA_PATH = Path(__file__).parent / "data" / "training_plans.csv"

DISCLAIMER = (
    "These are general training templates, not personalised coaching - built from established, "
    "publicly documented endurance-training principles, for a plausible entry-level or "
    "step-up athlete. They know nothing about you. For a plan that actually fits your current "
    "fitness, injury history and the terrain and elevation of your specific event, uploading "
    "your own personal plan above is strongly recommended. If you're new to running, have an "
    "injury history, or have any health concerns, check with a doctor or a qualified coach "
    "before starting."
)

# Every distinct session_type value in the CSV must be mapped here to a sport that
# sports.group_from_plan() resolves cleanly (no unrecognised-sport warning, no unmatched-sport
# error). test_plan_templates.py asserts this mapping is exhaustive, so a future edit to the CSV
# that introduces a new session_type fails a test rather than silently dropping/mis-grouping rows.
SESSION_TYPE_SPORT = {
    "Run - easy": "Run",
    "Run - easy (back-to-back)": "Run",
    "Run - easy (recovery)": "Run",   # the day after a back-to-back long run, in the two ultra intermediate plans
    "Run - speed/intervals": "Run",
    "Run - tempo": "Run",
    "Long run": "Run",
    "RACE DAY": "Run",
    "Cross-training / rest": "Rest",  # explicitly optional in the notes - never "missed" if skipped
    "Rest": "Rest",
}

# Display order for the picker: ascending distance/difficulty, not CSV or alphabetical order.
RACE_ORDER = ["10K", "Half Marathon", "Marathon", "50K", "100K"]
LEVEL_ORDER = ["Beginner", "Intermediate"]


def _load():
    """Parse the CSV once at import time into {plan_id: {..., "rows": [...]}}."""
    with DATA_PATH.open(newline="") as f:
        raw = list(csv.DictReader(f))

    plans = {}
    for r in raw:
        pid = r["plan_id"]
        if pid not in plans:
            race, _, level = r["plan_name"].partition(" - ")
            plans[pid] = {
                "plan_id": pid,
                "name": r["plan_name"],
                "race": race,
                "level": r["level"] or level,
                "rows": [],
            }
        plans[pid]["rows"].append({
            "week": int(r["week"]),
            "day_offset": int(r["day_offset"]),
            "session_type": r["session_type"],
            "planned_distance_km": float(r["planned_distance_km"]) if r["planned_distance_km"] else None,
            "planned_duration_min": float(r["planned_duration_min"]) if r["planned_duration_min"] else None,
            "notes": r["notes"],
        })

    for p in plans.values():
        p["rows"].sort(key=lambda r: (r["week"], r["day_offset"]))   # defensive: don't trust file order
        p["weeks"] = max(r["week"] for r in p["rows"])
        race_day = next((r for r in p["rows"] if r["session_type"] == "RACE DAY"), p["rows"][-1])
        p["race_distance_km"] = race_day["planned_distance_km"]
        p["race_duration_min"] = race_day["planned_duration_min"]
        p["race_day_offset"] = race_day["day_offset"]   # days after that week's Monday
    return plans


_TEMPLATES = _load()


def _sort_key(p):
    race_i = RACE_ORDER.index(p["race"]) if p["race"] in RACE_ORDER else len(RACE_ORDER)
    level_i = LEVEL_ORDER.index(p["level"]) if p["level"] in LEVEL_ORDER else len(LEVEL_ORDER)
    return (race_i, level_i)


def default_start_monday(today=None):
    """The coming Monday - today itself, if today already is one."""
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7)


def monday_of(d):
    """The Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def list_templates(today=None):
    """Summary rows for the picker, each with a race-day preview against the default start date."""
    monday = default_start_monday(today)
    out = []
    for p in sorted(_TEMPLATES.values(), key=_sort_key):
        race_date = monday + timedelta(weeks=p["weeks"] - 1, days=p["race_day_offset"])
        out.append({
            "plan_id": p["plan_id"], "name": p["name"], "race": p["race"], "level": p["level"],
            "weeks": p["weeks"], "race_distance_km": p["race_distance_km"],
            "race_duration_min": p["race_duration_min"], "race_day_offset": p["race_day_offset"],
            "default_start_date": monday.isoformat(), "default_race_date": race_date.isoformat(),
        })
    return out


def build_rows(plan_id, start_date, user_id):
    """Real, dated plan rows for this template starting on the Monday of start_date's week - the
    same shape planparse.parse_plan() produces, so the caller can save them the same way.
    Raises KeyError if plan_id is unknown."""
    template = _TEMPLATES[plan_id]
    monday = monday_of(start_date)
    rows = []
    for i, t in enumerate(template["rows"]):
        actual_date = monday + timedelta(weeks=t["week"] - 1, days=t["day_offset"])
        sport = SESSION_TYPE_SPORT[t["session_type"]]
        group, _known = sports.group_from_plan(sport, t["session_type"])
        rows.append({
            "date": actual_date.isoformat(),
            "session_type": t["session_type"],
            "sport": sport,
            "sport_group": group,
            "planned_distance_km": t["planned_distance_km"],
            "planned_duration_min": t["planned_duration_min"],
            "notes": t["notes"],
            "position": i,
            "user_id": user_id,
        })
    return rows
