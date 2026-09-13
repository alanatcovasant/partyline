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
