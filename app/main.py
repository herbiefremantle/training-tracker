import ipaddress
import logging
import os
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal, Optional
from urllib.parse import urlencode, urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, clock, matching, metrics, planparse, sports, strava, views
from .db import ROOT, connect, db_path, get_meta, init_db, volume_warning

# override=True: .env is the source of truth, even if the shell already exports (stale/empty) STRAVA_* vars
load_dotenv(ROOT / ".env", override=True)


log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app):
    auth.validate_config()          # refuses to start with a missing/short password when a login is required
    init_db()
    log.info("Database: %s", db_path())
    if volume_warning():
        log.warning(volume_warning())
    log.info("Login: %s", "required" if auth.enabled() else "OFF (no APP_PASSWORD set)")
    yield


app = FastAPI(title="Training Tracker", lifespan=lifespan)


RAILWAY_HEALTHCHECK_HOST = "healthcheck.railway.app"   # the Host header Railway's healthcheck sends


def _extra_hosts():
    """Public names this deployment answers to: Railway's healthcheck host, the Railway-provided public domain,
    and anything listed in ALLOWED_HOSTS (comma-separated; use it for a custom domain). Read on every request
    so a variable change needs only a restart."""
    names = {RAILWAY_HEALTHCHECK_HOST}
    for raw in (os.environ.get("ALLOWED_HOSTS", "") + "," + os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")).split(","):
        raw = raw.strip()
        if raw:
            names.add((urlsplit(raw if "//" in raw else "//" + raw).hostname or "").lower())   # tolerate https://
    return names


def host_allowed(host_header):
    """True for localhost, private-network IP addresses (home Wi-Fi: 192.168.x.x, 10.x.x.x, ...), mDNS names like
    my-mac.local, and the explicitly configured public names above. Any other name is refused, which is what stops
    a DNS-rebinding attack from a website pointing its own domain at this app."""
    try:
        name = urlsplit("//" + (host_header or "")).hostname or ""
    except ValueError:
        return False
    if name in ("localhost", "testserver") or name.endswith(".local") or name in _extra_hosts():
        return True
    try:
        return ipaddress.ip_address(name).is_private
    except ValueError:
        return False


@app.middleware("http")
async def require_login(request, call_next):
    """With APP_PASSWORD set, everything except the public paths needs a valid session cookie."""
    path = request.url.path
    if not auth.enabled() or path in auth.PUBLIC_PATHS or auth.request_authenticated(request):
        return await call_next(request)
    if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "Login required"}, status_code=401)      # fetch() calls and form posts
    return RedirectResponse("/login" + ("?" + urlencode({"next": path}) if path != "/" else ""), status_code=303)


@app.middleware("http")
async def check_host(request, call_next):
    if not host_allowed(request.headers.get("host")):
        return PlainTextResponse("Invalid host header", status_code=400)
    return await call_next(request)

_oauth_state = {"value": None}
_sync_lock = threading.Lock()


app.include_router(auth.router)


# ---- health (Railway healthcheck) -----------------------------------------------------------

@app.get("/health", include_in_schema=False)
def health():
    """200 when the app is up and its database answers; 503 otherwise, so Railway won't route traffic to a broken deploy."""
    try:
        with connect() as conn:
            conn.execute("SELECT 1")
    except sqlite3.Error:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---- pages / auth ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/auth/login", include_in_schema=False)
def auth_login():
    if not strava.is_configured():
        return RedirectResponse("/?" + urlencode({"auth_error": "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first."}))
    _oauth_state["value"] = secrets.token_urlsafe(24)
    return RedirectResponse(strava.authorize_url(_oauth_state["value"]))


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(code: str = "", state: str = "", scope: str = "", error: str = ""):
    def fail(msg):
        return RedirectResponse("/?" + urlencode({"auth_error": msg}))

    expected, _oauth_state["value"] = _oauth_state["value"], None   # single use
    if error:
        return fail("Strava authorisation was cancelled (%s)." % error)
    if not expected or not secrets.compare_digest(state.encode(), expected.encode()):
        return fail("Login state didn't match - start again from the Connect button.")
    if "activity:read" not in scope:
        return fail("Strava didn't grant activity access. Tick 'View data about your activities' and try again.")
    try:
        with connect() as conn, strava.make_client() as http:
            strava.exchange_code(conn, http, code, scope)
    except (strava.StravaError, httpx.HTTPError) as e:
        return fail(str(e) or "Couldn't reach Strava.")
    return RedirectResponse("/?connected=1")


# ---- status / sync --------------------------------------------------------------------------

@app.get("/api/status")
def status():
    with connect() as conn:
        token = conn.execute("SELECT athlete_name, athlete_id, scope FROM auth WHERE id = 1").fetchone()
        return {
            "configured": strava.is_configured(),
            "redirect_uri": strava.redirect_uri(),
            "connected": token is not None,
            "athlete": token["athlete_name"] if token else None,
            "last_sync": get_meta(conn, "last_sync"),
            "activity_count": conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
            "plan_count": conn.execute("SELECT COUNT(*) FROM plan").fetchone()[0],
            "auth_enabled": auth.enabled(),
        }


