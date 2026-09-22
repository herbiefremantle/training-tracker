"""Multi-account login: bootstrap, invites, sessions, and how each resists forged cookies, open redirects,
guessing, and one account seeing another's data."""
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, users

PASSWORD = "correct horse battery staple"
SECRET = "a-long-random-session-secret-value"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def secured(tmp_path, monkeypatch):
    """A client for an app with an admin account already bootstrapped from APP_PASSWORD."""
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)

    async def no_delay(_seconds):        # the real 1-second penalty per wrong guess would make these tests crawl
        return None
    monkeypatch.setattr(auth.asyncio, "sleep", no_delay)
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def log_in(client, username="pete", password=PASSWORD, **extra):
    return client.post("/login", data={"username": username, "password": password, **extra})


def admin_id(client):
    with db.connect() as conn:
        return users.get_by_username(conn, "pete")["id"]


def make_invite(client):
    log_in(client)
    token = client.post("/api/invites").json()["token"]
    client.post("/logout")
    return token


# ---- off by default; on once an account exists -------------------------------------------------

def test_no_accounts_means_no_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "open.db"))
    with TestClient(main.app, follow_redirects=False) as c:
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/status").json()["auth_enabled"] is False
        assert c.get("/login").headers["location"] == "/"          # nothing to log in to


def test_app_password_bootstraps_the_first_admin_account(secured):
    s = log_in(secured)
    assert s.status_code == 303
    me = secured.get("/api/status").json()
    assert me["auth_enabled"] and me["username"] == "pete" and me["is_admin"] is True


def test_admin_username_env_var_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_USERNAME", "herbie")
    with TestClient(main.app, follow_redirects=False) as c:
        r = log_in(c, username="herbie")
        assert r.status_code == 303


def test_everything_is_protected_until_logged_in(secured):
    assert secured.get("/api/status").status_code == 401
    assert secured.get("/api/dashboard").status_code == 401
    assert secured.get("/api/plan").status_code == 401
    assert secured.post("/api/sync").status_code == 401
    assert secured.post("/api/invites").status_code == 401
    assert secured.post("/api/plan/import", json={"text": "date,sport\n2026-09-28,Run\n"}).status_code == 401
    assert secured.delete("/api/plan").status_code == 401
    assert secured.get("/api/status").json() == {"detail": "Login required"}
    for page in ("/", "/static/app.js", "/docs", "/openapi.json"):
        r = secured.get(page)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), page


