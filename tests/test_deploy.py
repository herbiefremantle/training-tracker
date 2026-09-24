"""Railway deployment behaviour: $PORT, /data volume, /health, env-only secrets, Railway hostnames, build files."""
import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import __main__ as launcher
from app import db, main, strava

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "app.db"))
    for var in ("ALLOWED_HOSTS", "RAILWAY_PUBLIC_DOMAIN", "STRAVA_REDIRECT_URI"):
        monkeypatch.delenv(var, raising=False)
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


# ---- 2. binds to $PORT ------------------------------------------------------------------------

def test_port_and_host_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    assert launcher.server_config() == ("127.0.0.1", 8000)          # safe local default
    monkeypatch.setenv("PORT", "43117")                              # what Railway does
    monkeypatch.setenv("HOST", "0.0.0.0")                            # what the Dockerfile does
    assert launcher.server_config() == ("0.0.0.0", 43117)


def test_bad_port_fails_loudly(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-number")
    with pytest.raises(SystemExit, match="PORT must be a number"):
        launcher.server_config()


# ---- 3. secrets only from the environment ----------------------------------------------------

def test_strava_credentials_are_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
    assert not strava.is_configured()
    monkeypatch.setenv("STRAVA_CLIENT_ID", " 123 ")               # stray whitespace from a paste is tolerated
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "abc")
    assert strava.is_configured() and strava.client_id() == "123"


def test_no_secret_like_values_in_the_source():
    """Guards against a credential being pasted into code: no 40-hex-char strings, no assigned client secrets."""
    offenders = []
    text_files = [p for p in (ROOT / "static").rglob("*") if p.is_file() and p.suffix in
                 (".js", ".css", ".html", ".json")]   # skip icons/binaries - not where a pasted secret would land
    for path in list((ROOT / "app").glob("*.py")) + text_files + [ROOT / "Dockerfile", ROOT / "railway.json"]:
        text = path.read_text()
        if re.search(r"\b[0-9a-f]{40}\b", text) or re.search(r"CLIENT_SECRET\s*=\s*['\"]?[A-Za-z0-9]{8,}", text):
            offenders.append(path.name)
    assert offenders == []


def test_redirect_uri_follows_the_railway_domain(monkeypatch):
    monkeypatch.delenv("STRAVA_REDIRECT_URI", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    assert strava.redirect_uri() == "http://localhost:8000/auth/callback"
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "tracker-production.up.railway.app")
    assert strava.redirect_uri() == "https://tracker-production.up.railway.app/auth/callback"
    monkeypatch.setenv("STRAVA_REDIRECT_URI", "https://mytracker.example.com/auth/callback")   # explicit wins
    assert strava.redirect_uri() == "https://mytracker.example.com/auth/callback"


# ---- 4. database on the volume ---------------------------------------------------------------

def test_database_is_created_at_the_configured_path_including_missing_folders(tmp_path, monkeypatch):
    target = tmp_path / "data" / "nested" / "app.db"               # folder doesn't exist yet
    monkeypatch.setenv("FITNESS_DB", str(target))
    assert db.db_path() == str(target)
    db.init_db()
    assert target.exists()
    with sqlite3.connect(target) as c:
        assert {"users", "invites", "strava_auth", "activities", "plan", "meta"} <= \
            {r[0] for r in c.execute("SELECT name FROM sqlite_master")}


def test_default_database_path_is_local_when_unset(monkeypatch):
    monkeypatch.delenv("FITNESS_DB", raising=False)
    assert db.db_path() == str(ROOT / "training.db")


def test_data_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("FITNESS_DB", str(tmp_path / "app.db"))
    with TestClient(main.app) as c:
        c.post("/api/plan/import", json={"text": "date,sport\n2026-09-28,Run\n"})
    with TestClient(main.app) as c:                                 # a fresh start of the app, same file
        assert c.get("/api/status").json()["plan_count"] == 1


