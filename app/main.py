import ipaddress
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Literal, Optional
from urllib.parse import urlencode, urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, clock, demo_data, legal, matching, metrics, plan_templates, planparse, sports, strava, users, views, webhook
from .db import LOCAL_USER_ID, ROOT, connect, db_path, get_meta, init_db, volume_warning

# override=True: .env is the source of truth, even if the shell already exports (stale/empty) STRAVA_* vars
load_dotenv(ROOT / ".env", override=True)


log = logging.getLogger("uvicorn.error")


def _regenerate_demo_if_stale():
    """Rebuild the demo account's plan/activities if they weren't already regenerated today. Called at
    startup (covers a restart that crossed midnight, or the very first boot with DEMO_ACCOUNT set) and once a
    day from _demo_scheduler_loop (covers a process that stays up across midnight without restarting)."""
    with connect() as conn:
        demo = users.get_demo_account(conn)
        if demo is None:
            return
        today = clock.today()
        if demo_data.is_stale(conn, demo["id"], today):
            demo_data.regenerate(conn, demo["id"], today)
            log.info("Demo account data regenerated for %s", today)


def _demo_scheduler_loop():
    """Sleeps until the next local midnight, regenerates the demo account, repeats - for as long as the
    process stays up. A plain background thread, not an asyncio task: it only needs to run synchronous,
    blocking work on a real-time schedule, entirely independent of the request-handling event loop - and
    unlike an asyncio task started from `lifespan`, a daemon thread needs no explicit shutdown/cancellation
    dance (it just dies with the process), which sidesteps a real hang seen in testing with the asyncio-task
    version: Starlette's TestClient doesn't reliably drive a lifespan-owned background task's cancellation to
    completion, so `await`ing it after `.cancel()` could hang the test process indefinitely.

    Real wall-clock time throughout (sleeping has to be); the regeneration itself still goes through
    clock.today(), so FITNESS_TODAY can pin it for local testing - see README "Testing the demo account"."""
    while True:
        now = datetime.now()
        next_midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        time.sleep(max(1.0, (next_midnight - now).total_seconds()))
        try:
            _regenerate_demo_if_stale()
        except Exception:
            log.exception("Scheduled demo data regeneration failed")


@asynccontextmanager
async def lifespan(_app):
    init_db()                       # schema + one-time account bootstrap/migration (needs to run first)
    auth.validate_config()          # then fail closed if a login is required but still isn't possible
    log.info("Database: %s", db_path())
    if volume_warning():
        log.warning(volume_warning())
    log.info("Login: %s", "required" if auth.enabled() else "OFF (no accounts yet)")
    if auth.enabled() and not legal.contact_email():
        log.warning("PRIVACY_CONTACT_EMAIL is not set - the privacy policy has no contact address. Strava's API "
                    "policy and UK GDPR both expect one; set it before applying for production API access.")
    _regenerate_demo_if_stale()
    threading.Thread(target=_demo_scheduler_loop, daemon=True, name="demo-scheduler").start()
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
    if not auth.enabled() or path in auth.PUBLIC_PATHS or path.startswith("/static/"):
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


def _block_if_demo(request):
    """Refuses any write on the shared demo login - see app/demo_data.py. The frontend also disables the
    buttons that would reach these routes, but that's just the UX layer; this is the actual enforcement, since
    a disabled button is not a security boundary."""
    me = current_user(request)
    if me and me["is_demo"]:
        raise HTTPException(403, "Demo mode is read-only - try this on your own account.")


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
    me = current_user(request)
    if me and me["is_demo"]:
        # a plain 403 would be an ugly page for a browser navigation (unlike the JSON write endpoints below) -
        # redirect home with the same friendly message auth_callback's own failures already use
        return RedirectResponse("/?" + urlencode({"auth_error": "Demo mode can't connect a real Strava account."}))
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

    me = current_user(request)
    if me and me["is_demo"]:
        return fail("Demo mode can't connect a real Strava account.")
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
            "display_name": (me["first_name"] or me["username"]) if me else None,
            "is_admin": bool(me["is_admin"]) if me else False,
            "is_demo": bool(me["is_demo"]) if me else False,
            "email": me["email"] if me else None,
        }


@app.post("/api/sync")
def sync(request: Request):
    _block_if_demo(request)
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


# ---- privacy, Strava webhook, and getting your data out / off ------------------------------------

@app.get("/privacy", include_in_schema=False)
def privacy():
    return HTMLResponse(legal.privacy_html(), headers={"Cache-Control": "no-store"})


