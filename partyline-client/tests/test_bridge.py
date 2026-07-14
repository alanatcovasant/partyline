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

        async def fake_connect(tag=None):
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

        async def fake_connect(tag=None):
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

        async def fake_connect(tag=None):
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


def test_stale_token_forces_reconnect_before_use(monkeypatch):
    # The token-lifetime fix: an existing session older than _TOKEN_MAX_AGE_SECS must be
    # rebuilt (fresh token) BEFORE the call runs — never ride an expired token.
    monkeypatch.setattr(bridge, "_TOKEN_MAX_AGE_SECS", 0.0)  # everything counts as stale

    async def body():
        r = _remote()
        connects = {"n": 0}

        async def fake_connect(tag=None):
            connects["n"] += 1
            r._session = f"session-{connects['n']}"
            r._connected_at = time.monotonic()
            return r._session

        r._connect = fake_connect
        # Prime a live-but-old session, as if the bridge connected an hour ago.
        r._session = "stale-session"
        r._connected_at = time.monotonic() - 10_000

        seen = {}

        async def op(session):
            seen["session"] = session
            return "ok"

        assert await r.call(op, "stale") == "ok"
        assert connects["n"] == 1, "a stale session must trigger exactly one reconnect"
        assert seen["session"] == "session-1", "op must run on the freshly reconnected session"

    asyncio.run(body())


def test_fresh_token_reuses_session(monkeypatch):
    # Inverse guard: a recently-minted session must NOT reconnect (no needless churn).
    monkeypatch.setattr(bridge, "_TOKEN_MAX_AGE_SECS", 10_000.0)

    async def body():
        r = _remote()
        connects = {"n": 0}

        async def fake_connect(tag=None):
            connects["n"] += 1
            r._session = "should-not-reconnect"
            r._connected_at = time.monotonic()
            return r._session

        r._connect = fake_connect
        r._session = "live-session"
        r._connected_at = time.monotonic()  # just minted

        async def op(session):
            assert session == "live-session"
            return "ok"

        assert await r.call(op, "fresh") == "ok"
        assert connects["n"] == 0, "a fresh session must be reused, not reconnected"

    asyncio.run(body())


def test_token_stale_prefers_real_expiry_over_age():
    # When the SDK exposes a real expiry, staleness is a CHECK (not the age guess): a token
    # within the safety margin of expiry is stale even if just minted; one far from expiry
    # is fresh even if the age heuristic would call it old.
    import datetime as dt

    r = _remote()
    now = dt.datetime.now(dt.timezone.utc)

    # Far from expiry -> fresh, regardless of mint age.
    r._connected_at = time.monotonic() - 10_000  # age heuristic would say "stale"
    r._token_expiry = now + dt.timedelta(seconds=3600)
    assert r._token_stale() is False

    # Within the safety margin -> stale, even though just minted.
    r._connected_at = time.monotonic()
    r._token_expiry = now + dt.timedelta(seconds=30)  # < _TOKEN_EXPIRY_SAFETY_SECS
    assert r._token_stale() is True

    # Already expired -> stale.
    r._token_expiry = now - dt.timedelta(seconds=1)
    assert r._token_stale() is True


def test_bridge_info_identifies_the_running_build():
    info = bridge._bridge_info()
    assert info["component"] == "covasant-partyline MCP bridge"
    assert info["module_path"].replace("\\", "/").endswith("covasant_partyline/bridge.py")
    assert info["pid"] == __import__("os").getpid()
    assert "version" in info and "python" in info and "uptime_secs" in info

    # With a remote it also reports live connection + token state.
    r = _remote()
    import datetime as dt
    r._session = object()
    r._connected_at = time.monotonic()
    r._token_expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=900)
    info = bridge._bridge_info(r)
    assert info["connected"] is True
    assert info["mcp_url"] == "http://test/mcp"
    assert info["token_valid"] is True
    assert info["token_valid_for_secs"] > 0


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
