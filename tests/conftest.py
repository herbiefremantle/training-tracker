from pathlib import Path

import pytest

from app import auth, db

_REAL_DB = Path(__file__).resolve().parent.parent / "training.db"


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    """Existing tests assume no login. Auth tests opt in with their own env vars and accounts."""
    for var in ("APP_PASSWORD", "REQUIRE_AUTH", "SESSION_SECRET", "ADMIN_USERNAME", "MAX_USERS"):
        monkeypatch.delenv(var, raising=False)
    auth._failures.clear()
    yield
    auth._failures.clear()


@pytest.fixture(autouse=True)
def never_touch_the_real_database():
    """Every test is expected to point FITNESS_DB at a temp path. This is a tripwire, not a mechanism: if a test
    (accidentally, e.g. via monkeypatch.undo() reverting FITNESS_DB mid-test) ever causes db.db_path() to
    resolve to the real local training.db next to the code, or that file's size changes during a test, fail
    loudly instead of silently reading or writing someone's real data."""
    before = _REAL_DB.stat().st_size if _REAL_DB.exists() else None
    yield
    after = _REAL_DB.stat().st_size if _REAL_DB.exists() else None
    assert before == after, "a test touched the real training.db next to the code - see never_touch_the_real_database"