@app.get("/strava/webhook", include_in_schema=False)
def strava_webhook_validate(request: Request):
    """Strava's one-off validation GET when a subscription is created (see strava_webhook.py)."""
    q = request.query_params
    answer = webhook.challenge_response(q.get("hub.mode"), q.get("hub.challenge"), q.get("hub.verify_token"))
    if answer is None:
        raise HTTPException(404, "Not found")   # indistinguishable from the route not existing
    return answer


@app.post("/strava/webhook", include_in_schema=False)
async def strava_webhook_event(request: Request, background: BackgroundTasks):
    if not webhook.enabled():
        raise HTTPException(404, "Not found")
    try:
        event = await request.json()
    except ValueError:
        raise HTTPException(400, "Bad JSON")
    background.add_task(webhook.process_event, event)   # Strava wants its 200 within 2s; the real work follows it
    return {}


def _disconnect_result(conn, uid):
    """Revoke at Strava, then delete what we synced. Order matters: revoking needs the stored token."""
    with strava.make_client() as http:
        revoked = strava.revoke(conn, http, uid)
    return {"strava_revoked": revoked, "activities_deleted": strava.delete_strava_data(conn, uid)}


@app.post("/api/account/disconnect-strava")
def account_disconnect_strava(request: Request):
    _block_if_demo(request)
    with connect() as conn:
        return _disconnect_result(conn, current_user_id(request))


class AccountDelete(BaseModel):
    password: str = Field(max_length=1000)


@app.post("/api/account/delete")
def account_delete(body: AccountDelete, request: Request):
    """Self-service erasure. Asks for the password again (a stolen session shouldn't be enough to destroy an
    account), shares the login form's wrong-guess brake, and refuses to delete the only admin."""
    _block_if_demo(request)
    me = current_user(request)
    if not me:
        raise HTTPException(400, "There's no account to delete - login isn't switched on here.")
    now = time.time()
    if auth._locked(now):
        raise HTTPException(429, "Too many wrong attempts. Wait a few minutes and try again.")
    if not users.verify_password(body.password, me["password_hash"]):
        auth._failures.append(now)
        raise HTTPException(403, "That password isn't right.")
    with connect() as conn:
        if me["is_admin"] and not users.other_admin_exists(conn, me["id"]):
            raise HTTPException(400, "This is the only admin account, so it can't be deleted from here.")
        result = _disconnect_result(conn, me["id"])
        users.delete_account(conn, me["id"])
    response = JSONResponse({**result, "deleted": True})
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.delete("/api/accounts/{username}")
def admin_delete_account(username: str, request: Request):
    """An admin removing someone else's account (e.g. a person who's asked to be deleted). Same clean-up as
    deleting your own, including revoking their Strava access."""
    me = _require_admin(request)
    with connect() as conn:
        target = users.get_by_username(conn, username)
        if not target:
            raise HTTPException(404, "No such account.")
        if target["id"] == me["id"]:
            raise HTTPException(400, "Use the Account page to delete your own account.")
        if target["is_demo"]:
            raise HTTPException(400, "The demo account is managed by DEMO_ACCOUNT, not deleted here.")
        result = _disconnect_result(conn, target["id"])
        users.delete_account(conn, target["id"])
    return {**result, "deleted": True}


@app.get("/api/account/export")
def account_export(request: Request):
    """Everything we hold about the signed-in account, as a JSON download (right of access/portability). Tokens
    and the password hash are deliberately left out - they're credentials, not the person's data."""
    uid, me = current_user_id(request), current_user(request)
    with connect() as conn:
        link = conn.execute("SELECT athlete_id, athlete_name, scope FROM strava_auth WHERE user_id = ?", (uid,)).fetchone()
        data = {
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "account": {k: me[k] for k in ("username", "first_name", "last_name", "email", "created_at",
                                            "last_login_at", "last_active_at")} if me else None,
            "strava": dict(link) if link else None,
            "plan": [dict(r) for r in conn.execute(
                "SELECT date, session_type, sport, planned_distance_km, planned_duration_min, notes FROM plan "
                "WHERE user_id = ? ORDER BY date, position", (uid,))],
            "activities": [dict(r) for r in conn.execute(
                "SELECT id, date, name, sport_type, distance, moving_time, average_heartrate, max_heartrate, "
                "average_speed, max_speed, total_elevation_gain, suffer_score, workout_type FROM activities "
                "WHERE user_id = ? ORDER BY date, start_epoch", (uid,))],
        }
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="training-tracker-data.json"'})


