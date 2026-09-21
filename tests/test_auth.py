"""Password login: what's protected, what isn't, and how it resists forged cookies, open redirects and guessing."""
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, main

PASSWORD = "correct horse battery staple"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def secured(tmp_path, monkeypatch):
    """A client for an app with APP_PASSWORD set. Cookies persist in the client, like a browser."""
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)

    async def no_delay(_seconds):        # the real 1-second penalty per wrong guess would make these tests crawl
        return None
    monkeypatch.setattr(auth.asyncio, "sleep", no_delay)
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def log_in(client, password=PASSWORD, **extra):
    return client.post("/login", data={"password": password, **extra})


# ---- off by default; on when APP_PASSWORD is set ----------------------------------------------

def test_no_password_means_no_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "open.db"))
    with TestClient(main.app, follow_redirects=False) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/status").json()["auth_enabled"] is False
        assert c.get("/login").headers["location"] == "/"          # nothing to log in to


def test_everything_is_protected_until_logged_in(secured):
    assert secured.get("/api/status").status_code == 401
    assert secured.get("/api/dashboard").status_code == 401
    assert secured.get("/api/plan").status_code == 401
    assert secured.post("/api/sync").status_code == 401
    assert secured.post("/api/plan/import", json={"text": "date,sport\n2026-09-28,Run\n"}).status_code == 401
    assert secured.delete("/api/plan").status_code == 401
    assert secured.get("/api/status").json() == {"detail": "Login required"}
    for page in ("/", "/static/app.js", "/docs", "/openapi.json"):
        r = secured.get(page)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), page


