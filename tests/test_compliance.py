"""What Strava's API policy and UK GDPR ask of the app: a privacy policy, revoking + deleting on disconnect or
account deletion, a way to get your data out, and the webhook that keeps us in step when someone revokes on
Strava's side. Strava itself is faked with an httpx mock transport - nothing here talks to the real API."""
import base64
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, strava, users, webhook

PASSWORD = "correct horse battery staple"
SECRET = "a-long-random-session-secret-value"
VERIFY = "a-verify-token-for-tests"


class FakeStrava:
    """Stands in for Strava: records every request and answers from a few settable knobs."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.revoke_status, self.refresh_status, self.activity_status = 200, 200, 200
        self.activity_name = "Renamed on Strava"
        self.network_down = False
        monkeypatch.setattr(strava, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(self.handle)))

    def handle(self, request):
        self.calls.append(request)
        url = str(request.url)
        if self.network_down:
            raise httpx.ConnectError("down")
        if url.startswith(strava.REVOKE_URL):
            return httpx.Response(self.revoke_status)
        if url.startswith(strava.TOKEN_URL):
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"message": "Bad Request"})
            return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2",
                                              "expires_at": int(time.time()) + 21600})
        if "/activities/" in url:
            if self.activity_status != 200:
                return httpx.Response(self.activity_status, json={})
            aid = int(url.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": aid, "name": self.activity_name, "sport_type": "Run",
                                              "start_date": "2026-09-01T07:00:00Z",
                                              "start_date_local": "2026-09-01T07:00:00Z", "distance": 5000.0,
                                              "moving_time": 1500})
        return httpx.Response(404)

    def urls(self):
        return [str(c.url) for c in self.calls]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "c.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shhh")
    monkeypatch.setenv("PRIVACY_CONTACT_EMAIL", "privacy@example.com")
    # a developer's own .env may switch the webhook on; these tests start from "off"
    monkeypatch.delenv("STRAVA_WEBHOOK_VERIFY_TOKEN", raising=False)
    monkeypatch.delenv("STRAVA_WEBHOOK_SUBSCRIPTION_ID", raising=False)
    return monkeypatch


@pytest.fixture()
def client(env):
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


@pytest.fixture()
def fake(env):
    return FakeStrava(env)


def login(c, username="pete", password=PASSWORD):
    r = c.post("/login", data={"username": username, "password": password})
    assert r.status_code == 303, r.text
    return c


def seed(uid, athlete_id, n=3):
    """A Strava link, n activities, a plan session and a last-sync marker for this user."""
    with db.connect() as conn:
        conn.execute("INSERT INTO strava_auth (user_id, athlete_id, athlete_name, access_token, refresh_token, "
                     "expires_at, scope) VALUES (?, ?, 'Someone', ?, ?, ?, 'read,activity:read_all')",
                     (uid, athlete_id, "AT-%d" % uid, "RT-%d" % uid, int(time.time()) + 9999))
        for i in range(n):
            conn.execute(strava.UPSERT, strava.activity_row({
                "id": uid * 1000 + i, "name": "Run %d" % i, "sport_type": "Run", "distance": 5000.0,
                "moving_time": 1500, "start_date": "2026-09-0%dT07:00:00Z" % (i + 1)}, uid))
        conn.execute("INSERT INTO plan (user_id, date, sport_group, position) VALUES (?, '2026-09-01', 'run', 0)", (uid,))
        db.set_meta(conn, uid, "last_sync", "2026-09-05T07:00:00")


def counts(uid):
    with db.connect() as conn:
        return {t: conn.execute("SELECT COUNT(*) FROM %s WHERE user_id = ?" % t, (uid,)).fetchone()[0]
                for t in ("activities", "plan", "strava_auth", "meta")}


def add_user(username="alex", password="friend-password", **kw):
    with db.connect() as conn:
        return users.create(conn, username, password, first_name="Al", last_name="Ex",
                            email="%s@example.com" % username, **kw)


def pete_id():
    with db.connect() as conn:
        return users.get_by_username(conn, "pete")["id"]


# ---- privacy policy ---------------------------------------------------------------------------

def test_privacy_policy_is_public_and_says_the_important_things(client):
    r = TestClient(main.app).get("/privacy")   # no session at all
    assert r.status_code == 200
    for needle in ("Strava", "activity:read_all", "Delete my account", "Download my data", "UK GDPR",
                   "privacy@example.com", "ico.org.uk"):
        assert needle in r.text, needle


def test_privacy_policy_escapes_operator_supplied_text(client, env):
    env.setenv("PRIVACY_OPERATOR_NAME", "<script>alert(1)</script>")
    env.setenv("PRIVACY_CONTACT_EMAIL", 'x"><img src=x>@e.com')
    text = client.get("/privacy").text
    assert "<script>alert(1)</script>" not in text and '"><img' not in text


def test_privacy_policy_only_promises_revocation_handling_if_the_webhook_is_really_on(client, env):
    assert "Strava notifies us" not in client.get("/privacy").text
    assert "Disconnect Strava" in client.get("/privacy").text
    env.setenv("STRAVA_WEBHOOK_VERIFY_TOKEN", VERIFY)
    assert "Strava notifies us" in client.get("/privacy").text


def test_every_page_links_the_privacy_policy_and_credits_strava(client):
    for html in (auth._login_html("/"), auth._register_html("t"), auth._reset_password_html("t")):
        assert 'href="/privacy"' in html
    index = client.get("/static/index.html").text
    assert 'href="/privacy"' in index and "Powered by Strava" in index
    for asset in ("btn_strava_connect_with_orange.svg", "api_logo_pwrdBy_strava_horiz_orange.svg"):
        assert client.get("/static/strava/" + asset).status_code == 200


# ---- disconnecting Strava ---------------------------------------------------------------------

def test_disconnect_revokes_at_strava_then_deletes_only_this_accounts_strava_data(client, fake):
    pete, alex = pete_id(), add_user()
    seed(pete, 111)
    seed(alex, 222)
    login(client)

    r = client.post("/api/account/disconnect-strava")
    assert r.json() == {"strava_revoked": True, "activities_deleted": 3}

    revoke = [c for c in fake.calls if str(c.url) == strava.REVOKE_URL]
    assert len(revoke) == 1 and revoke[0].method == "POST"
    assert revoke[0].headers["authorization"] == "Basic " + base64.b64encode(b"12345:shhh").decode()
    assert b"token=RT-%d" % pete in revoke[0].content   # the refresh token: revoking it kills the access tokens too

    assert counts(pete) == {"activities": 0, "plan": 1, "strava_auth": 0, "meta": 0}   # plan is theirs, so it stays
    assert counts(alex) == {"activities": 3, "plan": 1, "strava_auth": 1, "meta": 1}    # untouched


@pytest.mark.parametrize("how", ["strava_says_no", "network_down"])
def test_disconnect_still_deletes_locally_when_strava_cannot_be_told(client, fake, how):
    seed(pete_id(), 111)
    login(client)
    if how == "strava_says_no":
        fake.revoke_status = 500
    else:
        fake.network_down = True
    r = client.post("/api/account/disconnect-strava").json()
    assert r["strava_revoked"] is False and r["activities_deleted"] == 3   # reported honestly, deleted regardless
    assert counts(pete_id())["activities"] == 0


def test_disconnect_with_nothing_connected_is_harmless(client, fake):
    login(client)
    assert client.post("/api/account/disconnect-strava").json() == {"strava_revoked": None, "activities_deleted": 0}
    assert fake.calls == []


# ---- deleting your own account ----------------------------------------------------------------

def test_delete_account_needs_the_right_password(client, fake):
    alex = add_user()
    seed(alex, 222)
    c = login(TestClient(main.app, follow_redirects=False), "alex", "friend-password")
    r = c.post("/api/account/delete", json={"password": "not-it"})
    assert r.status_code == 403
    assert counts(alex)["activities"] == 3
    with db.connect() as conn:
        assert users.get_by_id(conn, alex) is not None
    assert fake.calls == []   # nothing was revoked either


def test_delete_account_removes_everything_belonging_to_it_and_nothing_else(client, fake):
    pete, alex = pete_id(), add_user()
    seed(pete, 111)
    seed(alex, 222)
    with db.connect() as conn:
        users.create_invite(conn, alex)                                   # an invite alex issued
        users.create_reset_link(conn, alex, pete)                         # a reset link for alex
        users.create_reset_link(conn, pete, alex)                         # ...and one alex issued
        token = users.create_invite(conn, pete)                           # pete's invite that alex redeemed
        conn.execute("UPDATE invites SET used_by = ? WHERE token = ?", (alex, token))

    c = login(TestClient(main.app, follow_redirects=False), "alex", "friend-password")
    r = c.post("/api/account/delete", json={"password": "friend-password"})
    assert r.status_code == 200 and r.json()["deleted"] is True and r.json()["strava_revoked"] is True
    assert "tt_session=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]   # signed out

    assert any(str(x.url) == strava.REVOKE_URL for x in fake.calls)       # revoked before the tokens were deleted
    with db.connect() as conn:
        assert users.get_by_id(conn, alex) is None
        assert conn.execute("SELECT COUNT(*) FROM invites WHERE created_by = ?", (alex,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM password_resets WHERE user_id = ? OR created_by = ?",
                            (alex, alex)).fetchone()[0] == 0
        assert conn.execute("SELECT used_by FROM invites WHERE token = ?", (token,)).fetchone()["used_by"] is None
        assert users.get_by_id(conn, pete) is not None
    assert counts(alex) == {"activities": 0, "plan": 0, "strava_auth": 0, "meta": 0}
    assert counts(pete) == {"activities": 3, "plan": 1, "strava_auth": 1, "meta": 1}
    assert TestClient(main.app).post("/login", data={"username": "alex", "password": "friend-password"}).status_code == 401


def test_the_only_admin_cannot_delete_themselves_but_one_of_two_can(client, fake):
    login(client)
    assert client.post("/api/account/delete", json={"password": PASSWORD}).status_code == 400
    with db.connect() as conn:
        assert users.get_by_id(conn, pete_id()) is not None
    add_user("second", "another-password", is_admin=True)
    assert client.post("/api/account/delete", json={"password": PASSWORD}).status_code == 200


def test_delete_shares_the_login_forms_wrong_guess_brake(client):
    add_user()
    c = login(TestClient(main.app, follow_redirects=False), "alex", "friend-password")
    for _ in range(auth.MAX_FAILURES):
        assert c.post("/api/account/delete", json={"password": "nope"}).status_code == 403
    assert c.post("/api/account/delete", json={"password": "friend-password"}).status_code == 429


# ---- an admin deleting someone else ------------------------------------------------------------

def test_admin_can_delete_another_account_and_it_revokes_their_strava_access(client, fake):
    alex = add_user()
    seed(alex, 222)
    login(client)
    r = client.delete("/api/accounts/alex")
    assert r.status_code == 200 and r.json()["activities_deleted"] == 3 and r.json()["strava_revoked"] is True
    with db.connect() as conn:
        assert users.get_by_username(conn, "alex") is None
    assert b"token=RT-%d" % alex in [c for c in fake.calls if str(c.url) == strava.REVOKE_URL][0].content


def test_admin_delete_guards(client, fake):
    add_user()
    add_user("bob", "bob-password")
    login(client)
    assert client.delete("/api/accounts/pete").status_code == 400      # yourself: use the Account page
    assert client.delete("/api/accounts/nobody").status_code == 404
    friend = login(TestClient(main.app, follow_redirects=False), "alex", "friend-password")
    assert friend.delete("/api/accounts/bob").status_code == 403       # not an admin


def test_the_demo_account_cannot_be_deleted_or_used_to_delete(tmp_path, env):
    env.setenv("DEMO_ACCOUNT", "1")
    FakeStrava(env)
    with TestClient(main.app, follow_redirects=False) as c:
        admin = login(c)
        assert admin.delete("/api/accounts/demo").status_code == 400
        demo = login(TestClient(main.app, follow_redirects=False), "demo", "demo")
        assert demo.post("/api/account/disconnect-strava").status_code == 403
        assert demo.post("/api/account/delete", json={"password": "demo"}).status_code == 403
        assert demo.get("/api/account/export").status_code == 200      # reading is fine


# ---- downloading your data -------------------------------------------------------------------

def test_export_contains_your_data_and_nothing_else(client, fake):
    pete, alex = pete_id(), add_user()
    seed(pete, 111)
    seed(alex, 222)
    login(client)
    r = client.get("/api/account/export")
    assert 'attachment; filename="training-tracker-data.json"' == r.headers["content-disposition"]
    data = r.json()
    assert data["account"]["username"] == "pete"
    assert len(data["activities"]) == 3 and {a["id"] for a in data["activities"]} == {pete * 1000 + i for i in range(3)}
    assert len(data["plan"]) == 1 and data["strava"]["athlete_id"] == 111
    for secret in ("AT-", "RT-", "password_hash", "pbkdf2"):
        assert secret not in r.text                                   # credentials aren't "your data"
    assert "alex" not in r.text and "Run" in r.text


# ---- the Strava webhook: validation ------------------------------------------------------------

def test_webhook_validation_echoes_the_challenge_only_for_the_right_token(client, env):
    q = {"hub.mode": "subscribe", "hub.challenge": "abc123", "hub.verify_token": VERIFY}
    assert client.get("/strava/webhook", params=q).status_code == 404       # not switched on yet
    env.setenv("STRAVA_WEBHOOK_VERIFY_TOKEN", VERIFY)
    r = client.get("/strava/webhook", params=q)
    assert r.status_code == 200 and r.json() == {"hub.challenge": "abc123"}
    assert client.get("/strava/webhook", params={**q, "hub.verify_token": "wrong"}).status_code == 404
    assert client.get("/strava/webhook", params={**q, "hub.mode": "unsubscribe"}).status_code == 404
    assert client.get("/strava/webhook", params={**q, "hub.challenge": ""}).status_code == 404


def test_webhook_events_are_refused_when_off_and_bad_json_is_a_400(client, env):
    assert client.post("/strava/webhook", json={"a": 1}).status_code == 404
    env.setenv("STRAVA_WEBHOOK_VERIFY_TOKEN", VERIFY)
    assert client.post("/strava/webhook", content=b"not json").status_code == 400
    assert client.post("/strava/webhook", json={"object_type": "activity"}).json() == {}   # acknowledged


# ---- the Strava webhook: events ----------------------------------------------------------------

DEAUTH = {"object_type": "athlete", "aspect_type": "update", "owner_id": 111, "object_id": 111,
          "subscription_id": 9, "updates": {"authorized": "false"}}


@pytest.fixture()
def hooked(client, env, fake):
    env.setenv("STRAVA_WEBHOOK_VERIFY_TOKEN", VERIFY)
    pete, alex = pete_id(), add_user()
    seed(pete, 111)
    seed(alex, 222)
    return client, fake, pete, alex


def test_a_genuine_deauthorisation_deletes_that_athletes_strava_data_only(hooked):
    client, fake, pete, alex = hooked
    fake.refresh_status = 400            # Strava confirms: that token no longer works
    assert client.post("/strava/webhook", json=DEAUTH).status_code == 200
    assert counts(pete) == {"activities": 0, "plan": 1, "strava_auth": 0, "meta": 0}
    assert counts(alex)["activities"] == 3 and counts(alex)["strava_auth"] == 1


def test_a_forged_deauthorisation_is_ignored_because_the_token_still_works(hooked):
    client, fake, pete, _ = hooked
    client.post("/strava/webhook", json=DEAUTH)
    assert counts(pete)["activities"] == 3 and counts(pete)["strava_auth"] == 1
    assert any(str(c.url).startswith(strava.TOKEN_URL) for c in fake.calls)   # it did check with Strava


def test_deauthorisation_deletes_even_if_strava_cannot_be_asked(hooked):
    client, fake, pete, _ = hooked
    fake.network_down = True   # the deletion obligation has a deadline; a re-connect is cheap
    client.post("/strava/webhook", json=DEAUTH)
    assert counts(pete)["activities"] == 0


def test_events_for_athletes_we_do_not_hold_do_nothing(hooked):
    client, fake, pete, alex = hooked
    fake.refresh_status = 400
    client.post("/strava/webhook", json={**DEAUTH, "owner_id": 999})
    assert fake.calls == [] and counts(pete)["activities"] == 3 and counts(alex)["activities"] == 3


def test_events_for_another_subscription_are_ignored_when_pinned(hooked, env):
    client, fake, pete, _ = hooked
    env.setenv("STRAVA_WEBHOOK_SUBSCRIPTION_ID", "9")
    fake.refresh_status = 400
    client.post("/strava/webhook", json={**DEAUTH, "subscription_id": 1234})
    assert counts(pete)["activities"] == 3
    client.post("/strava/webhook", json=DEAUTH)                     # the right subscription id
    assert counts(pete)["activities"] == 0


def test_the_demo_accounts_fake_strava_link_is_never_touched(env, fake):
    env.setenv("STRAVA_WEBHOOK_VERIFY_TOKEN", VERIFY)
    env.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False) as c:
        with db.connect() as conn:
            demo = users.get_demo_account(conn)["id"]
        before = counts(demo)
        fake.refresh_status = 400
        c.post("/strava/webhook", json={**DEAUTH, "owner_id": 0})   # the demo's fake athlete id
        assert counts(demo) == before and before["activities"] > 0


def event(kind, aspect, object_id, owner=111):
    return {"object_type": kind, "aspect_type": aspect, "object_id": object_id, "owner_id": owner,
            "subscription_id": 9, "updates": {}}


def test_an_activity_deleted_on_strava_is_deleted_here(hooked):
    client, fake, pete, _ = hooked
    fake.activity_status = 404
    client.post("/strava/webhook", json=event("activity", "delete", pete * 1000))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activities WHERE user_id = ? AND id = ?",
                            (pete, pete * 1000)).fetchone()[0] == 0
    assert counts(pete)["activities"] == 2


def test_a_forged_activity_delete_is_ignored_if_strava_still_has_it(hooked):
    client, fake, pete, _ = hooked
    client.post("/strava/webhook", json=event("activity", "delete", pete * 1000))   # Strava answers 200
    assert counts(pete)["activities"] == 3


def test_an_edit_on_strava_refreshes_our_copy(hooked):
    client, fake, pete, _ = hooked
    client.post("/strava/webhook", json=event("activity", "update", pete * 1000))
    with db.connect() as conn:
        name = conn.execute("SELECT name FROM activities WHERE user_id = ? AND id = ?", (pete, pete * 1000)).fetchone()["name"]
    assert name == "Renamed on Strava"


def test_when_strava_cannot_confirm_an_activity_event_nothing_is_deleted(hooked):
    client, fake, pete, _ = hooked
    fake.activity_status = 429      # rate limited: can't tell, so a forged event mustn't cost anyone history
    client.post("/strava/webhook", json=event("activity", "delete", pete * 1000))
    assert counts(pete)["activities"] == 3


def test_unknown_activities_and_create_events_cost_no_strava_calls(hooked):
    client, fake, pete, _ = hooked
    client.post("/strava/webhook", json=event("activity", "delete", 424242))
    client.post("/strava/webhook", json=event("activity", "create", pete * 1000))
    assert fake.calls == []


# ---- strava.py helpers ------------------------------------------------------------------------

def test_revoke_reports_what_happened(env, fake, client):
    with db.connect() as conn:
        with fake_client(fake) as http:
            assert strava.revoke(conn, http, 777) is None                     # nothing stored
    seed(pete_id(), 111)
    env.delenv("STRAVA_CLIENT_ID")
    with db.connect() as conn, fake_client(fake) as http:
        assert strava.revoke(conn, http, pete_id()) is False                  # can't authenticate without credentials


def fake_client(fake):
    return httpx.Client(transport=httpx.MockTransport(fake.handle))


def test_subscription_helpers_call_stravas_endpoints_with_the_apps_credentials(env, fake):
    with fake_client(fake) as http:
        fake_handle = fake.handle

        def handle(request):
            fake.calls.append(request)
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 9}])
            return httpx.Response(201, json={"id": 9})
        http = httpx.Client(transport=httpx.MockTransport(handle))
        assert strava.create_subscription(http, "https://x.example/strava/webhook", VERIFY) == {"id": 9}
        assert strava.view_subscription(http) == [{"id": 9}]
        assert strava.delete_subscription(http, 9) is None
    post, get, delete = fake.calls
    body = post.content.decode()
    assert "client_id=12345" in body and "client_secret=shhh" in body and "verify_token=" + VERIFY in body
    assert "callback_url=https%3A%2F%2Fx.example%2Fstrava%2Fwebhook" in body
    assert "client_id=12345" in str(get.url) and str(delete.url).startswith(strava.SUBSCRIPTIONS_URL + "/9")


def test_subscription_errors_carry_stravas_status(env, fake):
    fake.network_down = False
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="callback url not verifiable")))
    with pytest.raises(strava.StravaError) as e:
        strava.create_subscription(http, "https://x.example/w", VERIFY)
    assert e.value.status == 400 and "not verifiable" in str(e.value)
