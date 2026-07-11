"""Unit tests for the MCP bridge's hard-bounded call path (no live network).

These lock in the reliability fix: a wedged remote must surface as a *fast* error the
MCP client can reconnect from, never a silent hang up to the client's ~1800s idle
timeout. Tests drive asyncio.run() from sync functions so they don't depend on any
pytest-asyncio configuration.
"""
import asyncio
import time

import pytest

from covasant_partyline import bridge


def _remote():
    # cfg/url are only touched by _connect(), which every test stubs out.
    return bridge._Remote(None, "http://test/mcp")


def test_call_success():
    async def body():
        r = _remote()
        sentinel = object()

        async def fake_connect():
            r._session = sentinel
            return sentinel

        r._connect = fake_connect

        async def op(session):
            assert session is sentinel
            return "ok"

        assert await r.call(op, "success") == "ok"

    asyncio.run(body())


def test_reconnects_once_on_error():
    async def body():
        r = _remote()
        connects = {"n": 0}

        async def fake_connect():
            connects["n"] += 1
            r._session = f"session-{connects['n']}"
            return r._session

        r._connect = fake_connect
        calls = {"n": 0}

        async def op(session):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("stale session (server restarted)")
            return "ok-after-reconnect"

        assert await r.call(op, "retry") == "ok-after-reconnect"
        assert connects["n"] == 2, "expected initial connect + exactly one reconnect"
        assert calls["n"] == 2

    asyncio.run(body())


def test_hard_deadline_on_wedged_op_fails_fast_and_drops_session(monkeypatch):
    # The core regression: a call that never returns must be abandoned at the deadline,
    # not hang until the client's idle timeout.
    monkeypatch.setattr(bridge, "_CALL_TIMEOUT_SECS", 0.2)

    async def body():
        r = _remote()
        sentinel = object()

        async def fake_connect():
            r._session = sentinel
            return sentinel

        r._connect = fake_connect

        async def wedged(session):
            await asyncio.sleep(100)  # never completes within the deadline

        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await r.call(wedged, "wedged")
        elapsed = time.monotonic() - t0
        assert elapsed < 5, f"call should fail fast at the deadline, took {elapsed:.1f}s"
        assert r._session is None, "session must be dropped so the next call reconnects"

    asyncio.run(body())


def test_bounded_lock_fails_fast_when_prior_call_wedged(monkeypatch):
    # If a prior call is wedged holding the lock, a new call must fail fast (RuntimeError)
    # instead of queueing behind it forever.
    monkeypatch.setattr(bridge, "_LOCK_ACQUIRE_TIMEOUT_SECS", 0.2)

    async def body():
        r = _remote()
        await r._lock.acquire()  # simulate an in-flight call still holding the lock

        async def op(session):
            return "should-not-run"

        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="busy"):
            await r.call(op, "queued")
        elapsed = time.monotonic() - t0
        assert elapsed < 5, f"queued call should fail fast, took {elapsed:.1f}s"

    asyncio.run(body())
