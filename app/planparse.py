"""Parse a pasted / uploaded training plan (CSV, TSV or semicolon-separated text).

Columns: date, session type, sport, planned distance, planned duration, notes.
A header row is detected and columns may be in any order; without a header the columns are
read positionally in the order above.
"""
import csv
import io
import re
from datetime import datetime

from . import sports

FIELDS = ["date", "session_type", "sport", "distance", "duration", "notes"]

HEADER_ALIASES = {
    "date": "date", "when": "date",
    "sessiontype": "session_type", "session": "session_type", "type": "session_type",
    "workout": "session_type", "sessionname": "session_type",
    "sport": "sport", "activity": "sport", "discipline": "sport", "activitytype": "sport",
    "sporttype": "sport",
    "planneddistance": "distance", "distance": "distance", "dist": "distance", "km": "distance",
    "distancekm": "distance", "distancemi": "distance", "plannedkm": "distance",
    "plannedduration": "duration", "duration": "duration", "time": "duration",
    "plannedtime": "duration", "mins": "duration", "minutes": "duration",
    "notes": "notes", "note": "notes", "comments": "notes", "description": "notes",
}

MISSING = {"", "-", "--", "n/a", "na", "none", "tbc", "tbd"}

ISO_PREFIX = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:$|[T\s])")
DAY_FIRST = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"]
MONTH_FIRST = ["%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%m/%d/%y", "%m-%d-%y", "%m.%d.%y"]
NAMED = ["%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d %b %y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"]


def parse_date(text, day_first=True):
    s = (text or "").strip()
    m = ISO_PREFIX.match(s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            raise ValueError("invalid date '%s'" % text)
    s = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*[,.\s]+", "", s, flags=re.I)  # drop weekday name
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in (DAY_FIRST if day_first else MONTH_FIRST) + NAMED:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("can't read date '%s'" % text)


_DIST = re.compile(r"^(\d+(?:[.,]\d+)?|[.,]\d+)\s*([a-z]*)\.?$", re.I)
_UNITS = {"": None, "km": 1.0, "kms": 1.0, "k": 1.0, "kilometer": 1.0, "kilometers": 1.0,
          "kilometre": 1.0, "kilometres": 1.0,
          "mi": 1.609344, "mile": 1.609344, "miles": 1.609344,
          "m": 0.001, "meter": 0.001, "meters": 0.001, "metre": 0.001, "metres": 0.001}


def parse_distance_km(text, default_unit="km"):
    s = (text or "").strip().lower()
    if s in MISSING:
        return None
    m = _DIST.match(s)
    if not m or m.group(2) not in _UNITS:
        raise ValueError("can't read distance '%s'" % text)
    factor = _UNITS[m.group(2)]
    if factor is None:
        factor = 1.609344 if default_unit == "mi" else 1.0
    return round(float(m.group(1).replace(",", ".")) * factor, 3)


_HMS = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
_HM = re.compile(r"^(\d+):(\d{2})$")
_HOURS = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)$")
_HMIN = re.compile(r"^(?:(\d+)\s*(?:h|hr|hrs|hour|hours)\s*)?(?:(\d+)\s*(?:m|min|mins|minute|minutes|')?)?$")


def parse_duration_min(text):
    """'90', '90min', '1h30', '1h 30m', '1:30' (h:mm), '1:30:00', '1.5h' -> minutes."""
    s = (text or "").strip().lower()
    if s in MISSING:
        return None
    m = _HMS.match(s)
    if m:
        return round(int(m[1]) * 60 + int(m[2]) + int(m[3]) / 60, 2)
    m = _HM.match(s)
    if m:
        return float(int(m[1]) * 60 + int(m[2]))
    m = _HOURS.match(s)
    if m:
        return round(float(m[1]) * 60, 2)
    if re.match(r"^\d+(\.\d+)?$", s):
        return float(s)  # bare number = minutes
    m = _HMIN.match(s)
    if m and (m[1] or m[2]):
        return float(int(m[1] or 0) * 60 + int(m[2] or 0))
    raise ValueError("can't read duration '%s'" % text)


def _norm_header(cell):
    return re.sub(r"[^a-z0-9]", "", cell.lower())


def _split(text):
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    counts = {d: first.count(d) for d in ("\t", ";", ",")}
    delim = max(counts, key=counts.get) if any(counts.values()) else ","
    if counts["\t"]:
        delim = "\t"
    return list(csv.reader(io.StringIO(text), delimiter=delim)), delim


def parse_plan(text, day_first=True, distance_unit="km"):
    """Return {"rows": [...], "errors": [...], "warnings": [...]}. Bad rows are reported, not fatal."""
    text = (text or "").lstrip("﻿")
    rows, errors, warnings = [], [], []
    raw, delim = _split(text)
    raw = [(i + 1, r) for i, r in enumerate(raw) if any(c.strip() for c in r)]
    if not raw:
        return {"rows": [], "errors": ["Nothing to import - the plan is empty."], "warnings": []}

    mapping, dist_unit = dict(zip(range(len(FIELDS)), FIELDS)), distance_unit
    first_no, first = raw[0]
    if "date" in {HEADER_ALIASES.get(_norm_header(c)) for c in first}:
        mapping = {}
        for idx, cell in enumerate(first):
            field = HEADER_ALIASES.get(_norm_header(cell))
            if field and field not in mapping.values():
                mapping[idx] = field
            elif cell.strip():
                warnings.append("Ignoring unrecognised column '%s'." % cell.strip())
            if field == "distance":   # a unit in the header ("Distance (mi)") beats the default
                if re.search(r"\b(mi|miles?)\b", cell.lower()):
                    dist_unit = "mi"
                elif re.search(r"\b(km|kms|kilomet\w+)\b", cell.lower()):
                    dist_unit = "km"
        raw = raw[1:]
        if "sport" not in mapping.values() and "session_type" not in mapping.values():
            return {"rows": [], "errors": ["Couldn't find a 'sport' or 'session type' column in the header."],
                    "warnings": warnings}

    for line_no, cells in raw:
        rec = {f: "" for f in FIELDS}
        for idx, field in mapping.items():
            if idx < len(cells):
                rec[field] = cells[idx].strip()
        last = max(mapping)
        if mapping[last] == "notes" and len(cells) > last + 1:
            # unquoted delimiter inside the notes column: keep the whole note rather than truncating it
            rec["notes"] = (delim if delim != "," else ", ").join(c.strip() for c in cells[last:]).strip()
        try:
            iso = parse_date(rec["date"], day_first)
            group, known = sports.group_from_plan(rec["sport"], rec["session_type"])
            if not group:
                raise ValueError("no sport given (and none could be inferred from the session type)")
            dist, dur = parse_distance_km(rec["distance"], dist_unit), parse_duration_min(rec["duration"])
            if not known and group != sports.REST:   # only for rows we keep
                warnings.append("Row %d: unrecognised sport '%s' - it will only match Strava activities "
                                "whose sport_type is '%s'." % (line_no, rec["sport"], rec["sport"]))
            rows.append({
                "date": iso,
                "session_type": rec["session_type"],
                "sport": rec["sport"] or rec["session_type"],
                "sport_group": group,
                "planned_distance_km": dist,
                "planned_duration_min": dur,
                "notes": rec["notes"],
                "position": len(rows),
            })
        except ValueError as e:
            errors.append("Row %d: %s" % (line_no, e))
    return {"rows": rows, "errors": errors, "warnings": warnings}
