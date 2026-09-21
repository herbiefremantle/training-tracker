"""Match planned sessions to Strava activities.

The key is (date, sport group). Within one key there can be several planned sessions and several
activities (two runs in a day); they are paired one-to-one, best fit first, so a planned session is
never "done" twice and an activity never satisfies two sessions. Anything left over is:
  * planned, unmatched  -> missed (past) / pending (today) / upcoming (future)
  * activity, unmatched -> extra
Different sports on the same day are matched entirely independently.

A matched session is "done" (on target) when its moving time is within DURATION_TOLERANCE_MIN of the
planned duration; more than that longer is "over", more than that shorter is "under". Sessions with no
planned duration can't be judged and are simply "done".
"""
from collections import defaultdict

from . import sports

DURATION_TOLERANCE_MIN = 10


def activity_view(a):
    dist_km = (a["distance"] or 0) / 1000.0
    return {
        "id": a["id"], "date": a["date"], "name": a["name"], "sport_type": a["sport_type"],
        "sport_group": a["sport_group"],
        "distance_km": round(dist_km, 2) if dist_km else None,
        "duration_min": round((a["moving_time"] or 0) / 60.0, 1) or None,
        "elevation_m": a["total_elevation_gain"],
        "average_heartrate": a["average_heartrate"],
        "start_epoch": a["start_epoch"],
    }


def _rel_diff(planned, actual):
    return abs(planned - actual) / max(planned, actual)


def _cost(plan, act):
    """Lower = better fit. Compares distance and/or duration where both sides have them."""
    parts = []
    if plan["planned_distance_km"] and act["distance_km"]:
        parts.append(_rel_diff(plan["planned_distance_km"], act["distance_km"]))
    if plan["planned_duration_min"] and act["duration_min"]:
        parts.append(_rel_diff(plan["planned_duration_min"], act["duration_min"]))
    return sum(parts) / len(parts) if parts else 0.5   # no basis to prefer one: fall back to order


def _pair(plans, acts):
    """Greedy best-fit one-to-one pairing. Ties resolve by plan row order vs chronological order."""
    pairs = sorted(
        ((_cost(p, a), pi, ai) for pi, p in enumerate(plans) for ai, a in enumerate(acts)))
    used_p, used_a, out = set(), set(), {}
    for _, pi, ai in pairs:
        if pi in used_p or ai in used_a:
            continue
        used_p.add(pi)
        used_a.add(ai)
        out[pi] = ai
    return out


def _completion(plan, act):
    if plan["planned_distance_km"] and act["distance_km"]:
        return round(100 * act["distance_km"] / plan["planned_distance_km"])
    if plan["planned_duration_min"] and act["duration_min"]:
        return round(100 * act["duration_min"] / plan["planned_duration_min"])
    return None


def _adherence(plan, act):
    """(status, minutes actual is longer(+)/shorter(-) than planned, or None if it can't be judged)."""
    planned, actual = plan["planned_duration_min"], act["duration_min"]
    if not planned or actual is None:
        return "done", None
    diff = actual - planned
    if diff > DURATION_TOLERANCE_MIN:
        status = "over"
    elif diff < -DURATION_TOLERANCE_MIN:
        status = "under"
    else:
        status = "done"
    return status, round(diff)


def match(plan_rows, activity_rows, today):
    """today: 'YYYY-MM-DD'. Returns {"sessions": [...], "extras": [...]}, both sorted by date."""
    plans_by_key, acts_by_key = defaultdict(list), defaultdict(list)
    for p in sorted(plan_rows, key=lambda r: (r["date"], r["position"], r["id"])):
        plans_by_key[(p["date"], p["sport_group"])].append(dict(p))
    for a in sorted(activity_rows, key=lambda r: (r["date"], r["start_epoch"])):
        acts_by_key[(a["date"], a["sport_group"])].append(activity_view(a))

    sessions, extras = [], []
    for key in sorted(set(plans_by_key) | set(acts_by_key)):
        plans, acts = plans_by_key.get(key, []), acts_by_key.get(key, [])
        pairing = _pair(plans, acts) if plans and acts else {}
        matched_acts = set(pairing.values())
        for pi, p in enumerate(plans):
            p["sport_label"] = sports.label(p["sport_group"])
            p["activity"], p["completion_pct"], p["duration_diff_min"] = None, None, None
            if p["sport_group"] == sports.REST:
                p["status"] = "rest"
            elif pi in pairing:
                p["activity"] = acts[pairing[pi]]
                p["completion_pct"] = _completion(p, p["activity"])
                p["status"], p["duration_diff_min"] = _adherence(p, p["activity"])
            else:
                p["status"] = "missed" if p["date"] < today else "pending" if p["date"] == today else "upcoming"
            sessions.append(p)
        for ai, a in enumerate(acts):
            if ai not in matched_acts:
                a["status"] = "extra"
                a["sport_label"] = sports.label(a["sport_group"])
                extras.append(a)
    sessions.sort(key=lambda s: (s["date"], s["position"], s["id"]))
    extras.sort(key=lambda a: (a["date"], a["start_epoch"]))
    return {"sessions": sessions, "extras": extras}
