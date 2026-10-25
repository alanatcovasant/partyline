"""Regression tests for long-lived SSE token refresh.

Root cause of the ~1h connection drops: clients captured the Databricks OAuth
token once at connect and reused it on every reconnect. Databricks U2M tokens
expire in ~1h (the CLI hands out a *cached* token whose remaining life may be
far less), so once it expired the auth proxy 401'd every reconnect forever.

The fix is a re-minting token provider used on each (re)connect. These lock in
its contract: it re-mints from the profile (fresh every call), falls back to a
static token only when minting is unavailable, and stays tokenless for localhost.
"""

from covasant_partyline import channel, cli


def test_resolve_stream_token_remints_fresh_each_call(monkeypatch):
    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    seq = iter(["t1", "t2", "t3"])
    monkeypatch.setattr(cli, "_mint_token", lambda profile: next(seq))
    # A long-lived stream calls this once per (re)connect; each must be fresh.
    got = [cli.resolve_stream_token("https://app.example", "dev-eastus2", "stale") for _ in range(3)]
    assert got == ["t1", "t2", "t3"]


def test_resolve_stream_token_prefers_mint_over_static(monkeypatch):
    monkeypatch.setenv("PARTYLINE_TOKEN", "stale-env")
    monkeypatch.setattr(cli, "_mint_token", lambda profile: "fresh")
    assert cli.resolve_stream_token("https://app.example", "dev-eastus2", "stale-init") == "fresh"


def test_resolve_stream_token_falls_back_when_mint_fails(monkeypatch):
    monkeypatch.setenv("PARTYLINE_TOKEN", "envtok")
    monkeypatch.setattr(cli, "_mint_token", lambda profile: None)
    assert cli.resolve_stream_token("https://app.example", "dev-eastus2", None) == "envtok"


def test_resolve_stream_token_localhost_is_tokenless(monkeypatch):
    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_mint_token", lambda profile: "should-not-be-used")
    assert cli.resolve_stream_token("http://localhost:8000", "dev-eastus2", None) is None


def test_channel_current_token_delegates_and_remints(monkeypatch):
    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    seq = iter(["c1", "c2"])
    monkeypatch.setattr(cli, "_mint_token", lambda profile: next(seq))
    s = channel.Settings(agent_id="me", url="https://app.example", token="stale", profile="dev-eastus2")
    assert channel.current_token(s) == "c1"
    assert channel.current_token(s) == "c2"  # fresh on the next reconnect


# --- channel token cache: reuse a JWT until shortly before `exp` (mints are slow under load) ---


def _jwt(exp: float) -> str:
    import base64
    import json as _json

    def b64(d):
        return base64.urlsafe_b64encode(_json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': exp})}.sig"


def _settings():
    return channel.Settings(agent_id="me", url="https://app.example", token=None, profile="dev-eastus2")


def test_jwt_expiry_reads_exp_and_rejects_garbage():
    assert channel._jwt_expiry(_jwt(1234567890)) == 1234567890.0
    for bad in ("c1", "a.b.c", "", "a.e30.c"):  # not a JWT / bad base64 / no exp claim
        assert channel._jwt_expiry(bad) is None


def test_channel_reuses_a_jwt_until_near_expiry(monkeypatch):
    import time

    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    mints = []
    good = _jwt(time.time() + 3600)
    monkeypatch.setattr(cli, "_mint_token", lambda profile: mints.append(1) or good)
    s = _settings()
    assert channel.current_token(s) == good
    assert channel.current_token(s) == good
    assert len(mints) == 1  # second call served from cache — no subprocess


def test_channel_remints_a_jwt_inside_the_refresh_margin(monkeypatch):
    import time

    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    soon, later = _jwt(time.time() + 60), _jwt(time.time() + 3600)
    seq = iter([soon, later])
    monkeypatch.setattr(cli, "_mint_token", lambda profile: next(seq))
    s = _settings()
    assert channel.current_token(s) == soon  # expires inside the 300s margin...
    assert channel.current_token(s) == later  # ...so the next call re-mints


def test_channel_unparseable_token_is_never_cached(monkeypatch):
    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    seq = iter(["opaque1", "opaque2"])
    monkeypatch.setattr(cli, "_mint_token", lambda profile: next(seq))
    s = _settings()
    assert [channel.current_token(s), channel.current_token(s)] == ["opaque1", "opaque2"]


def test_stream_loop_backs_off_exponentially_and_closes_failed_responses(monkeypatch):
    import io
    import urllib.error

    import anyio

    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_mint_token", lambda profile: None)  # mint keeps failing
    closed, delays = [], []

    def boom(req, timeout=None):
        err = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        err.close = lambda: closed.append(1)
        raise err

    class Stop(Exception):
        pass

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) == 5:
            raise Stop

    monkeypatch.setattr(channel.urllib.request, "urlopen", boom)
    monkeypatch.setattr(channel.anyio, "sleep", fake_sleep)
    s = _settings()
    s._cached = ("stale", 9e12)  # a cached token the server rejected must be dropped

    try:
        anyio.run(channel._subscribe_and_push, s, None)
    except (Stop, BaseExceptionGroup):
        pass
    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]  # not a fixed 2s spin
    assert len(closed) == 5  # every failed response released, none left in CLOSE_WAIT
    assert s._cached is None
