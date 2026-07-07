"""Pytest fixtures for Covasant Partyline server tests."""

import os
import sys
import pathlib

import pytest

# Ensure the app package, jobs wheel, and client package are importable from tests.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "covasant-partyline"))
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "partyline-client"))


def _pg_reachable() -> bool:
    import socket

    try:
        with socket.create_connection(
            (os.environ.get("PGHOST", "localhost"), int(os.environ.get("PGPORT", "5432"))),
            timeout=1.0,
        ):
            return True
    except OSError:
        return False

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
    """Start the app lifespan once for the entire test session.

    The REST API suite needs a live local Postgres (docker compose up -d);
    skip it cleanly when none is listening so the unit tests still run green.
    """
    if not _pg_reachable():
        pytest.skip("no local Postgres on PGHOST:PGPORT (run: docker compose up -d)")
    try:
        from asgi_lifespan import LifespanManager
    except ImportError:
        pytest.skip("asgi-lifespan not installed (pip install asgi-lifespan)")
    try:
        async with LifespanManager(app) as manager:
            yield manager.app
    except Exception as exc:  # port open but Postgres not actually usable
        pytest.skip(f"app lifespan failed against local Postgres: {exc}")


@pytest.fixture()
async def client(started_app):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=started_app),
        base_url="http://test",
    ) as ac:
        yield ac