def test_strava_connect_flow_requires_login_too(secured):
    """Otherwise a stranger could start (or overwrite) the Strava connection."""
    assert secured.get("/auth/login").headers["location"].startswith("/login")
    r = secured.get("/auth/callback", params={"code": "x", "state": "y", "scope": "read,activity:read_all"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_only_health_login_and_stylesheet_are_public(secured):
    assert secured.get("/health").status_code == 200
    assert secured.get("/health", headers={"host": "healthcheck.railway.app"}).status_code == 200   # Railway can't log in
    assert secured.get("/static/style.css").status_code == 200
    page = secured.get("/login")
    assert page.status_code == 200 and 'type="password"' in page.text and 'autocomplete="current-password"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert secured.get("/static/index.html").status_code == 303                                     # not the whole /static


# ---- logging in and out -----------------------------------------------------------------------

def test_wrong_password_is_refused_and_sets_no_cookie(secured):
    for bad in ("", "wrong", PASSWORD.upper(), PASSWORD + " ", "é" * 5):
        r = log_in(secured, bad)
        assert r.status_code == 401 and "Wrong password" in r.text and "set-cookie" not in r.headers
    assert secured.get("/api/status").status_code == 401


def test_correct_password_logs_in_with_a_hardened_cookie(secured):
    r = log_in(secured)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie and "max-age=2592000" in cookie
    assert "secure" not in cookie.replace("samesite", "")                     # plain-http local use must still work
    assert PASSWORD not in r.headers["set-cookie"]                            # the password itself is never in the cookie
    ok = secured.get("/api/status")
    assert ok.status_code == 200 and ok.json()["auth_enabled"] is True
    assert secured.get("/").status_code == 200


def test_cookie_is_secure_behind_https(secured):
    r = secured.post("/login", data={"password": PASSWORD}, headers={"x-forwarded-proto": "https"})
    assert "; secure" in r.headers["set-cookie"].lower()


def test_logout_ends_the_session(secured):
    log_in(secured)
    assert secured.get("/api/status").status_code == 200
    r = secured.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert secured.get("/api/status").status_code == 401


def test_already_logged_in_visiting_login_goes_to_the_app(secured):
    log_in(secured)
    assert secured.get("/login").headers["location"] == "/"


# ---- the cookie can't be forged, replayed after expiry, or carried across a password change --------

def test_tampered_or_forged_cookies_are_rejected(secured):
    good = auth.make_token()
    expires, sig = good.split(".")
    forged = [
        "", "garbage", ".", expires + ".", "." + sig,
        str(int(expires) + 999999) + "." + sig,                 # extended expiry, old signature
        expires + "." + sig[:-1] + ("0" if sig[-1] != "0" else "1"),   # one signature character changed
        "abc." + sig,                                           # non-numeric expiry
    ]
    for value in forged:
        secured.cookies.clear()
        secured.cookies.set(auth.COOKIE, value)
        assert secured.get("/api/status").status_code == 401, value
    secured.cookies.clear()
    secured.cookies.set(auth.COOKIE, good)
    assert secured.get("/api/status").status_code == 200        # ...and the genuine one still works


def test_expired_sessions_stop_working(secured):
    old = auth.make_token(now=time.time() - auth.SESSION_SECONDS - 60)
    secured.cookies.set(auth.COOKIE, old)
    assert secured.get("/api/status").status_code == 401
    assert auth.valid_token(auth.make_token(now=1_000_000), now=1_000_000 + auth.SESSION_SECONDS - 1)
    assert not auth.valid_token(auth.make_token(now=1_000_000), now=1_000_000 + auth.SESSION_SECONDS + 1)


def test_changing_the_password_signs_everyone_out(secured, monkeypatch):
    log_in(secured)
    assert secured.get("/api/status").status_code == 200
    monkeypatch.setenv("APP_PASSWORD", "a completely different password")
    assert secured.get("/api/status").status_code == 401         # old cookie was signed with the old password


# ---- redirects can't leave the site ---------------------------------------------------------------

@pytest.mark.parametrize("nxt,expected", [
    ("/", "/"), ("/#/plan", "/#/plan"), ("/static/app.js", "/static/app.js"),
    ("//evil.example.com", "/"), ("https://evil.example.com", "/"), ("http://evil.example.com/x", "/"),
    ("/\\evil.example.com", "/"), ("javascript:alert(1)", "/"), ("evil.example.com", "/"), ("", "/"),
    ("/login", "/"), ("/login?next=/x", "/"), ("/a\nb", "/"), ("/a\x00b", "/")])
def test_next_parameter_is_same_site_only(secured, nxt, expected):
    assert auth.safe_next(nxt) == expected
    r = log_in(secured, next=nxt)
    assert r.status_code == 303 and r.headers["location"] == expected
    secured.cookies.clear()


def test_login_page_escapes_the_next_value(secured):
    r = secured.get("/login", params={"next": '/"><script>alert(1)</script>'})
    assert "<script>" not in r.text and 'value="/"' in r.text                 # rejected outright as unsafe -> "/"
    r = secured.post("/login", data={"password": "nope", "next": '/x"><img src=x onerror=alert(1)>'})
    assert "<img" not in r.text


# ---- guessing ----------------------------------------------------------------------------------

def test_repeated_wrong_guesses_lock_logins_even_for_the_right_password(secured):
    for _ in range(auth.MAX_FAILURES):
        assert log_in(secured, "nope").status_code == 401
    r = log_in(secured, PASSWORD)
    assert r.status_code == 429 and "set-cookie" not in r.headers and "Too many" in r.text
    assert secured.get("/api/status").status_code == 401


def test_lockout_ends_when_old_failures_age_out(secured):
    auth._failures.extend([time.time() - auth.FAILURE_WINDOW - 5] * auth.MAX_FAILURES)   # all older than the window
    assert log_in(secured).status_code == 303


def test_existing_sessions_survive_a_lockout(secured):
    log_in(secured)
    for _ in range(auth.MAX_FAILURES):
        client_b = TestClient(main.app, follow_redirects=False)
        client_b.post("/login", data={"password": "nope"})
    assert secured.get("/api/status").status_code == 200               # the owner's session isn't affected


def test_oversized_login_body_is_refused(secured):
    r = secured.post("/login", data={"password": PASSWORD, "pad": "x" * 10000})
    assert r.status_code == 401


# ---- configuration fails closed -------------------------------------------------------------------

def test_deployment_refuses_to_start_without_a_password(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    with pytest.raises(RuntimeError, match="APP_PASSWORD is not set"):
        with TestClient(main.app):
            pass


def test_short_passwords_are_refused_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("APP_PASSWORD", "short")
    with pytest.raises(RuntimeError, match="too short"):
        with TestClient(main.app):
            pass


def test_the_docker_image_requires_a_login():
    assert re.search(r"^ENV REQUIRE_AUTH=1$", (ROOT / "Dockerfile").read_text(), re.M)


def test_good_password_and_require_auth_starts_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200


def test_login_html_escapes_whatever_it_is_given():
    """safe_next now rejects such values first, but the renderer must be safe on its own."""
    page = auth._login_html('/"><script>alert(1)</script>', error="<b>x</b>")
    assert "<script" not in page and "<b>x</b>" not in page
    assert "&lt;script&gt;" in page and "&lt;b&gt;x&lt;/b&gt;" in page