@app.post("/api/sync")
def sync():
    if not strava.is_configured():
        raise HTTPException(400, "Strava credentials aren't configured (see .env).")
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running.")
    try:
        with connect() as conn, strava.make_client() as http:
            if not conn.execute("SELECT 1 FROM auth").fetchone():
                raise HTTPException(400, "Not connected to Strava yet.")
            return strava.sync(conn, http)
    except strava.StravaError as e:
        raise HTTPException(502, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach Strava - check your connection.")
    finally:
        _sync_lock.release()


# ---- plan -----------------------------------------------------------------------------------

class PlanImport(BaseModel):
    text: str = Field(max_length=1_000_000)
    day_first: bool = True
    mode: Literal["replace_dates", "replace_all"] = "replace_dates"
    dry_run: bool = False
    distance_unit: Literal["km", "mi"] = "km"   # for distances written without a unit


@app.post("/api/plan/import")
def plan_import(body: PlanImport):
    result = planparse.parse_plan(body.text, body.day_first, body.distance_unit)
    rows = result["rows"]
    result["saved"] = 0
    result["dry_run"] = body.dry_run
    if body.dry_run or not rows:
        return result
    with connect() as conn:
        if body.mode == "replace_all":
            conn.execute("DELETE FROM plan")
        else:
            conn.executemany("DELETE FROM plan WHERE date = ?", [(d,) for d in {r["date"] for r in rows}])
        conn.executemany(
            "INSERT INTO plan (date, session_type, sport, sport_group, planned_distance_km, "
            "planned_duration_min, notes, position) VALUES (:date, :session_type, :sport, :sport_group, "
            ":planned_distance_km, :planned_duration_min, :notes, :position)", rows)
    result["saved"] = len(rows)
    return result


@app.delete("/api/plan")
def plan_clear():
    with connect() as conn:
        n = conn.execute("DELETE FROM plan").rowcount
    return {"deleted": n}


@app.get("/api/plan")
def plan_list():
    """The whole plan, each session with its match status against Strava activities."""
    today = clock.today().isoformat()
    with connect() as conn:
        plans = conn.execute("SELECT * FROM plan").fetchall()
        if not plans:
            return {"sessions": []}
        lo, hi = min(p["date"] for p in plans), max(p["date"] for p in plans)
        acts = conn.execute("SELECT * FROM activities WHERE date BETWEEN ? AND ?", (lo, hi)).fetchall()
    return {"sessions": matching.match(plans, acts, today)["sessions"]}


# ---- dashboard ------------------------------------------------------------------------------

def _groups_for(sport):
    if sport == "all":
        return None
    if sport == "foot":
        return set(sports.FOOT_GROUPS)
    return {sport}


def _matched(conn, lo, hi, today):
    """Plan vs activities for lo..hi (inclusive dates)."""
    span = (lo.isoformat(), hi.isoformat())
    plans = conn.execute("SELECT * FROM plan WHERE date BETWEEN ? AND ?", span).fetchall()
    acts = conn.execute("SELECT * FROM activities WHERE date BETWEEN ? AND ?", span).fetchall()
    return matching.match(plans, acts, today.isoformat())


@app.get("/api/dashboard")
def dashboard():
    today = clock.today()
    monday = metrics.week_start(today)
    horizon = max(monday + timedelta(days=6), today + timedelta(days=6))
    with connect() as conn:
        matched = _matched(conn, monday, horizon, today)
        # 90-day chart + the 28-day window behind its first point
        recent = conn.execute("SELECT * FROM activities WHERE date >= ?",
                              ((today - timedelta(days=120)).isoformat(),)).fetchall()
        present = [r[0] for r in conn.execute(
            "SELECT DISTINCT sport_group FROM activities WHERE sport_group != '' ORDER BY 1")]
    end = (today + timedelta(days=6)).isoformat()
    options = [{"value": "all", "label": "All sports"}]
    if {"run", "hike"} <= set(present):
        options.append({"value": "foot", "label": sports.label("foot")})
    options += [{"value": g, "label": sports.label(g)} for g in present]
    return {
        "today": today.isoformat(),
        "week": views.week_view(matched, monday, today),
        "upcoming": [s for s in matched["sessions"] if today.isoformat() <= s["date"] <= end and s["status"] != "rest"],
        "load": metrics.load_summary(recent, today),
        "thresholds": {"high": metrics.HIGH_RATIO, "low": metrics.LOW_RATIO},
        "sport_options": options,
        "default_sport": "run" if "run" in present else "all",
        "has_activities": bool(present),
    }


@app.get("/api/week")
def week(start: Optional[date] = None):
    """Planned-vs-actual for the Mon-Sun week containing `start` (default: this week)."""
    today = clock.today()
    monday = metrics.week_start(start or today)
    with connect() as conn:
        matched = _matched(conn, monday, monday + timedelta(days=6), today)
    return views.week_view(matched, monday, today)


@app.get("/api/calendar")
def calendar(month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Month grid (YYYY-MM, default this month): one status dot per planned session."""
    today = clock.today()
    first = date(int(month[:4]), int(month[5:]), 1) if month else today.replace(day=1)
    grid_start, grid_end, _ = views.month_grid(first)
    with connect() as conn:
        matched = _matched(conn, grid_start, grid_end, today)
    return views.calendar_view(matched, first, today)


@app.get("/api/explore")
def explore(scope: Literal["year", "month", "week"] = "year", anchor: Optional[date] = None,
            sport: str = Query("all", pattern=r"^[a-z0-9_]{1,40}$")):
    """Distance / elevation / pace summary of a year (weekly buckets), month or week (daily buckets)."""
    today = clock.today()
    anchor = anchor or today
    start, end, _ = metrics.period(scope, anchor)
    with connect() as conn:
        acts = conn.execute("SELECT * FROM activities WHERE date BETWEEN ? AND ?",
                            (start.isoformat(), end.isoformat())).fetchall()
    return {**metrics.explore(acts, scope, anchor, today, _groups_for(sport)), "sport": sport}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
