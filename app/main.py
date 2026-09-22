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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, clock, matching, metrics, planparse, sports, strava, users, views
from .db import LOCAL_USER_ID, ROOT, connect, db_path, get_meta, init_db, volume_warning

# override=True: .env is the source of truth, even if the shell already exports (stale/empty) STRAVA_* vars
load_dotenv(ROOT / ".env", override=True)


log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app):
    init_db()                       # schema + one-time account bootstrap/migration (needs to run first)
    auth.validate_config()          # then fail closed if a login is required but still isn't possible
    log.info("Database: %s", db_path())
    if volume_warning():
        log.warning(volume_warning())
    log.info("Login: %s", "required" if auth.enabled() else "OFF (no accounts yet)")
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
    """With at least one account, everything except the public paths needs a valid session cookie. The logged-in
    account (or None, when login is off) is stashed on request.state.user for routes to read via current_user()."""
    request.state.user = None
    path = request.url.path
    if not auth.enabled() or path in auth.PUBLIC_PATHS:
        return await call_next(request)
    user = auth.resolve_user(request)
    if user is None:
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "Login required"}, status_code=401)      # fetch() calls and form posts
        return RedirectResponse("/login" + ("?" + urlencode({"next": path}) if path != "/" else ""), status_code=303)
    request.state.user = user
    return await call_next(request)


@app.middleware("http")
async def check_host(request, call_next):
    if not host_allowed(request.headers.get("host")):
        return PlainTextResponse("Invalid host header", status_code=400)
    return await call_next(request)


def current_user(request):
    """The logged-in account's row, or None when login is off (local/dev use)."""
    return request.state.user


def current_user_id(request):
    """The logged-in account's id, or the shared local id when login is off - so every query can filter by
    user_id unconditionally, in both modes."""
    user = request.state.user
    return user["id"] if user else LOCAL_USER_ID


_oauth_state = {}                    # user_id -> the random token issued for their in-flight Strava connect
_oauth_state_guard = threading.Lock()
_sync_locks = {}                     # user_id -> lock, so one account's sync can't be blocked by another's
_sync_locks_guard = threading.Lock()


def _sync_lock_for(user_id):
    with _sync_locks_guard:
        return _sync_locks.setdefault(user_id, threading.Lock())


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
def auth_login(request: Request):
    if not strava.is_configured():
        return RedirectResponse("/?" + urlencode({"auth_error": "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first."}))
    uid = current_user_id(request)
    token = secrets.token_urlsafe(24)
    with _oauth_state_guard:
        _oauth_state[uid] = token
    return RedirectResponse(strava.authorize_url(token))


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(request: Request, code: str = "", state: str = "", scope: str = "", error: str = ""):
    def fail(msg):
        return RedirectResponse("/?" + urlencode({"auth_error": msg}))

    uid = current_user_id(request)
    with _oauth_state_guard:
        expected = _oauth_state.pop(uid, None)
    if error:
        return fail("Strava authorisation was cancelled (%s)." % error)
    if not expected or not secrets.compare_digest(state.encode(), expected.encode()):
        return fail("Login state didn't match - start again from the Connect button.")
    if "activity:read" not in scope:
        return fail("Strava didn't grant activity access. Tick 'View data about your activities' and try again.")
    try:
        with connect() as conn, strava.make_client() as http:
            strava.exchange_code(conn, http, uid, code, scope)
    except (strava.StravaError, httpx.HTTPError) as e:
        return fail(str(e) or "Couldn't reach Strava.")
    return RedirectResponse("/?connected=1")


# ---- status / sync --------------------------------------------------------------------------

