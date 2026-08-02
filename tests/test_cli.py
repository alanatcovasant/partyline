"""Pure unit tests for the partyline CLI querystring builders (no network, no pool)."""

from urllib.parse import parse_qs, urlsplit

import pytest

from covasant_partyline import cli


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