def test_volume_warning_only_when_data_is_not_a_mount(monkeypatch):
    monkeypatch.setenv("FITNESS_DB", "/data/app.db")
    monkeypatch.setattr("os.path.ismount", lambda p: False)
    assert "no volume is mounted at /data" in db.volume_warning()
    monkeypatch.setattr("os.path.ismount", lambda p: p == "/data")
    assert db.volume_warning() is None
    monkeypatch.setenv("FITNESS_DB", "/somewhere/else.db")           # local runs never warn
    monkeypatch.setattr("os.path.ismount", lambda p: False)
    assert db.volume_warning() is None


# ---- 5. /health -----------------------------------------------------------------------------

def test_health_returns_200_for_the_railway_healthcheck_host(client):
    r = client.get("/health", headers={"host": "healthcheck.railway.app"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_needs_no_strava_setup_and_no_login(client, monkeypatch):
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    assert client.get("/health").status_code == 200


def test_health_reports_503_when_the_database_is_unreachable(client, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(main, "connect", broken)
    assert client.get("/health").status_code == 503


# ---- Railway hostnames (would otherwise be rejected as an unknown Host) ----------------------

@pytest.mark.parametrize("host", ["tracker-production.up.railway.app", "tracker-production.up.railway.app:443", "example.com"])
def test_public_host_refused_until_configured(client, host):
    assert client.get("/api/status", headers={"host": host}).status_code == 400


def test_railway_public_domain_is_accepted(client, monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "tracker-production.up.railway.app")
    assert client.get("/api/status", headers={"host": "tracker-production.up.railway.app"}).status_code == 200
    assert client.get("/api/status", headers={"host": "evil.up.railway.app"}).status_code == 400   # only *your* domain


def test_custom_domains_via_allowed_hosts(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "train.example.com, https://alt.example.org/ ,")
    for host in ("train.example.com", "alt.example.org"):
        assert client.get("/api/status", headers={"host": host}).status_code == 200
    assert client.get("/api/status", headers={"host": "train.example.com.evil.com"}).status_code == 400


# ---- 1, 6, 7. build files ---------------------------------------------------------------------

def test_railway_json_is_valid_and_sane():
    cfg = json.loads((ROOT / "railway.json").read_text())
    assert cfg["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}
    d = cfg["deploy"]
    assert d["healthcheckPath"] == "/health" and d["numReplicas"] == 1        # SQLite: exactly one instance
    assert "$PORT" not in d["startCommand"] and not re.search(r"\b\d{4,5}\b", d["startCommand"])   # no hard-coded port
    assert d["startCommand"] == "python -m app"


def test_dockerfile_conventions():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.\d+-slim", text, re.M)
    assert "ENV FITNESS_DB=/data/app.db" in text
    assert 'CMD ["python", "-m", "app"]' in text
    assert not re.search(r"^\s*VOLUME\b", text, re.M)                        # unsupported on Railway
    assert not re.search(r"--port\s+\d+", text)                              # port comes from $PORT
    assert not re.search(r"COPY\s+.*\.env(?!\.example)\b", text)             # secrets never baked into the image
    assert "COPY requirements.txt" in text and "pip install -r requirements.txt" in text


def test_dockerignore_keeps_secrets_and_data_out_of_the_image():
    lines = (ROOT / ".dockerignore").read_text().split()
    assert {".env", "*.db", ".venv/", "tests/"} <= set(lines)


def test_requirements_are_fully_pinned():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text().splitlines():
            line = line.split("#")[0].strip()
            if line and not line.startswith("-r"):
                assert re.match(r"^[A-Za-z0-9_.\-]+==[0-9][^\s;]*(\s*;.*)?$", line), "%s: not pinned: %r" % (name, line)
    packages = {l.split("#")[0].split("==")[0].strip().lower()
                for l in (ROOT / "requirements.txt").read_text().splitlines() if "==" in l.split("#")[0]}
    assert "pytest" not in packages                                            # test tools stay out of the image
    assert {"fastapi", "uvicorn", "httpx", "python-dotenv"} <= packages


# ---- "Add to Home Screen": manifest, icons, service worker -------------------------------------

def test_manifest_is_valid_and_matches_what_the_pages_reference():
    manifest = json.loads((ROOT / "static" / "manifest.json").read_text())
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    assert len(manifest["icons"]) >= 2
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert (ROOT / icon["src"].lstrip("/")).is_file(), icon["src"]
        # Not "maskable": the branded icon artwork ships its own rounded corners and transparency (a finished
        # icon, not a full-bleed safe-zone design), so letting the OS additionally crop/mask it would double
        # up or clip the corners - "any" is the honest purpose for this artwork.
        assert icon.get("purpose") == "any"

    for page_path in ("static/index.html",):
        html = (ROOT / page_path).read_text()
        assert '<link rel="manifest" href="/static/manifest.json">' in html
        assert 'rel="apple-touch-icon"' in html and "apple-mobile-web-app-capable" in html


def test_login_and_register_pages_also_offer_the_manifest_and_install_button():
    """Not just the main app - a brand new invited friend hits /register, /login, or /reset-password first."""
    from app import auth
    for html in (auth._login_html("/"), auth._register_html("token"), auth._reset_password_html("token")):
        assert '<link rel="manifest" href="/static/manifest.json">' in html
        assert 'rel="apple-touch-icon"' in html
        assert 'id="install-slot"' in html and '/static/install.js' in html


def test_icons_are_real_square_images_at_the_declared_sizes():
    from PIL import Image
    for name, size in [("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)]:
        with Image.open(ROOT / "static" / "icons" / name) as img:
            assert img.size == (size, size), name
            assert img.mode in ("RGB", "RGBA"), name   # not a broken/empty file


def test_apple_touch_icon_has_no_transparency():
    """iOS applies its own squircle mask and doesn't support a transparent home-screen icon (historically fills
    transparent pixels with black) - this one must be fully opaque, unlike the manifest/favicon icons."""
    from PIL import Image
    with Image.open(ROOT / "static" / "icons" / "apple-touch-icon.png") as img:
        assert img.mode == "RGB"   # no alpha channel at all


# ---- the "colourful" theme switch ---------------------------------------------------------------

def test_colourful_theme_is_defined_and_never_follows_os_dark_mode():
    css = (ROOT / "static" / "style.css").read_text()
    assert ':root[data-theme="colourful"]' in css
    # the OS dark-mode media query must not also match when colourful is on, or the two would fight
    assert 'not([data-theme="colourful"])' in css


def test_index_page_offers_the_colourful_switch_and_brand_wordmark():
    html = (ROOT / "static" / "index.html").read_text()
    assert 'id="colour-seg"' in html
    assert 'data-act="colourMode"' in html and 'data-mode="colourful"' in html
    assert 'class="brand-training"' in html and 'class="brand-tracker"' in html
    assert '/static/icons/icon-192.png' in html


def test_auth_pages_apply_a_saved_colourful_choice_before_first_paint():
    """No toggle control on these pages (there's no topbar to put it in) - they just have to respect a choice
    already made in the main app, and do it early enough that switching pages never flashes the wrong theme."""
    from app import auth
    for html in (auth._login_html("/"), auth._register_html("token"), auth._reset_password_html("token")):
        assert 'localStorage.getItem("colourMode")' in html
        assert 'setAttribute("data-theme","colourful")' in html
    assert 'class="brand-tracker"' in auth._login_html("/")   # the login page also gets the branded wordmark


def test_service_worker_does_not_cache_aggressively():
    """A service worker that caches stale JS/HTML across a deploy would be a nasty, hard-to-diagnose bug for
    everyone using the installed app - confirm this one is deliberately a pass-through with no cache API use."""
    sw = (ROOT / "static" / "sw.js").read_text()
    assert "addEventListener(\"fetch\"" in sw
    assert "caches.open" not in sw and "cache.put" not in sw and ".put(event.request" not in sw
    assert "skipWaiting" in sw and "clients.claim" in sw   # a fixed sw.js takes over immediately, not next visit


def test_static_assets_referenced_by_the_pwa_all_exist():
    for rel in ("static/sw.js", "static/install.js", "static/manifest.json",
               "static/icons/icon-192.png", "static/icons/icon-512.png",
               "static/icons/apple-touch-icon.png", "static/icons/favicon-32.png"):
        assert (ROOT / rel).is_file(), rel
