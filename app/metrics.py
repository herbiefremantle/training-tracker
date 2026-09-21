"""Training load, load ratio, and the year / month / week drill-down summaries."""
from collections import defaultdict
from datetime import date, timedelta

from . import sports

HIGH_RATIO = 1.5
LOW_RATIO = 0.8


def activity_load(a):
    """(load, source). suffer_score when Strava has one; otherwise duration_min * (avg_hr / 100).

    Note the two aren't on the same scale (suffer score is Strava's Relative Effort), so a mix of
    sources within the window will make the ratio less meaningful - the source counts are reported
    so the dashboard can say so. Activities with neither a score nor heart rate count as 0."""
    if a["suffer_score"]:
        return float(a["suffer_score"]), "suffer_score"
    if a["average_heartrate"] and a["moving_time"]:
        return (a["moving_time"] / 60.0) * (a["average_heartrate"] / 100.0), "hr_fallback"
    return 0.0, "none"


def _d(iso):
    return date.fromisoformat(iso)


def daily_loads(activities):
    """{date: total load that day} plus per-activity source tags keyed by date."""
    loads, sources = defaultdict(float), defaultdict(list)
    for a in activities:
        load, src = activity_load(a)
        d = _d(a["date"])
        loads[d] += load
        sources[d].append(src)
    return loads, sources


def _window_avg(loads, end, days):
    return sum(loads.get(end - timedelta(days=i), 0.0) for i in range(days)) / days


def risk_flag(ratio):
    if ratio is None:
        return None
    if ratio > HIGH_RATIO:
        return "high"
    if ratio < LOW_RATIO:
        return "low"
    return "ok"


def load_summary(activities, today, chart_days=90):
    loads, sources = daily_loads(activities)
    avg7 = _window_avg(loads, today, 7)
    avg28 = _window_avg(loads, today, 28)
    ratio = round(avg7 / avg28, 2) if avg28 > 0 else None

    counts = {"suffer_score": 0, "hr_fallback": 0, "none": 0}
    for i in range(28):
        for src in sources.get(today - timedelta(days=i), []):
            counts[src] += 1

    series = []
    for i in range(chart_days - 1, -1, -1):
        d = today - timedelta(days=i)
        series.append({"date": d.isoformat(),
                       "avg7": round(_window_avg(loads, d, 7), 1),
                       "avg28": round(_window_avg(loads, d, 28), 1)})
    return {
        "avg7": round(avg7, 1), "avg28": round(avg28, 1),
        "ratio": ratio, "flag": risk_flag(ratio),
        "sources_28d": counts,
        "series": series,
    }


def week_start(d):
    return d - timedelta(days=d.weekday())   # Monday


def _in_groups(a, groups):
    return groups is None or a["sport_group"] in groups


# ---- drill-down: year -> month -> week -------------------------------------------------------------

WORKOUT_LABELS = {1: "Race", 2: "Long run", 3: "Workout", 11: "Race", 12: "Workout"}


def period(scope, anchor):
    """(start, end, label) of the year / month / Mon-Sun week containing `anchor`."""
    if scope == "year":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31), str(anchor.year)
    if scope == "month":
        start = anchor.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return start, end, start.strftime("%B %Y")
    start = week_start(anchor)
    end = start + timedelta(days=6)
    return start, end, "%d %s \u2013 %d %s" % (start.day, start.strftime("%b"), end.day, end.strftime("%b %Y"))


def _neighbours(scope, start, end):
    """Anchor dates of the previous and next period."""
    before, after = start - timedelta(days=1), end + timedelta(days=1)
    if scope == "week":
        return start - timedelta(days=7), start + timedelta(days=7)
    return period(scope, before)[0], after


def _buckets(scope, start, end, today):
    """Weekly buckets for a year (up to the current week), daily for a month or week."""
    out = []
    if scope == "year":
        stop = min(end, today)
        ws = week_start(start)
        while ws <= stop:
            out.append((max(ws, start), min(ws + timedelta(days=6), end)))
            ws += timedelta(days=7)
    else:
        d = start
        while d <= end:
            out.append((d, d))
            d += timedelta(days=1)
    return out


def activity_detail(a):
    load, source = activity_load(a)
    return {
        "id": a["id"], "date": a["date"], "name": a["name"], "sport_type": a["sport_type"],
        "sport_group": a["sport_group"], "sport_label": sports.label(a["sport_group"]),
        "workout": WORKOUT_LABELS.get(a["workout_type"], ""),
        "distance_km": round((a["distance"] or 0) / 1000.0, 2),
        "duration_min": round((a["moving_time"] or 0) / 60.0, 1),
        "elevation_m": round(a["total_elevation_gain"] or 0),
        "average_heartrate": a["average_heartrate"], "max_heartrate": a["max_heartrate"],
        "average_speed": a["average_speed"], "max_speed": a["max_speed"],
        "load": round(load), "load_source": source,
    }


def explore(activities, scope, anchor, today, groups=None):
    """Summary of the period containing `anchor`, split into buckets, plus its activities in detail.

    `activities` need only cover the period. Year buckets are weeks (clipped to the year, so an
    activity is never counted in two years); month and week buckets are days."""
    start, end, label = period(scope, anchor)
    prev_anchor, next_anchor = _neighbours(scope, start, end)
    spans = _buckets(scope, start, end, today)
    acc = [{"metres": 0.0, "elev": 0.0, "secs": 0, "n": 0, "speed_m": 0.0, "speed_s": 0} for _ in spans]

    chosen = sorted((a for a in activities if _in_groups(a, groups) and start.isoformat() <= a["date"] <= end.isoformat()),
                    key=lambda r: (r["date"], r["start_epoch"]))
    for a in chosen:
        d = _d(a["date"])
        idx = (d - week_start(start)).days // 7 if scope == "year" else (d - start).days
        if not 0 <= idx < len(acc):
            continue   # e.g. a future-dated activity in a year view that stops at the current week
        b = acc[idx]
        b["metres"] += a["distance"] or 0
        b["elev"] += a["total_elevation_gain"] or 0
        b["secs"] += a["moving_time"] or 0
        b["n"] += 1
        if a["distance"] and a["moving_time"]:
            b["speed_m"] += a["distance"]
            b["speed_s"] += a["moving_time"]

    buckets = []
    for (b_start, b_end), b in zip(spans, acc):
        km = b["metres"] / 1000.0
        buckets.append({
            "start": b_start.isoformat(), "end": b_end.isoformat(),
            "distance_km": round(km, 1), "elevation_m": round(b["elev"]), "hours": round(b["secs"] / 3600.0, 1),
            "count": b["n"], "climb_per_km": round(b["elev"] / km, 1) if km > 0 else None,
            "avg_speed": b["speed_m"] / b["speed_s"] if b["speed_s"] else None,
            "partial": b_start <= today <= b_end, "future": b_start > today,
        })
    return {
        "scope": scope, "start": start.isoformat(), "end": end.isoformat(), "label": label,
        "prev": prev_anchor.isoformat(), "next": next_anchor.isoformat(), "has_next": next_anchor <= today,
        "buckets": buckets,
        "activities": [activity_detail(a) for a in chosen],
        "totals": {"distance_km": round(sum(b["metres"] for b in acc) / 1000.0, 1),
                   "elevation_m": round(sum(b["elev"] for b in acc)),
                   "hours": round(sum(b["secs"] for b in acc) / 3600.0, 1), "count": sum(b["n"] for b in acc)},
    }
