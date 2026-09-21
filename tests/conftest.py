import pytest

from app import auth


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    """Existing tests assume no login. Auth tests opt in with their own APP_PASSWORD."""
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("REQUIRE_AUTH", raising=False)
    auth._failures.clear()
    yield
    auth._failures.clear()
