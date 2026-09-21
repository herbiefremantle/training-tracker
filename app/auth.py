"""Single-user password login: a form, plus a signed, expiring cookie.

Off unless APP_PASSWORD is set, so local use is unchanged. The Docker image sets REQUIRE_AUTH=1, which makes the app
refuse to start without a password - a forgotten variable fails the deploy instead of publishing an open app.

The cookie holds only an expiry time and an HMAC of it, keyed from the password itself. So there's no separate secret
to manage, and changing APP_PASSWORD signs every device out.
"""
import asyncio
import hashlib
import hmac
import html
import os
import time
from collections import deque
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

COOKIE = "tt_session"
SESSION_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LENGTH = 12

# Public paths: the health probe can't log in, and the login page needs its stylesheet. Everything else is protected.
PUBLIC_PATHS = {"/health", "/login", "/static/style.css"}

# Brute-force brake: after MAX_FAILURES wrong guesses in FAILURE_WINDOW seconds, logins are refused until they age out.
# It's global (this is a one-person app, and sessions that already exist are unaffected), and in memory (one process).
MAX_FAILURES = 10
FAILURE_WINDOW = 600
_failures = deque()

router = APIRouter()


# ---- configuration ---------------------------------------------------------------------------

def _password():
    return os.environ.get("APP_PASSWORD", "")


def enabled():
    return bool(_password())


def _required():
    return os.environ.get("REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")


def validate_config():
    """Called at startup. Raises so the process exits and the deploy is marked failed, rather than serving an open app."""
    if _required() and not _password():
        raise RuntimeError("APP_PASSWORD is not set, and this deployment requires a login (REQUIRE_AUTH=1). "
                           "Set APP_PASSWORD to a password of at least %d characters." % MIN_PASSWORD_LENGTH)
    if _password() and len(_password()) < MIN_PASSWORD_LENGTH:
        raise RuntimeError("APP_PASSWORD is too short: use at least %d characters (a long random one is best)."
                           % MIN_PASSWORD_LENGTH)


# ---- password and session token --------------------------------------------------------------

def check_password(candidate):
    # hash both sides first so the comparison is constant-time whatever the lengths
    return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(_password().encode()).digest())


def _sign(payload):
    key = hmac.new(_password().encode(), b"training-tracker session v1", hashlib.sha256).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_token(now=None):
    expires = str(int((time.time() if now is None else now) + SESSION_SECONDS))
    return expires + "." + _sign(expires)


def valid_token(token, now=None):
    expires, _, signature = (token or "").partition(".")
    if not expires.isdigit() or not signature:
        return False
    if not hmac.compare_digest(signature, _sign(expires)):
        return False
    return int(expires) > (time.time() if now is None else now)


def request_authenticated(request):
    return valid_token(request.cookies.get(COOKIE))


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


# ---- pages -----------------------------------------------------------------------------------

def _login_html(next_path, error=""):
    err = '<div class="banner error" role="alert">%s</div>' % html.escape(error) if error else ""
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Log in - Training Tracker</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="login-wrap"><form class="card login-card" method="post" action="/login">
  <h1 style="font-size:20px;margin-bottom:14px">Training Tracker</h1>
  %s
  <input type="hidden" name="next" value="%s">
  <input type="text" name="username" value="training-tracker" autocomplete="username" hidden>
  <label class="muted small" for="pw">Password</label>
  <input id="pw" type="password" name="password" autocomplete="current-password" required autofocus>
  <button class="btn primary" type="submit" style="width:100%%;margin-top:14px">Log in</button>
</form></main></body></html>""" % (err, html.escape(next_path, quote=True))


def _page(next_path, error="", status=200):
    return HTMLResponse(_login_html(next_path, error), status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/login", include_in_schema=False)
def login_page(request: Request, next_path: str = Query("/", alias="next")):
    target = safe_next(next_path)
    if not enabled() or request_authenticated(request):
        return RedirectResponse(target, status_code=303)
    return _page(target)


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    if not enabled():
        return RedirectResponse("/", status_code=303)
    body = await request.body()
    form = parse_qs(body[:4096].decode("utf-8", "replace"))          # no python-multipart needed for one small form
    target = safe_next(form.get("next", ["/"])[0])
    now = time.time()
    if _locked(now):
        return _page(target, "Too many wrong passwords. Wait a few minutes and try again.", 429)
    if len(body) > 4096 or not check_password(form.get("password", [""])[0]):
        _failures.append(now)
        await asyncio.sleep(1)                                        # slows a guessing script down further
        return _page(target, "Wrong password.", 401)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(COOKIE, make_token(), max_age=SESSION_SECONDS, path="/", httponly=True, samesite="lax",
                        secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https")
    return response


@router.post("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response
