"""Multi-account login: bootstrap, invites, sessions, and how each resists forged cookies, open redirects,
guessing, and one account seeing another's data."""
import re
import time
from datetime import date, datetime, timedelta
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


def register(client, token, username="alex", password="friend-password", first_name="Alex", last_name="Friend",
            email=None, **extra):
    return client.post("/register", data={
        "invite": token, "first_name": first_name, "last_name": last_name,
        "email": email if email is not None else "%s@example.com" % username.lower(),
        "username": username, "password": password, "password2": extra.pop("password2", password), **extra})


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
    for page in ("/", "/docs", "/openapi.json"):   # /static/* is public by design - see the next test
        r = secured.get(page)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), page


def test_strava_connect_flow_requires_login_too(secured):
    assert secured.get("/auth/login").headers["location"].startswith("/login")
    r = secured.get("/auth/callback", params={"code": "x", "state": "y", "scope": "read,activity:read_all"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_health_login_register_and_all_static_assets_are_public(secured):
    """/static/* is public by design (see require_login in main.py): none of it is secret, and the manifest,
    icons and service worker for "Add to Home Screen" have to load before anyone has logged in."""
    assert secured.get("/health").status_code == 200
    assert secured.get("/health", headers={"host": "healthcheck.railway.app"}).status_code == 200
    assert secured.get("/register").status_code == 200
    page = secured.get("/login")
    assert page.status_code == 200 and 'name="username"' in page.text and 'type="password"' in page.text
    assert page.headers["cache-control"] == "no-store"
    for asset in ("/static/style.css", "/static/app.js", "/static/install.js", "/static/sw.js",
                 "/static/manifest.json", "/static/index.html", "/static/icons/icon-512.png"):
        assert secured.get(asset).status_code == 200, asset
    # but nothing under /static/ can be used to reach an API route or bypass login for the app itself
    assert secured.get("/static/../api/status").status_code in (401, 404, 307)


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
    register(TestClient(main.app, follow_redirects=False), token)
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
    # the page legitimately has its own <script src="/static/install.js"> tag now, so check the *injected*
    # payload specifically is neutralised, not that the substring "<script" is absent from the whole page
    page = auth._login_html('/"><script>alert(1)</script>', error="<b>x</b>")
    assert "<script>alert(1)</script>" not in page and "<b>x</b>" not in page
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
    assert len(listing["accounts"]) == 1 and len(listing["pending_invites"]) == 1
    pete = listing["accounts"][0]
    assert (pete["username"], pete["is_admin"], pete["first_name"]) == ("pete", True, None)   # bootstrap has no profile
    assert pete["created_at"] and pete["last_login_at"] and pete["last_active_at"]              # all set from the start

    secured.post("/logout")
    token = make_invite(secured)   # logs in as pete, creates one, logs out again
    friend = TestClient(main.app, follow_redirects=False)
    register(friend, token)
    friend.post("/login", data={"username": "alex", "password": "friend-password"})
    assert friend.post("/api/invites").status_code == 403
    assert friend.get("/api/invites").status_code == 403


def test_full_invite_and_registration_flow(secured):
    token = make_invite(secured)
    friend = TestClient(main.app, follow_redirects=False)
    page = friend.get("/register", params={"invite": token})
    assert page.status_code == 200 and 'name="username"' in page.text

    r = register(friend, token, username="Alex", first_name="Alex", last_name="Friend", email="alex@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "set-cookie" in r.headers                                    # registering logs them straight in
    me = friend.get("/api/status").json()
    assert me["username"] == "alex" and me["is_admin"] is False and me["display_name"] == "Alex"

    with db.connect() as conn:
        row = users.get_by_username(conn, "alex")
        assert (row["first_name"], row["last_name"], row["email"]) == ("Alex", "Friend", "alex@example.com")
        assert row["created_at"] == row["last_login_at"]      # registering counts as the first login

    # the same invite can't be used twice
    again = TestClient(main.app, follow_redirects=False)
    r2 = register(again, token, username="someoneelse", password="another-password", email="someone@example.com")
    assert r2.status_code == 400 and "already been used" in r2.text


# ---- "last active" (any day the app was opened) vs. "last login" (an actual credential event) ----

def test_resolve_user_updates_last_active_at_most_once_a_day(secured, monkeypatch):
    """Distinct from last_login_at: a 30-day session cookie means most authenticated requests never touch
    /login again, so last_active_at has to come from ordinary requests instead - see app/auth.py:resolve_user.
    Only clock.today() is mocked here (not time.time()) - real timestamps, just pinned to which calendar day
    resolve_user thinks "today" is, so this doesn't care what the real wall-clock date happens to be."""
    log_in(secured)
    uid = admin_id(secured)
    with db.connect() as conn:   # force "never recorded" regardless of whatever bootstrap already set
        conn.execute("UPDATE users SET last_active_at = 0 WHERE id = ?", (uid,))

    day1 = date(2026, 9, 24)
    monkeypatch.setattr(auth.clock, "today", lambda: day1)
    secured.get("/api/status")
    with db.connect() as conn:
        first = users.get_by_id(conn, uid)["last_active_at"]
    assert first and first > 0                                    # got a fresh, real timestamp

    secured.get("/api/status")                                    # a second request, same mocked day
    with db.connect() as conn:
        second = users.get_by_id(conn, uid)["last_active_at"]
    assert second == first                                        # not rewritten - same calendar day

    monkeypatch.setattr(auth.clock, "today", lambda: day1 + timedelta(days=1))
    secured.get("/api/status")
    with db.connect() as conn:
        third = users.get_by_id(conn, uid)["last_active_at"]
    assert third > first                                          # a new day - recorded again


@pytest.mark.parametrize("last_active_at,today,expected", [
    (None, date(2026, 9, 24), True),                                                    # never recorded
    (datetime(2026, 9, 23, 23, 59).timestamp(), date(2026, 9, 24), True),                # yesterday
    (datetime(2026, 9, 24, 0, 1).timestamp(), date(2026, 9, 24), False),                 # earlier today
    (datetime(2026, 9, 24, 23, 0).timestamp(), date(2026, 9, 24), False),                # later today
])
def test_is_new_day(last_active_at, today, expected):
    assert users.is_new_day(last_active_at, today) is expected


# ---- admin-generated password reset links (no email sending yet - see app/auth.py docstring) -----

def _register_alex(secured):
    """pete invites and an anonymous client registers as alex - leaves `secured` logged back in as pete
    when it returns, so callers can immediately do admin actions on it."""
    log_in(secured)
    token = secured.post("/api/invites").json()["token"]
    secured.post("/logout")
    friend = TestClient(main.app, follow_redirects=False)
    register(friend, token, username="alex")
    log_in(secured)
    return friend


def test_only_admin_can_create_a_reset_link(secured):
    friend = _register_alex(secured)
    friend.post("/login", data={"username": "alex", "password": "friend-password"})
    assert friend.post("/api/accounts/pete/reset-link").status_code == 403
    assert secured.post("/api/accounts/alex/reset-link").status_code == 200   # pete can, for any account incl. their own


def test_reset_link_for_an_unknown_account_404s(secured):
    log_in(secured)
    assert secured.post("/api/accounts/nobody/reset-link").status_code == 404


def test_full_password_reset_flow(secured):
    _register_alex(secured)

    r = secured.post("/api/accounts/alex/reset-link")
    assert r.status_code == 200
    reset_token = r.json()["token"]
    assert r.json()["url"] == "/reset-password?token=%s" % reset_token
    assert r.json()["expires_in_hours"] == 24

    alex = TestClient(main.app, follow_redirects=False)
    page = alex.get("/reset-password", params={"token": reset_token})
    assert page.status_code == 200 and 'name="password"' in page.text

    r2 = alex.post("/reset-password", data={"token": reset_token, "password": "brand-new-password",
                                            "password2": "brand-new-password"})
    assert r2.status_code == 303 and r2.headers["location"] == "/"
    assert "set-cookie" in r2.headers                             # resetting logs them straight in, like registering
    assert alex.get("/api/status").json()["username"] == "alex"

    # the old password no longer works, the new one does
    fresh = TestClient(main.app, follow_redirects=False)
    assert fresh.post("/login", data={"username": "alex", "password": "friend-password"}).status_code == 401
    assert fresh.post("/login", data={"username": "alex", "password": "brand-new-password"}).status_code == 303

    # the same link can't be redeemed twice
    again = TestClient(main.app, follow_redirects=False)
    r3 = again.post("/reset-password", data={"token": reset_token, "password": "another-one-entirely",
                                              "password2": "another-one-entirely"})
    assert r3.status_code == 400 and "already been used" in r3.text


def test_reset_link_rejects_mismatched_or_short_passwords(secured):
    _register_alex(secured)
    reset_token = secured.post("/api/accounts/alex/reset-link").json()["token"]

    mismatched = TestClient(main.app, follow_redirects=False).post(
        "/reset-password", data={"token": reset_token, "password": "one-password", "password2": "a-different-one"})
    assert mismatched.status_code == 400 and "match" in mismatched.text

    too_short = TestClient(main.app, follow_redirects=False).post(
        "/reset-password", data={"token": reset_token, "password": "short", "password2": "short"})
    assert too_short.status_code == 400 and "8 characters" in too_short.text


def test_reset_link_rejects_an_unknown_token(secured):
    log_in(secured)   # just to get an initialised database - this token was never issued by it
    r = TestClient(main.app, follow_redirects=False).post(
        "/reset-password", data={"token": "not-a-real-token", "password": "whatever-12345", "password2": "whatever-12345"})
    assert r.status_code == 400 and "reset link isn" in r.text   # "isn't" - html.escape turns the apostrophe into &#x27;


def test_reset_link_rejects_an_expired_token(secured):
    _register_alex(secured)
    reset_token = secured.post("/api/accounts/alex/reset-link").json()["token"]
    with db.connect() as conn:
        conn.execute("UPDATE password_resets SET expires_at = 1 WHERE token = ?", (reset_token,))

    r = TestClient(main.app, follow_redirects=False).post(
        "/reset-password", data={"token": reset_token, "password": "whatever-12345", "password2": "whatever-12345"})
    assert r.status_code == 400 and "expired" in r.text


def test_reset_password_page_is_public_but_redirects_once_already_logged_in(secured):
    anon = TestClient(main.app, follow_redirects=False)
    assert anon.get("/reset-password", params={"token": "x"}).status_code == 200   # public: no session needed

    log_in(secured)
    r = secured.get("/reset-password", params={"token": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/"                    # already signed in - nothing to do


@pytest.mark.parametrize("field,value,message", [
    ("username", "ab", "3-20 characters"),                 # too short
    ("username", "Has Spaces", "3-20 characters"),
    ("password", "short", "at least"),                      # too short
    ("first_name", "", "first and last name"),
    ("last_name", "", "first and last name"),
    ("email", "not-an-email", "valid email"),
    ("email", "", "valid email"),
])
def test_registration_validates_fields(secured, field, value, message):
    token = make_invite(secured)
    form = {"invite": token, "first_name": "New", "last_name": "User", "email": "newuser@example.com",
            "username": "newuser", "password": "a-fine-password", "password2": "a-fine-password"}
    form[field] = value
    if field == "password":
        form["password2"] = value
    r = TestClient(main.app, follow_redirects=False).post("/register", data=form)
    assert r.status_code == 400 and message in r.text


def test_registration_rejects_a_taken_email(secured):
    token = make_invite(secured)
    with db.connect() as conn:   # pete (bootstrap admin) has no email yet, so seed one directly to test against
        conn.execute("UPDATE users SET email = ? WHERE username = ?", ("pete@example.com", "pete"))
    r = TestClient(main.app, follow_redirects=False).post("/register", data={
        "invite": token, "first_name": "New", "last_name": "User", "email": "PETE@EXAMPLE.COM",   # different case
        "username": "newuser", "password": "a-fine-password", "password2": "a-fine-password"})
    assert r.status_code == 400 and "already uses that email" in r.text


def test_registration_prefills_the_form_on_error_but_never_the_password(secured):
    token = make_invite(secured)
    r = TestClient(main.app, follow_redirects=False).post("/register", data={
        "invite": token, "first_name": "Alex", "last_name": "Friend", "email": "alex@example.com",
        "username": "pete", "password": "a-fine-password", "password2": "a-fine-password"})   # username taken
    assert r.status_code == 400 and 'value="Alex"' in r.text and "alex@example.com" in r.text
    assert "a-fine-password" not in r.text


def test_registration_rejects_mismatched_passwords(secured):
    token = make_invite(secured)
    r = register(TestClient(main.app, follow_redirects=False), token, password="one-password", password2="different")
    assert r.status_code == 400 and "match" in r.text and "Passwords" in r.text


def test_registration_rejects_a_taken_username(secured):
    token = make_invite(secured)
    r = register(TestClient(main.app, follow_redirects=False), token, username="pete")
    assert r.status_code == 400 and "taken" in r.text


def test_bad_or_expired_invite_is_refused(secured):
    client = TestClient(main.app, follow_redirects=False)
    r = register(client, "not-a-real-token", username="x")
    assert r.status_code == 400 and "invite link" in r.text and "valid" in r.text

    token = make_invite(secured)
    with db.connect() as conn:
        conn.execute("UPDATE invites SET expires_at = ? WHERE token = ?", (time.time() - 1, token))
    r = register(client, token, username="y")
    assert r.status_code == 400 and "expired" in r.text


def test_max_users_cap(secured, monkeypatch):
    monkeypatch.setenv("MAX_USERS", "2")     # pete + 1 more
    log_in(secured)
    r = secured.post("/api/invites")
    assert r.status_code == 200
    token = r.json()["token"]
    register(TestClient(main.app, follow_redirects=False), token, username="second", email="second@example.com")
    # at the cap: no more invites can be created...
    assert secured.post("/api/invites").status_code == 400
    # ...and a still-valid pre-existing invite can't be redeemed past the cap either
    with db.connect() as conn:
        token2 = users.create_invite(conn, admin_id(secured))
    r2 = register(TestClient(main.app, follow_redirects=False), token2, username="third", email="third@example.com")
    assert r2.status_code == 400 and "full" in r2.text


# ---- data isolation between accounts --------------------------------------------------------------

def test_two_accounts_never_see_each_others_data(secured):
    token = make_invite(secured)
    friend = TestClient(main.app, follow_redirects=False)
    register(friend, token)

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