# ---- invites & password resets (admin only) --------------------------------------------------

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
        "accounts": [{"username": a["username"], "first_name": a["first_name"], "last_name": a["last_name"],
                      "email": a["email"], "is_admin": bool(a["is_admin"]), "is_demo": bool(a["is_demo"]),
                      "created_at": a["created_at"], "last_login_at": a["last_login_at"],
                      "last_active_at": a["last_active_at"]} for a in accounts],
        "pending_invites": [{"url": "/register?invite=%s" % p["token"],
                             "expires_in_days": max(0, round((p["expires_at"] - p["created_at"]) / 86400))}
                            for p in pending],
    }


@app.post("/api/accounts/{username}/reset-link")
def create_reset_link(username: str, request: Request):
    """A one-time link that lets the account set a new password, same trust model as an invite - the admin
    sends it however they like (text, email, in person). No self-service "forgot password" request yet
    (that's an email-sending feature for later) - an admin always initiates this from the Admin page."""
    me = _require_admin(request)
    with connect() as conn:
        target = users.get_by_username(conn, username)
        if not target:
            raise HTTPException(404, "No such account.")
        token = users.create_reset_link(conn, target["id"], me["id"])
    return {"token": token, "url": "/reset-password?token=%s" % token,
            "expires_in_hours": users.RESET_TTL_SECONDS // 3600}


# ---- plan -----------------------------------------------------------------------------------

class PlanImport(BaseModel):
    text: str = Field(max_length=1_000_000)
    day_first: bool = True
    mode: Literal["replace_dates", "replace_all"] = "replace_dates"
    dry_run: bool = False
    distance_unit: Literal["km", "mi"] = "km"   # for distances written without a unit


def _save_plan_rows(conn, uid, rows, mode):
    """Shared by /api/plan/import and /api/plan-templates/{id}/apply: both end up with the same
    row shape (planparse.parse_plan's output, or plan_templates.build_rows's), so both save the
    same way."""
    if mode == "replace_all":
        conn.execute("DELETE FROM plan WHERE user_id = ?", (uid,))
    else:
        conn.executemany("DELETE FROM plan WHERE user_id = ? AND date = ?",
                         [(uid, d) for d in {r["date"] for r in rows}])
    conn.executemany(
        "INSERT INTO plan (user_id, date, session_type, sport, sport_group, planned_distance_km, "
        "planned_duration_min, notes, position) VALUES (:user_id, :date, :session_type, :sport, :sport_group, "
        ":planned_distance_km, :planned_duration_min, :notes, :position)", rows)


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
    _block_if_demo(request)   # previewing (dry_run) is fine - only the actual save is blocked
    with connect() as conn:
        _save_plan_rows(conn, uid, rows, body.mode)
    result["saved"] = len(rows)
    return result


# ---- ready-made plan templates ---------------------------------------------------------------

@app.get("/api/plan-templates")
def plan_templates_list():
    return {"disclaimer": plan_templates.DISCLAIMER, "plans": plan_templates.list_templates(clock.today())}


class PlanTemplateApply(BaseModel):
    start_date: Optional[str] = None   # ISO date; snapped to that week's Monday. Default: the coming Monday.
    race_date: Optional[str] = None    # ISO date; snapped to that week's Sunday. Wins over start_date if both are given.
    mode: Literal["replace_dates", "replace_all"] = "replace_all"
    dry_run: bool = False


@app.post("/api/plan-templates/{plan_id}/apply")
def plan_templates_apply(plan_id: str, body: PlanTemplateApply, request: Request):
    uid = current_user_id(request)
    try:
        if body.race_date:
            start = plan_templates.start_monday_for_race_date(plan_id, date.fromisoformat(body.race_date))
        elif body.start_date:
            start = date.fromisoformat(body.start_date)
        else:
            start = plan_templates.default_start_monday(clock.today())
    except ValueError:
        raise HTTPException(400, "start_date/race_date must be YYYY-MM-DD.")
    except KeyError:
        raise HTTPException(404, "No such plan template.")
    try:
        rows = plan_templates.build_rows(plan_id, start, uid)
    except KeyError:
        raise HTTPException(404, "No such plan template.")
    result = {"rows": rows, "errors": [], "warnings": [], "dry_run": body.dry_run, "saved": 0,
              "start_date": rows[0]["date"], "race_date": rows[-1]["date"]}
    if body.dry_run:
        return result
    _block_if_demo(request)   # previewing (dry_run) is fine - only the actual save is blocked
    with connect() as conn:
        _save_plan_rows(conn, uid, rows, body.mode)
    result["saved"] = len(rows)
    return result


@app.delete("/api/plan")
def plan_clear(request: Request):
    _block_if_demo(request)
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
