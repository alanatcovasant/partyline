"""Pure unit tests for cli.py: the querystring builders and the shared identity
resolvers (no network, no pool, no filesystem — `cfg` is injected)."""

from urllib.parse import parse_qs, urlsplit

import pytest

from covasant_partyline import cli

# `clean_env` (drops the PARTYLINE_*/DATABRICKS_* env the resolvers consult) lives in
# conftest.py — the peek-hook tests need the same isolation.


def _capture(monkeypatch):
    calls = {}

    def fake_request(method, base, path, body=None):
        calls["method"], calls["path"] = method, path
        return {"ok": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_print", lambda obj: None)
    return calls


def test_search_builds_querystring_dropping_none(monkeypatch):
    calls = _capture(monkeypatch)
    args = cli.build_parser().parse_args(
        ["search", "--issue", "123", "--repo", "web", "--limit", "10"]
    )
    args.url = "http://localhost:8000"
    args.func(args)
    assert calls["method"] == "GET"
    parts = urlsplit(calls["path"])
    assert parts.path == "/api/messages/query"
    qs = parse_qs(parts.query)
    assert qs["issue_ref"] == ["123"]
    assert qs["repo"] == ["web"]
    assert qs["limit"] == ["10"]
    assert "agent_id" not in qs  # unset flags are dropped, not sent blank


def test_search_maps_text_flag_to_q(monkeypatch):
    calls = _capture(monkeypatch)
    args = cli.build_parser().parse_args(["search", "-q", "pool fix"])
    args.url = "http://localhost:8000"
    args.func(args)
    qs = parse_qs(urlsplit(calls["path"]).query)
    assert qs["q"] == ["pool fix"]


def test_conversation_builds_path(monkeypatch):
    calls = _capture(monkeypatch)
    args = cli.build_parser().parse_args(["conversation", "abc-123", "--limit", "50"])
    args.url = "http://localhost:8000"
    args.func(args)
    parts = urlsplit(calls["path"])
    assert parts.path == "/api/conversations/abc-123"
    assert parse_qs(parts.query)["limit"] == ["50"]


def test_claim_issue_posts_work_item_and_paths(monkeypatch, tmp_path):
    bodies = {}

    def fake_request(method, base, path, body=None):
        bodies["path"], bodies["body"] = path, body
        return {"acquired": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_print", lambda obj: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()

    args = cli.build_parser().parse_args(
        ["claim-issue", "alan_aura_1", "aura", "issue:covasant/aura#482", "--path", "pkg"]
    )
    args.url = "http://localhost:8000"
    args.func(args)
    assert bodies["path"] == "/api/claims/work-item"
    assert bodies["body"]["work_item"] == "issue:covasant/aura#482"
    assert bodies["body"]["paths"][0].endswith("/pkg")
    assert "\\" not in bodies["body"]["paths"][0]


def test_register_sends_normalized_workdirs(monkeypatch, tmp_path):
    bodies = {}

    def fake_request(method, base, path, body=None):
        bodies["body"] = body
        return {"agent_id": "x"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli, "_print", lambda obj: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "domain").mkdir()

    args = cli.build_parser().parse_args(
        ["register", "alan_aura_1", "--owner", "alan", "--repo", "aura", "--workdir", "domain"]
    )
    args.url = "http://localhost:8000"
    args.func(args)
    assert bodies["body"]["workdirs"][0].endswith("/domain")


# --------------------------------------------------------------------------- #
# Shared identity resolvers — the single home every consumer delegates to
# (cli.main, channel.resolve_settings, outbox.resolve_agent_id, check_unread, hook).
# --------------------------------------------------------------------------- #


def test_resolve_agent_id_prefers_flag(clean_env, monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "from_env")
    assert cli.resolve_agent_id("from_flag", cfg={"agentId": "from_cfg"}) == "from_flag"


def test_resolve_agent_id_env_beats_config(clean_env, monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "from_env")
    assert cli.resolve_agent_id(cfg={"agentId": "from_cfg"}) == "from_env"


def test_resolve_agent_id_falls_back_to_config(clean_env):
    """The #1545 case: identity lives only in .partyline/config.json."""
    assert cli.resolve_agent_id(cfg={"agentId": "alan_aura_0"}) == "alan_aura_0"


def test_resolve_agent_id_accepts_snake_case_config_key(clean_env):
    assert cli.resolve_agent_id(cfg={"agent_id": "alan_aura_0"}) == "alan_aura_0"


def test_resolve_agent_id_camel_case_wins_over_snake(clean_env):
    assert cli.resolve_agent_id(cfg={"agentId": "camel", "agent_id": "snake"}) == "camel"


def test_resolve_agent_id_none_when_nothing_set(clean_env):
    assert cli.resolve_agent_id(cfg={}) is None


def test_resolve_url_prefers_flag(clean_env, monkeypatch):
    monkeypatch.setenv("PARTYLINE_URL", "http://env")
    assert cli.resolve_url("http://flag", cfg={"url": "http://cfg"}) == "http://flag"


def test_resolve_url_env_beats_config(clean_env, monkeypatch):
    monkeypatch.setenv("PARTYLINE_URL", "http://env")
    assert cli.resolve_url(cfg={"url": "http://cfg"}) == "http://env"


def test_resolve_url_falls_back_to_config(clean_env):
    assert cli.resolve_url(cfg={"url": "https://app.example"}) == "https://app.example"


def test_resolve_url_defaults_to_localhost(clean_env):
    assert cli.resolve_url(cfg={}) == "http://localhost:8000"


def test_resolve_url_strips_trailing_slash(clean_env):
    """Callers concatenate `url + path`; a config slash must not yield '//api/...'."""
    assert cli.resolve_url(cfg={"url": "https://app.example/"}) == "https://app.example"


def test_resolve_profile_is_none_for_localhost(clean_env):
    assert cli.resolve_profile(url="http://localhost:8000", cfg={"databricksProfile": "p"}) is None


def test_resolve_profile_is_none_for_loopback_ip(clean_env):
    """127.0.0.1 is as local as localhost — cli.main and channel disagreed about this."""
    assert cli.resolve_profile(url="http://127.0.0.1:8000", cfg={"databricksProfile": "p"}) is None


def test_resolve_profile_prefers_flag(clean_env, monkeypatch):
    monkeypatch.setenv("PARTYLINE_PROFILE", "from_env")
    got = cli.resolve_profile("from_flag", url="https://app.example", cfg={"databricksProfile": "c"})
    assert got == "from_flag"


def test_resolve_profile_partyline_env_beats_config(clean_env, monkeypatch):
    """PARTYLINE_PROFILE is partyline-specific intent (the hooks' documented knob),
    so it ranks with the flag — above config, unlike ambient DATABRICKS_CONFIG_PROFILE."""
    monkeypatch.setenv("PARTYLINE_PROFILE", "from_env")
    assert cli.resolve_profile(url="https://app.example", cfg={"databricksProfile": "c"}) == "from_env"


def test_resolve_profile_config_beats_ambient_databricks_env(clean_env, monkeypatch):
    """The ambient profile often targets another workspace and would 401."""
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "other-workspace")
    assert cli.resolve_profile(url="https://app.example", cfg={"databricksProfile": "mine"}) == "mine"


def test_resolve_profile_accepts_bare_profile_config_key(clean_env):
    assert cli.resolve_profile(url="https://app.example", cfg={"profile": "mine"}) == "mine"


def test_resolve_profile_falls_back_to_ambient_then_default(clean_env, monkeypatch):
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient")
    assert cli.resolve_profile(url="https://app.example", cfg={}) == "ambient"
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE")
    assert cli.resolve_profile(url="https://app.example", cfg={}) == "dev-eastus2"
