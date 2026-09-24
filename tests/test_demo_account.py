"""The shared demo login (DEMO_ACCOUNT=1, username/password "demo"/"demo"): bootstrap, that it looks
"connected" with real-looking sample data straight from a cold start, and that every write is refused - see
app/demo_data.py for how the sample data itself is generated and tested."""
import pytest
from fastapi.testclient import TestClient

from app import db, main, strava

PASSWORD = "correct horse battery staple"
SECRET = "a-long-random-session-secret-value"


@pytest.fixture()
def demo_client(tmp_path, monkeypatch):
    """A client for an app with DEMO_ACCOUNT enabled *and* a real admin account, so both can be compared."""
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "demo.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def log_in_demo(client):
    return client.post("/login", data={"username": "demo", "password": "demo"})


def log_in_admin(client):
    return client.post("/login", data={"username": "pete", "password": PASSWORD})


# ---- bootstrap ------------------------------------------------------------------------------

def test_demo_account_is_not_created_without_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "no_demo.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    with TestClient(main.app, follow_redirects=False) as c:
        assert log_in_demo(c).status_code == 401


def test_demo_account_is_created_when_the_env_var_is_set(demo_client):
    r = log_in_demo(demo_client)
    assert r.status_code == 303   # /login's own redirect, always explicit 303
    me = demo_client.get("/api/status").json()
    assert me["username"] == "demo" and me["is_demo"] is True and me["is_admin"] is False


def test_demo_account_bootstrap_is_idempotent_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "restart.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False):
        pass   # first boot: creates it
    with TestClient(main.app, follow_redirects=False) as c:
        assert log_in_demo(c).status_code == 303   # second boot: still there, only the one
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username = 'demo'").fetchone()[0] == 1


def test_demo_password_defaults_to_demo_but_is_overridable(tmp_path, monkeypatch):
    """DEMO_PASSWORD exists because browsers' breached-password checkers (Chrome's Password Manager, at
    least) flag the literal word "demo" on sight - this is the escape hatch, without losing the account
    or needing a code change."""
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "custom_pw.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    monkeypatch.setenv("DEMO_PASSWORD", "a-less-common-demo-password")
    with TestClient(main.app, follow_redirects=False) as c:
        assert c.post("/login", data={"username": "demo", "password": "demo"}).status_code == 401
        assert c.post("/login", data={"username": "demo", "password": "a-less-common-demo-password"}).status_code == 303


def test_demo_password_change_takes_effect_on_the_next_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "rotate.db"))
    monkeypatch.setenv("APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DEMO_ACCOUNT", "1")
    with TestClient(main.app, follow_redirects=False) as c:
        assert log_in_demo(c).status_code == 303   # the default "demo" password works on first boot

    monkeypatch.setenv("DEMO_PASSWORD", "a-rotated-password")
    with TestClient(main.app, follow_redirects=False) as c:
        assert log_in_demo(c).status_code == 401   # the old password no longer works...
        assert c.post("/login", data={"username": "demo", "password": "a-rotated-password"}).status_code == 303
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username = 'demo'").fetchone()[0] == 1   # ...same account


# ---- what logging in as demo looks like, straight from a cold start ---------------------------

def test_demo_status_looks_connected_with_sample_data(demo_client):
    log_in_demo(demo_client)
    s = demo_client.get("/api/status").json()
    assert s["is_demo"] is True
    assert s["connected"] is True and s["athlete"] == "Demo Athlete"   # never a bare "Connect Strava" prompt
    assert s["activity_count"] > 0 and s["plan_count"] > 0


def test_demo_plan_is_already_populated_without_any_manual_regenerate_call(demo_client):
    """This is what proves the lifespan startup wiring actually ran - the test itself never calls
    demo_data.regenerate()."""
    log_in_demo(demo_client)
    sessions = demo_client.get("/api/plan").json()["sessions"]
    assert len(sessions) == 16 * 7   # the full 16-week marathon plan


# ---- read-only enforcement --------------------------------------------------------------------

def test_demo_cannot_sync(demo_client):
    log_in_demo(demo_client)
    assert demo_client.post("/api/sync").status_code == 403


def test_demo_cannot_import_or_clear_the_plan(demo_client):
    log_in_demo(demo_client)
    assert demo_client.post("/api/plan/import", json={"text": "date,sport\n2026-01-01,Run\n"}).status_code == 403
    assert demo_client.delete("/api/plan").status_code == 403


def test_demo_can_still_preview_without_saving(demo_client):
    """Read-only means read-only, not "can't touch anything" - browsing/previewing writes nothing and must
    still work, so a visitor can see how those features behave."""
    log_in_demo(demo_client)
    r = demo_client.post("/api/plan/import", json={"text": "date,sport\n2026-01-01,Run\n", "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] is True
    r2 = demo_client.post("/api/plan-templates/10k_beg/apply", json={"dry_run": True})
    assert r2.status_code == 200 and r2.json()["dry_run"] is True


def test_demo_cannot_apply_a_ready_made_plan_for_real(demo_client):
    log_in_demo(demo_client)
    assert demo_client.post("/api/plan-templates/10k_beg/apply", json={}).status_code == 403


def test_demo_cannot_connect_a_real_strava_account(demo_client):
    log_in_demo(demo_client)
    r = demo_client.get("/auth/login")
    assert r.status_code == 307
    assert "demo" in r.headers["location"].lower()


def test_demo_callback_also_refuses_even_with_a_crafted_state(demo_client):
    """Defence in depth: even if something reached /auth/callback directly, bypassing the /auth/login guard
    above, it still refuses - it doesn't just rely on never having issued a valid oauth state for this account."""
    log_in_demo(demo_client)
    r = demo_client.get("/auth/callback", params={"code": "x", "state": "y"})
    assert r.status_code == 307
    assert "demo" in r.headers["location"].lower()


def test_demo_cannot_reach_admin_endpoints(demo_client):
    log_in_demo(demo_client)
    assert demo_client.post("/api/invites").status_code == 403
    assert demo_client.get("/api/invites").status_code == 403


# ---- none of this affects a real account -------------------------------------------------------

def test_a_real_account_is_unaffected_by_any_of_the_demo_restrictions(demo_client, monkeypatch):
    log_in_admin(demo_client)
    assert demo_client.post("/api/plan/import", json={"text": "date,sport\n2026-01-01,Run\n"}).status_code == 200
    assert demo_client.delete("/api/plan").status_code == 200
    monkeypatch.setattr(strava, "is_configured", lambda: True)   # isolate the demo check from the separate
    r = demo_client.get("/auth/login")                            # "Strava isn't configured" redirect
    assert r.status_code == 307
    assert "auth_error" not in (r.headers.get("location") or "")
