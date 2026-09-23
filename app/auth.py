"""Multi-account login: a username/password form, an invite-only registration page, and a signed session cookie.

Off (no login at all) until there's at least one account. The Docker image sets REQUIRE_AUTH=1 and requires
SESSION_SECRET, so it refuses to start rather than publish an app nobody can lock.

The first account is created from APP_PASSWORD on first startup (see app/users.py:bootstrap_and_migrate) - after
that, every further account comes from an invite link an existing account creates. There's no "forgot password";
resetting one is a direct database change (ask - this is a small, invite-only app for now).

The session cookie holds a user id, an expiry, and an HMAC of both, keyed by SESSION_SECRET - a secret separate
from any one account's password, since changing your own password shouldn't sign out everyone else.
"""
import asyncio
import hmac
import hashlib
import html
import os
import time
from collections import deque
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, users

COOKIE = "tt_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_SESSION_SECRET_LENGTH = 20

# Public paths: the health probe can't log in, /login and /register need to be reachable to log in or sign up at
# all, and the login/register pages need their stylesheet. Everything else requires a session.
PUBLIC_PATHS = {"/health", "/login", "/register", "/static/style.css"}

# Brute-force brake: after MAX_FAILURES wrong guesses (any account) in FAILURE_WINDOW seconds, logins are refused
# until they age out. In-memory and shared across accounts - this is a handful of invited people, not a public
# service - and existing sessions aren't affected.
MAX_FAILURES = 10
FAILURE_WINDOW = 600
_failures = deque()

router = APIRouter()


# ---- configuration ------------------------------------------------------------------------------

def _session_secret():
    return os.environ.get("SESSION_SECRET", "")


def _required():
    return os.environ.get("REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")


