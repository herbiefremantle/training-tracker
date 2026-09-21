"""Shapes matched sessions into the week card and the month calendar."""
from collections import defaultdict
from datetime import timedelta

from . import matching, metrics

PLANNED_STATUSES = ("done", "over", "under", "missed", "pending", "upcoming")


def week_view(matched, monday, today):
    """Seven days (Mon-Sun) of planned sessions and unplanned extras, with status counts.
    `matched` is the output of matching.match() covering at least that week."""
    sunday = monday + timedelta(days=6)
    lo, hi = monday.isoformat(), sunday.isoformat()
    sessions = [s for s in matched["sessions"] if lo <= s["date"] <= hi]
    extras = [e for e in matched["extras"] if lo <= e["date"] <= hi]

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        iso = d.isoformat()
        items = [dict(kind="planned", **s) for s in sessions if s["date"] == iso]
        items += [dict(kind="extra", **e) for e in extras if e["date"] == iso]
        days.append({"date": iso, "weekday": d.strftime("%a"), "is_today": d == today, "items": items})

    counts = {k: sum(1 for s in sessions if s["status"] == k) for k in PLANNED_STATUSES}
    counts["extra"] = len(extras)
    counts["planned"] = sum(counts[k] for k in PLANNED_STATUSES)
    counts["completed"] = counts["done"] + counts["over"] + counts["under"]

    # every activity is either matched to a session or an extra, so together they are all of the week's training
    done_acts = [s["activity"] for s in sessions if s["activity"]] + extras
    totals = {
        "distance_km": round(sum(a["distance_km"] or 0 for a in done_acts), 2),
        "minutes": round(sum(a["duration_min"] or 0 for a in done_acts), 1),
        "planned_distance_km": round(sum(s["planned_distance_km"] or 0 for s in sessions), 2),
        "planned_minutes": round(sum(s["planned_duration_min"] or 0 for s in sessions)),
    }
    return {
        "start": lo, "end": hi, "is_current": monday == metrics.week_start(today), "totals": totals,
        "prev": (monday - timedelta(days=7)).isoformat(), "next": (monday + timedelta(days=7)).isoformat(),
        "days": days, "counts": counts, "tolerance_min": matching.DURATION_TOLERANCE_MIN,
    }


def month_grid(first):
    """(first Monday, last Sunday) of the calendar grid that contains the month starting at `first`."""
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return metrics.week_start(first), last + timedelta(days=6 - last.weekday()), last


def calendar_view(matched, first, today):
    """A Monday-first month grid; each day lists its planned sessions (with status) and extras."""
    grid_start, grid_end, last = month_grid(first)
    by_day = defaultdict(lambda: {"sessions": [], "extras": []})
    for s in matched["sessions"]:
        if s["status"] == "rest":
            continue
        by_day[s["date"]]["sessions"].append({
            "status": s["status"], "label": s["session_type"] or s["sport_label"],
            "sport": s["sport_label"], "diff": s["duration_diff_min"]})
    for e in matched["extras"]:
        by_day[e["date"]]["extras"].append({"label": e["name"], "sport": e["sport_label"]})

    days, counts, d = [], {"done": 0, "over": 0, "under": 0, "missed": 0, "extra": 0}, grid_start
    while d <= grid_end:
        iso = d.isoformat()
        cell = by_day.get(iso, {"sessions": [], "extras": []})
        in_month = d.month == first.month
        if in_month:
            for s in cell["sessions"]:
                if s["status"] in counts:
                    counts[s["status"]] += 1
            counts["extra"] += len(cell["extras"])
        days.append({"date": iso, "day": d.day, "in_month": in_month, "is_today": d == today, **cell})
        d += timedelta(days=1)

    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = last + timedelta(days=1)
    return {
        "month": first.strftime("%Y-%m"), "label": first.strftime("%B %Y"),
        "prev": prev_month.strftime("%Y-%m"), "next": next_month.strftime("%Y-%m"),
        "is_current": (first.year, first.month) == (today.year, today.month),
        "days": days, "counts": counts,
    }
