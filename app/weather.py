"""Weather for planning fuel and fluids, from Open-Meteo (open-meteo.com): no account or key, free for
NON-COMMERCIAL use only - if this app ever starts charging, Open-Meteo's own commercial plan is needed first.
Their data is CC BY 4.0, so the pages that show it carry a credit.

Two calls, both with only a place name or rounded coordinates - never anything else about the person:
  * places(): turns the town someone typed into a handful of matches to choose from;
  * hourly_table(): the hourly temperature and humidity around a point (the last couple of days and the next
    fortnight), kept for an hour so a page full of long runs costs one request, not one each.
Every failure comes back as "no weather" (or a WeatherError the caller shows) - a slow or missing forecast must
never stop the dashboard loading.
"""
import threading
import time
from datetime import datetime

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
PAST_DAYS, FORECAST_DAYS = 2, 16       # what the forecast service reaches either side of today
CACHE_SECONDS = 3600
FAILURE_CACHE_SECONDS = 120            # after a failure, don't ask again for a couple of minutes

_cache = {}                            # (lat, lon) -> (fetched_at, table)
_cache_guard = threading.Lock()


class WeatherError(Exception):
    pass


def make_client():
    return httpx.Client(timeout=4.0, headers={"User-Agent": "TrainingTracker"})


def places(query):
    """[{label, lat, lon}] for a typed place name (up to 5), or WeatherError."""
    query = (query or "").strip()
    if len(query) < 2:
        raise WeatherError("Type at least two letters of the town or place.")
    try:
        with make_client() as http:
            r = http.get(GEOCODE_URL, params={"name": query[:80], "count": 5, "language": "en", "format": "json"})
            r.raise_for_status()
            found = r.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        raise WeatherError("Couldn't reach the place search - try again in a moment.")
    out = []
    for p in found:
        label = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)
        out.append({"label": label, "lat": round(p["latitude"], 2), "lon": round(p["longitude"], 2)})
    return out


def hourly_table(lat, lon, now=None):
    """{"YYYY-MM-DD": {hour: (temp_c, humidity_pct)}} in the place's own time, or {} if unavailable."""
    key = (round(lat, 2), round(lon, 2))
    now = now if now is not None else time.time()
    with _cache_guard:
        hit = _cache.get(key)
        if hit and now - hit[0] < (CACHE_SECONDS if hit[1] else FAILURE_CACHE_SECONDS):
            return hit[1]
    table = {}
    try:
        with make_client() as http:
            r = http.get(FORECAST_URL, params={
                "latitude": key[0], "longitude": key[1], "hourly": "temperature_2m,relative_humidity_2m",
                "past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS, "timezone": "auto"})
            r.raise_for_status()
            h = r.json()["hourly"]
        for stamp, temp, rh in zip(h["time"], h["temperature_2m"], h["relative_humidity_2m"]):
            if temp is None:
                continue
            when = datetime.fromisoformat(stamp)
            table.setdefault(when.date().isoformat(), {})[when.hour] = (temp, rh)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        table = {}
    with _cache_guard:
        _cache[key] = (now, table)
    return table


def conditions(table, day_iso, start_hour, hours):
    """Average temperature (and humidity) over the run - from `start_hour` for `hours` - or None if the forecast
    doesn't cover that day."""
    day = table.get(day_iso)
    if not day:
        return None
    span = max(1, round(hours or 3))
    samples = [day[h] for h in range(start_hour, min(24, start_hour + span)) if h in day]
    if not samples:
        return None
    temps = [t for t, _ in samples]
    rhs = [r for _, r in samples if r is not None]
    return {"temp_c": round(sum(temps) / len(temps), 1), "humidity_pct": round(sum(rhs) / len(rhs)) if rhs else None}