def enabled():
    """Login is on once at least one account exists (created via APP_PASSWORD bootstrap or an invite)."""
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def validate_config():
    """Called at startup, after init_db() so account state is current. Raises so the process exits and the
    deploy is marked failed, rather than serving broken or absent auth."""
    secret = _session_secret()
    if secret and len(secret) < MIN_SESSION_SECRET_LENGTH:
        raise RuntimeError("SESSION_SECRET is too short (use %d+ characters) - generate one with: "
                           "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"" % MIN_SESSION_SECRET_LENGTH)
    if not _required():
        return
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set, and this deployment requires a login (REQUIRE_AUTH=1). "
                           "Generate one with: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    if not enabled():
        raise RuntimeError("REQUIRE_AUTH=1 but no accounts exist yet. Set APP_PASSWORD (%d+ characters) once to "
                           "create the first (admin) account - you can remove it again after that first boot."
                           % users.MIN_PASSWORD_LENGTH)


# ---- session token --------------------------------------------------------------------------------

def _sign(payload):
    return hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(user_id, now=None):
    expires = int((time.time() if now is None else now) + SESSION_SECONDS)
    payload = "%d.%d" % (user_id, expires)
    return payload + "." + _sign(payload)


def parse_token(token, now=None):
    """The user id encoded in a valid, unexpired token, else None."""
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    uid_s, exp_s, sig = parts
    if not hmac.compare_digest(sig, _sign(uid_s + "." + exp_s)):
        return None
    if int(exp_s) <= (time.time() if now is None else now):
        return None
    return int(uid_s)


def resolve_user(request):
    """The logged-in account's row, or None. Re-checks the account still exists, so a stale cookie from a wiped
    or reseeded database - or a rotated SESSION_SECRET - can't grant access to a since-vanished id."""
    uid = parse_token(request.cookies.get(COOKIE))
    if uid is None:
        return None
    with db.connect() as conn:
        return users.get_by_id(conn, uid)


def request_authenticated(request):
    return resolve_user(request) is not None


def safe_next(value):
    """Where to send the user after login: same-site paths only, never another site (open-redirect guard)."""
    v = value or "/"
    if (not v.startswith("/") or v.startswith("//") or v.startswith("/login") or "\\" in v
            or any(ord(c) < 32 or c in "\"'<> " for c in v)):     # no real page path contains these
        return "/"
    return v


def _locked(now):
    while _failures and now - _failures[0] > FAILURE_WINDOW:
        _failures.popleft()
    return len(_failures) >= MAX_FAILURES


def _set_cookie(response, request, user_id):
    response.set_cookie(COOKIE, make_token(user_id), max_age=SESSION_SECONDS, path="/", httponly=True, samesite="lax",
                        secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https")


# ---- pages ----------------------------------------------------------------------------------------

def _login_html(next_path, error=""):
    err = '<div class="banner error" role="alert">%s</div>' % html.escape(error) if error else ""
    body = """%s
  <input type="hidden" name="next" value="%s">
  <label class="muted small" for="un">Username</label>
  <input id="un" type="text" name="username" class="field" autocomplete="username" required autofocus>
  <label class="muted small" for="pw">Password</label>
  <input id="pw" type="password" name="password" autocomplete="current-password" required>
  <button class="btn primary" type="submit" style="width:100%%;margin-top:14px">Log in</button>""" % (
    err, html.escape(next_path, quote=True))
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log in - Training Tracker</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="login-wrap"><form class="card login-card" method="post" action="/login">
  <h1 style="font-size:20px;margin-bottom:14px">Training Tracker</h1>
  %s
</form></main></body></html>""" % body


def _register_html(invite, error="", first_name="", last_name="", username="", email=""):
    err = '<div class="banner error" role="alert">%s</div>' % html.escape(error) if error else ""
    esc = lambda s: html.escape(s, quote=True)
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Create your account - Training Tracker</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="login-wrap"><form class="card login-card" method="post" action="/register">
  <h1 style="font-size:20px;margin-bottom:14px">Create your account</h1>
  %s
  <input type="hidden" name="invite" value="%s">
  <div class="row two-col">
    <div><label class="muted small" for="fn">First name</label>
    <input id="fn" type="text" name="first_name" class="field" autocomplete="given-name" required autofocus value="%s"></div>
    <div><label class="muted small" for="ln">Last name</label>
    <input id="ln" type="text" name="last_name" class="field" autocomplete="family-name" required value="%s"></div>
  </div>
  <label class="muted small" for="em">Email address</label>
  <input id="em" type="email" name="email" class="field" autocomplete="email" required value="%s">
  <label class="muted small" for="un">Username</label>
  <input id="un" type="text" name="username" class="field" autocomplete="username" required
         pattern="[a-z0-9_-]{3,20}" title="3-20 characters: lowercase letters, numbers, - or _" value="%s">
  <label class="muted small" for="pw">Password</label>
  <input id="pw" type="password" name="password" autocomplete="new-password" required minlength="%d">
  <label class="muted small" for="pw2">Confirm password</label>
  <input id="pw2" type="password" name="password2" autocomplete="new-password" required minlength="%d">
  <button class="btn primary" type="submit" style="width:100%%;margin-top:14px">Create account</button>
</form></main></body></html>""" % (err, esc(invite), esc(first_name), esc(last_name), esc(email), esc(username),
                                   users.MIN_PASSWORD_LENGTH, users.MIN_PASSWORD_LENGTH)


def _page(html_text, status=200):
    return HTMLResponse(html_text, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/login", include_in_schema=False)
def login_page(request: Request, next_path: str = Query("/", alias="next")):
    target = safe_next(next_path)
    if not enabled() or request_authenticated(request):
        return RedirectResponse(target, status_code=303)
    return _page(_login_html(target))


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    if not enabled():
        return RedirectResponse("/", status_code=303)
    body = await request.body()
    form = parse_qs(body[:4096].decode("utf-8", "replace"))          # no python-multipart needed for these small forms
    target = safe_next(form.get("next", ["/"])[0])
    username = (form.get("username", [""])[0] or "").strip()
    password = form.get("password", [""])[0]
    now = time.time()
    if _locked(now):
        return _page(_login_html(target, "Too many wrong attempts. Wait a few minutes and try again."), 429)
    with db.connect() as conn:
        row = users.get_by_username(conn, username) if username else None
    if len(body) > 4096 or not row or not users.verify_password(password, row["password_hash"]):
        _failures.append(now)
        await asyncio.sleep(1)                                        # slows a guessing script down further
        return _page(_login_html(target, "Wrong username or password."), 401)
    with db.connect() as conn:
        users.record_login(conn, row["id"])
    response = RedirectResponse(target, status_code=303)
    _set_cookie(response, request, row["id"])
    return response


@router.get("/register", include_in_schema=False)
def register_page(request: Request, invite: str = Query("")):
    if request_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return _page(_register_html(invite))


@router.post("/register", include_in_schema=False)
async def register_submit(request: Request):
    body = await request.body()
    form = parse_qs(body[:4096].decode("utf-8", "replace"))
    invite = form.get("invite", [""])[0]
    first_name = form.get("first_name", [""])[0]
    last_name = form.get("last_name", [""])[0]
    email = form.get("email", [""])[0]
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    password2 = form.get("password2", [""])[0]

    def redisplay(error, status=400):   # keeps whatever they'd already typed except the passwords
        return _page(_register_html(invite, error, first_name, last_name, username, email), status)

    if len(body) > 4096:
        return redisplay("That's too much data.")
    if password != password2:
        return redisplay("Passwords don't match.")
    try:
        with db.connect() as conn:
            new_id = users.redeem_invite(conn, invite, username, password, first_name, last_name, email)
    except users.InviteError as e:
        return redisplay(str(e))
    response = RedirectResponse("/", status_code=303)
    _set_cookie(response, request, new_id)
    return response


@router.post("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response