def test_strava_connect_flow_requires_login_too(secured):
    assert secured.get("/auth/login").headers["location"].startswith("/login")
    r = secured.get("/auth/callback", params={"code": "x", "state": "y", "scope": "read,activity:read_all"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_only_health_login_register_and_stylesheet_are_public(secured):
    assert secured.get("/health").status_code == 200
    assert secured.get("/health", headers={"host": "healthcheck.railway.app"}).status_code == 200
    assert secured.get("/static/style.css").status_code == 200
    assert secured.get("/register").status_code == 200
    page = secured.get("/login")
    assert page.status_code == 200 and 'name="username"' in page.text and 'type="password"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert secured.get("/static/index.html").status_code == 303                                     # not the whole /static


# ---- logging in and out -----------------------------------------------------------------------

def test_wrong_credentials_are_refused_and_set_no_cookie(secured):
    for u, p in [("pete", "wrong"), ("nobody", PASSWORD), ("PETE", "wrong"), ("", PASSWORD), ("pete", "")]:
        r = log_in(secured, u, p)
        assert r.status_code == 401 and "Wrong username or password" in r.text and "set-cookie" not in r.headers
    assert secured.get("/api/status").status_code == 401


def test_username_is_case_insensitive(secured):
    assert log_in(secured, "PETE").status_code == 303


def test_correct_login_sets_a_hardened_cookie(secured):
    r = log_in(secured)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie and "max-age=2592000" in cookie
    assert "secure" not in cookie.replace("samesite", "")                     # plain-http local use must still work
    assert PASSWORD not in r.headers["set-cookie"] and SECRET not in r.headers["set-cookie"]
    ok = secured.get("/api/status")
    assert ok.status_code == 200 and ok.json()["auth_enabled"] is True


def test_cookie_is_secure_behind_https(secured):
    r = secured.post("/login", data={"username": "pete", "password": PASSWORD}, headers={"x-forwarded-proto": "https"})
    assert "; secure" in r.headers["set-cookie"].lower()


def test_logout_ends_the_session(secured):
    log_in(secured)
    assert secured.get("/api/status").status_code == 200
    r = secured.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert secured.get("/api/status").status_code == 401


def test_already_logged_in_visiting_login_or_register_goes_to_the_app(secured):
    log_in(secured)
    assert secured.get("/login").headers["location"] == "/"
    assert secured.get("/register").headers["location"] == "/"


# ---- the cookie can't be forged, replayed after expiry, or reused after the secret changes --------

def test_tampered_or_forged_cookies_are_rejected(secured):
    uid = admin_id(secured)
    good = auth.make_token(uid)
    parts = good.split(".")
    forged = [
        "", "garbage", ".", "1.2", parts[0] + "." + parts[1] + ".",
        str(uid) + "." + str(int(parts[1]) + 999999) + "." + parts[2],       # extended expiry, old signature
        parts[0] + "." + parts[1] + "." + (parts[2][:-1] + ("0" if parts[2][-1] != "0" else "1")),
        "abc." + parts[1] + "." + parts[2],                                  # non-numeric uid
        str(uid + 999) + "." + parts[1] + "." + parts[2],                    # someone else's id, old signature
    ]
    for value in forged:
        secured.cookies.clear()
        secured.cookies.set(auth.COOKIE, value)
        assert secured.get("/api/status").status_code == 401, value
    secured.cookies.clear()
    secured.cookies.set(auth.COOKIE, good)
    assert secured.get("/api/status").status_code == 200        # ...and the genuine one still works


def test_expired_sessions_stop_working(secured):
    uid = admin_id(secured)
    old = auth.make_token(uid, now=time.time() - auth.SESSION_SECONDS - 60)
    secured.cookies.set(auth.COOKIE, old)
    assert secured.get("/api/status").status_code == 401
    assert auth.parse_token(auth.make_token(uid, now=1_000_000), now=1_000_000 + auth.SESSION_SECONDS - 1) == uid
    assert auth.parse_token(auth.make_token(uid, now=1_000_000), now=1_000_000 + auth.SESSION_SECONDS + 1) is None


def test_deleted_account_cookie_stops_working(secured):
    """A stale cookie referencing an id that no longer exists must not grant access (e.g. after a reseed) -
    with a second account still present, so this isn't just "zero accounts means login is off"."""
    token = make_invite(secured)
    TestClient(main.app, follow_redirects=False).post(
        "/register", data={"invite": token, "username": "alex", "password": "friend-password", "password2": "friend-password"})
    uid = admin_id(secured)
    log_in(secured)
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert secured.get("/api/status").status_code == 401


def test_changing_the_session_secret_signs_everyone_out(secured, monkeypatch):
    log_in(secured)
    assert secured.get("/api/status").status_code == 200
    monkeypatch.setenv("SESSION_SECRET", "a-completely-different-session-secret")
    assert secured.get("/api/status").status_code == 401


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


def test_login_html_escapes_whatever_it_is_given():
    page = auth._login_html('/"><script>alert(1)</script>', error="<b>x</b>")
    assert "<script" not in page and "<b>x</b>" not in page
    assert "&lt;script&gt;" in page and "&lt;b&gt;x&lt;/b&gt;" in page


# ---- guessing ----------------------------------------------------------------------------------

def test_repeated_wrong_guesses_lock_logins_even_for_the_right_password(secured):
    for _ in range(auth.MAX_FAILURES):
        assert log_in(secured, password="nope").status_code == 401
    r = log_in(secured)
    assert r.status_code == 429 and "set-cookie" not in r.headers and "Too many" in r.text
    assert secured.get("/api/status").status_code == 401


def test_lockout_ends_when_old_failures_age_out(secured):
    auth._failures.extend([time.time() - auth.FAILURE_WINDOW - 5] * auth.MAX_FAILURES)
    assert log_in(secured).status_code == 303


def test_existing_sessions_survive_a_lockout(secured):
    log_in(secured)
    for _ in range(auth.MAX_FAILURES):
        TestClient(main.app, follow_redirects=False).post("/login", data={"username": "pete", "password": "nope"})
    assert secured.get("/api/status").status_code == 200               # the owner's session isn't affected


def test_oversized_login_body_is_refused(secured):
    r = secured.post("/login", data={"username": "pete", "password": PASSWORD, "pad": "x" * 10000})
    assert r.status_code == 401


# ---- invites and registration -------------------------------------------------------------------

def test_only_admin_can_create_or_list_invites(secured):
    log_in(secured)
    r = secured.post("/api/invites")
    assert r.status_code == 200 and r.json()["url"].startswith("/register?invite=")
    listing = secured.get("/api/invites").json()
    assert listing["accounts"] == [{"username": "pete", "is_admin": True}]
    assert len(listing["pending_invites"]) == 1

    secured.post("/logout")
    token = make_invite(secured)   # logs in as pete, creates one, logs out again
    friend = TestClient(main.app, follow_redirects=False)
    friend.post("/register", data={"invite": token, "username": "alex", "password": "friend-password", "password2": "friend-password"})
    friend.post("/login", data={"username": "alex", "password": "friend-password"})
    assert friend.post("/api/invites").status_code == 403
    assert friend.get("/api/invites").status_code == 403


def test_full_invite_and_registration_flow(secured):
    token = make_invite(secured)
    friend = TestClient(main.app, follow_redirects=False)
    page = friend.get("/register", params={"invite": token})
    assert page.status_code == 200 and 'name="username"' in page.text

    r = friend.post("/register", data={"invite": token, "username": "Alex", "password": "friend-password",
                                       "password2": "friend-password"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "set-cookie" in r.headers                                    # registering logs them straight in
    me = friend.get("/api/status").json()
    assert me["username"] == "alex" and me["is_admin"] is False

    # the same invite can't be used twice
    again = TestClient(main.app, follow_redirects=False)
    r2 = again.post("/register", data={"invite": token, "username": "someoneelse", "password": "another-password",
                                       "password2": "another-password"})
    assert r2.status_code == 400 and "already been used" in r2.text


@pytest.mark.parametrize("field,value,message", [
    ("username", "ab", "3-20 characters"),                 # too short
    ("username", "Has Spaces", "3-20 characters"),
    ("password", "short", "at least"),                      # too short
])
def test_registration_validates_fields(secured, field, value, message):
    token = make_invite(secured)
    form = {"invite": token, "username": "newuser", "password": "a-fine-password", "password2": "a-fine-password"}
    form[field] = value
    if field == "password":
        form["password2"] = value
    r = TestClient(main.app, follow_redirects=False).post("/register", data=form)
    assert r.status_code == 400 and message in r.text


def test_registration_rejects_mismatched_passwords(secured):
    token = make_invite(secured)
    r = TestClient(main.app, follow_redirects=False).post(
        "/register", data={"invite": token, "username": "newuser", "password": "one-password", "password2": "different"})
    assert r.status_code == 400 and "match" in r.text and "Passwords" in r.text


def test_registration_rejects_a_taken_username(secured):
    token = make_invite(secured)
    r = TestClient(main.app, follow_redirects=False).post(
        "/register", data={"invite": token, "username": "pete", "password": "a-fine-password", "password2": "a-fine-password"})
    assert r.status_code == 400 and "taken" in r.text


def test_bad_or_expired_invite_is_refused(secured):
    client = TestClient(main.app, follow_redirects=False)
    r = client.post("/register", data={"invite": "not-a-real-token", "username": "x", "password": "a-fine-password",
                                       "password2": "a-fine-password"})
    assert r.status_code == 400 and "invite link" in r.text and "valid" in r.text

    token = make_invite(secured)
    with db.connect() as conn:
        conn.execute("UPDATE invites SET expires_at = ? WHERE token = ?", (time.time() - 1, token))
    r = client.post("/register", data={"invite": token, "username": "y", "password": "a-fine-password",
                                       "password2": "a-fine-password"})
    assert r.status_code == 400 and "expired" in r.text


def test_max_users_cap(secured, monkeypatch):
    monkeypatch.setenv("MAX_USERS", "2")     # pete + 1 more
    log_in(secured)
    r = secured.post("/api/invites")
    assert r.status_code == 200
    token = r.json()["token"]
    TestClient(main.app, follow_redirects=False).post(
        "/register", data={"invite": token, "username": "second", "password": "a-fine-password", "password2": "a-fine-password"})
    # at the cap: no more invites can be created...
    assert secured.post("/api/invites").status_code == 400
    # ...and a still-valid pre-existing invite can't be redeemed past the cap either
    token2 = None
    with db.connect() as conn:
        token2 = users.create_invite(conn, admin_id(secured))
    r2 = TestClient(main.app, follow_redirects=False).post(
        "/register", data={"invite": token2, "username": "third", "password": "a-fine-password", "password2": "a-fine-password"})
    assert r2.status_code == 400 and "full" in r2.text


# ---- data isolation between accounts --------------------------------------------------------------

def test_two_accounts_never_see_each_others_data(secured):
    token = make_invite(secured)
    friend = TestClient(main.app, follow_redirects=False)
    friend.post("/register", data={"invite": token, "username": "alex", "password": "friend-password", "password2": "friend-password"})

    log_in(secured)
    secured.post("/api/plan/import", json={"text": "date,sport\n2026-09-28,Run\n"})
    friend.post("/api/plan/import", json={"text": "date,sport\n2026-09-29,Ride\n"})

    admin_plan = secured.get("/api/plan").json()["sessions"]
    friend_plan = friend.get("/api/plan").json()["sessions"]
    assert [s["date"] for s in admin_plan] == ["2026-09-28"]
    assert [s["date"] for s in friend_plan] == ["2026-09-29"]

    assert secured.get("/api/status").json()["plan_count"] == 1
    assert friend.get("/api/status").json()["plan_count"] == 1     # not 2 - each only sees their own

    friend.delete("/api/plan")
    assert secured.get("/api/plan").json()["sessions"][0]["date"] == "2026-09-28"   # untouched by the friend's delete


# ---- configuration fails closed -------------------------------------------------------------------

def test_deployment_refuses_to_start_without_a_session_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    with pytest.raises(RuntimeError, match="SESSION_SECRET is not set"):
        with TestClient(main.app):
            pass


def test_deployment_refuses_to_start_without_any_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("REQUIRE_AUTH", "1")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with pytest.raises(RuntimeError, match="no accounts exist"):
        with TestClient(main.app):
            pass


def test_short_session_secret_is_refused_even_without_require_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("SESSION_SECRET", "short")
    with pytest.raises(RuntimeError, match="too short"):
        with TestClient(main.app):
            pass


def test_short_app_password_is_refused_during_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("APP_PASSWORD", "short")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with pytest.raises(RuntimeError, match="too short"):
        with TestClient(main.app):
            pass


def test_the_docker_image_requires_a_login():
    assert re.search(r"^ENV REQUIRE_AUTH=1$", (ROOT / "Dockerfile").read_text(), re.M)


def test_good_config_starts_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("REQUIRE_AUTH", "true")
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200
