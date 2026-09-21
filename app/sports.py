"""Sport normalisation.

Planned sessions and Strava activities are matched on (date, sport *group*), so
"Run"/"TrailRun"/"running" all land in "run", and "WeightTraining"/"Gym" in "gym".
"""
import re

REST = "rest"

# Strava sport_type -> group. Anything not listed becomes its own lowercase group.
STRAVA_GROUPS = {
    "run": "run", "trailrun": "run", "virtualrun": "run",
    "ride": "ride", "mountainbikeride": "ride", "gravelride": "ride", "ebikeride": "ride",
    "emountainbikeride": "ride", "virtualride": "ride", "handcycle": "ride", "velomobile": "ride",
    "swim": "swim",
    "walk": "hike", "hike": "hike",
    "weighttraining": "gym", "workout": "gym", "crossfit": "gym",
    "highintensityintervaltraining": "gym",
    "yoga": "yoga", "pilates": "yoga",
}

# Free-text words people use in a plan's sport column -> group.
PLAN_ALIASES = {
    "run": "run", "running": "run", "trailrun": "run", "trailrunning": "run", "trail": "run",
    "jog": "run", "jogging": "run", "ultra": "run", "treadmill": "run",
    "ride": "ride", "bike": "ride", "cycling": "ride", "cycle": "ride", "biking": "ride",
    "spin": "ride", "mtb": "ride", "turbo": "ride", "zwift": "ride", "gravel": "ride",
    "swim": "swim", "swimming": "swim",
    "hike": "hike", "hiking": "hike", "walk": "hike", "walking": "hike", "powerhike": "hike",
    "gym": "gym", "strength": "gym", "weights": "gym", "weighttraining": "gym", "workout": "gym",
    "crossfit": "gym", "hiit": "gym", "sc": "gym", "resistance": "gym", "lifting": "gym",
    "yoga": "yoga", "pilates": "yoga",
    "rest": REST, "off": REST, "restday": REST,
}

GROUP_LABELS = {
    "run": "Run", "ride": "Ride", "swim": "Swim", "hike": "Walk / Hike",
    "gym": "Gym", "yoga": "Yoga / Pilates", "foot": "Run + Walk/Hike",
}

# Groups that count for the "foot" filter used on the trend charts.
FOOT_GROUPS = ("run", "hike")


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def group_from_strava(sport_type):
    key = _squash(sport_type)
    return STRAVA_GROUPS.get(key, key or "other")


def group_from_plan(sport, session_type=""):
    """Return (group, recognised). Falls back to keywords in the session type when the
    sport column is empty, e.g. session type 'Long run' -> run."""
    for i, text in enumerate((sport, session_type)):
        if not (text or "").strip():
            continue
        whole = _squash(text)
        if whole in PLAN_ALIASES:
            return PLAN_ALIASES[whole], True
        if whole in STRAVA_GROUPS:
            return STRAVA_GROUPS[whole], True
        for token in re.split(r"[^a-z0-9]+", text.lower()):
            if token in PLAN_ALIASES:
                return PLAN_ALIASES[token], True
        if i == 0:  # explicit but unknown sport: keep it, flag it
            return whole, False
    return "", False


def label(group):
    return GROUP_LABELS.get(group, group.replace("_", " ").title())
