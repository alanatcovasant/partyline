"""Pytest fixtures for Covasant Partyline server tests."""

import os
import sys
import pathlib

import pytest

# Ensure the app package is importable from tests.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "covasant-partyline"))

# Force local mode for tests.
os.environ.setdefault("PARTYLINE_LOCAL", "true")
os.environ.setdefault("PGHOST", "localhost")
os.environ.setdefault("PGPORT", "5432")
os.environ.setdefault("PGDATABASE", "partyline")
os.environ.setdefault("PGUSER", "partyline")
os.environ.setdefault("PGPASSWORD", "partyline")
os.environ.setdefault("PGSSLMODE", "disable")
os.environ.setdefault("PARTYLINE_AUTO_MIGRATE", "true")


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def app():
    from app.main import app as fastapi_app
    return fastapi_app


@pytest.fixture(scope="session")
async def started_app(app):
    """Start the app lifespan once for the entire test session."""
    from asgi_lifespan import LifespanManager

    async with LifespanManager(app) as manager:
        yield manager.app


@pytest.fixture()
async def client(started_app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=started_app),
        base_url="http://test",
    ) as ac:
        yield ac
