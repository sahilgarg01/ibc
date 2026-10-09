import pytest


@pytest.fixture(autouse=True)
def isolated_sqlite(monkeypatch):
    # Tests must never modify the local PostgreSQL database from .env.
    monkeypatch.setenv("DATABASE_URL", "")