@app.get("/api/status")
def status(request: Request):
    uid = current_user_id(request)
    me = current_user(request)
    with connect() as conn:
        token = conn.execute("SELECT athlete_name FROM strava_auth WHERE user_id = ?", (uid,)).fetchone()
        return {
            "configured": strava.is_configured(),
            "redirect_uri": strava.redirect_uri(),
            "connected": token is not None,
            "athlete": token["athlete_name"] if token else None,
            "last_sync": get_meta(conn, uid, "last_sync"),
            "activity_count": conn.execute("SELECT COUNT(*) FROM activities WHERE user_id = ?", (uid,)).fetchone()[0],
            "plan_count": conn.execute("SELECT COUNT(*) FROM plan WHERE user_id = ?", (uid,)).fetchone()[0],
            "auth_enabled": auth.enabled(),
            "username": me["username"] if me else None,
            "is_admin": bool(me["is_admin"]) if me else False,
        }


@app.post("/api/sync")
def sync(request: Request):
    uid = current_user_id(request)
    if not strava.is_configured():
        raise HTTPException(400, "Strava credentials aren't configured (see .env).")
    lock = _sync_lock_for(uid)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running.")
    try:
        with connect() as conn, strava.make_client() as http:
            if not conn.execute("SELECT 1 FROM strava_auth WHERE user_id = ?", (uid,)).fetchone():
                raise HTTPException(400, "Not connected to Strava yet.")
            return strava.sync(conn, http, uid)
    except strava.StravaError as e:
        raise HTTPException(502, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach Strava - check your connection.")
    finally:
        lock.release()


# ---- invites (admin only) --------------------------------------------------------------------

def _require_admin(request):
    me = current_user(request)
    if not auth.enabled() or not me:
        raise HTTPException(403, "Login isn't set up.")
    if not me["is_admin"]:
        raise HTTPException(403, "Only an admin can do that.")
    return me


@app.post("/api/invites")
def create_invite(request: Request):
    me = _require_admin(request)
    with connect() as conn:
        if users.count(conn) >= users.max_users():
            raise HTTPException(400, "Already at the maximum of %d accounts (set MAX_USERS to raise it - check "
                                     "your Strava API app's athlete capacity first)." % users.max_users())
        token = users.create_invite(conn, me["id"])
    return {"token": token, "url": "/register?invite=%s" % token, "expires_in_days": users.INVITE_TTL_SECONDS // 86400}


@app.get("/api/invites")
def list_invites(request: Request):
    _require_admin(request)
    with connect() as conn:
        pending = users.pending_invites(conn)
        accounts = users.list_accounts(conn)
    return {
        "max_users": users.max_users(),
        "accounts": [{"username": a["username"], "is_admin": bool(a["is_admin"])} for a in accounts],
        "pending_invites": [{"url": "/register?invite=%s" % p["token"],
                             "expires_in_days": max(0, round((p["expires_at"] - p["created_at"]) / 86400))}
                            for p in pending],
    }


# ---- plan -----------------------------------------------------------------------------------

class PlanImport(BaseModel):
    text: str = Field(max_length=1_000_000)
    day_first: bool = True
    mode: Literal["replace_dates", "replace_all"] = "replace_dates"
    dry_run: bool = False
    distance_unit: Literal["km", "mi"] = "km"   # for distances written without a unit


@app.post("/api/plan/import")
def plan_import(body: PlanImport, request: Request):
    uid = current_user_id(request)
    result = planparse.parse_plan(body.text, body.day_first, body.distance_unit)
    rows = result["rows"]
    for r in rows:
        r["user_id"] = uid
    result["saved"] = 0
    result["dry_run"] = body.dry_run
    if body.dry_run or not rows:
        return result
    with connect() as conn:
        if body.mode == "replace_all":
            conn.execute("DELETE FROM plan WHERE user_id = ?", (uid,))
        else:
            conn.executemany("DELETE FROM plan WHERE user_id = ? AND date = ?",
                             [(uid, d) for d in {r["date"] for r in rows}])
        conn.executemany(
            "INSERT INTO plan (user_id, date, session_type, sport, sport_group, planned_distance_km, "
            "planned_duration_min, notes, position) VALUES (:user_id, :date, :session_type, :sport, :sport_group, "
            ":planned_distance_km, :planned_duration_min, :notes, :position)", rows)
    result["saved"] = len(rows)
    return result


@app.delete("/api/plan")
def plan_clear(request: Request):
    uid = current_user_id(request)
    with connect() as conn:
        n = conn.execute("DELETE FROM plan WHERE user_id = ?", (uid,)).rowcount
    return {"deleted": n}


@app.get("/api/plan")
def plan_list(request: Request):
    """The whole plan, each session with its match status against Strava activities."""
    uid = current_user_id(request)
    today = clock.today().isoformat()
    with connect() as conn:
        plans = conn.execute("SELECT * FROM plan WHERE user_id = ?", (uid,)).fetchall()
        if not plans:
            return {"sessions": []}
        lo, hi = min(p["date"] for p in plans), max(p["date"] for p in plans)
        acts = conn.execute("SELECT * FROM activities WHERE user_id = ? AND date BETWEEN ? AND ?",
                            (uid, lo, hi)).fetchall()
    return {"sessions": matching.match(plans, acts, today)["sessions"]}


# ---- dashboard ------------------------------------------------------------------------------

def _groups_for(sport):
    if sport == "all":
        return None
    if sport == "foot":
        return set(sports.FOOT_GROUPS)
    return {sport}


def _matched(conn, uid, lo, hi, today):
    """Plan vs activities for lo..hi (inclusive dates), for one account."""
    span = (uid, lo.isoformat(), hi.isoformat())
    plans = conn.execute("SELECT * FROM plan WHERE user_id = ? AND date BETWEEN ? AND ?", span).fetchall()
    acts = conn.execute("SELECT * FROM activities WHERE user_id = ? AND date BETWEEN ? AND ?", span).fetchall()
    return matching.match(plans, acts, today.isoformat())


@app.get("/api/dashboard")
def dashboard(request: Request):
    uid = current_user_id(request)
    today = clock.today()
    monday = metrics.week_start(today)
    horizon = max(monday + timedelta(days=6), today + timedelta(days=6))
    with connect() as conn:
        matched = _matched(conn, uid, monday, horizon, today)
        # 90-day chart + the 28-day window behind its first point
        recent = conn.execute("SELECT * FROM activities WHERE user_id = ? AND date >= ?",
                              (uid, (today - timedelta(days=120)).isoformat())).fetchall()
        present = [r[0] for r in conn.execute(
            "SELECT DISTINCT sport_group FROM activities WHERE user_id = ? AND sport_group != '' ORDER BY 1", (uid,))]
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
def week(request: Request, start: Optional[date] = None):
    """Planned-vs-actual for the Mon-Sun week containing `start` (default: this week)."""
    uid = current_user_id(request)
    today = clock.today()
    monday = metrics.week_start(start or today)
    with connect() as conn:
        matched = _matched(conn, uid, monday, monday + timedelta(days=6), today)
    return views.week_view(matched, monday, today)


@app.get("/api/calendar")
def calendar(request: Request, month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Month grid (YYYY-MM, default this month): one status dot per planned session."""
    uid = current_user_id(request)
    today = clock.today()
    first = date(int(month[:4]), int(month[5:]), 1) if month else today.replace(day=1)
    grid_start, grid_end, _ = views.month_grid(first)
    with connect() as conn:
        matched = _matched(conn, uid, grid_start, grid_end, today)
    return views.calendar_view(matched, first, today)


@app.get("/api/explore")
def explore(request: Request, scope: Literal["year", "month", "week"] = "year", anchor: Optional[date] = None,
            sport: str = Query("all", pattern=r"^[a-z0-9_]{1,40}$")):
    """Distance / elevation / pace summary of a year (weekly buckets), month or week (daily buckets)."""
    uid = current_user_id(request)
    today = clock.today()
    anchor = anchor or today
    start, end, _ = metrics.period(scope, anchor)
    with connect() as conn:
        acts = conn.execute("SELECT * FROM activities WHERE user_id = ? AND date BETWEEN ? AND ?",
                            (uid, start.isoformat(), end.isoformat())).fetchall()
    return {**metrics.explore(acts, scope, anchor, today, _groups_for(sport)), "sport": sport}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
