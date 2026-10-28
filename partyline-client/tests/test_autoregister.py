import pytest

from covasant_partyline import autoregister as ar


@pytest.fixture
def posts(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(ar.cli, "_request", lambda method, url, path, body=None: sent.append((method, url, path, body)))
    monkeypatch.setattr(ar.cli, "_mint_token", lambda p: None)
    monkeypatch.setattr(ar.setup, "_load_user", lambda: {"owner": "alan"})
    monkeypatch.setattr(ar, "_git", lambda *a: str(tmp_path) if a[0] == "rev-parse" and "--show-toplevel" in a else "develop")
    monkeypatch.delenv("PARTYLINE_AGENT_ID", raising=False)
    monkeypatch.delenv("PARTYLINE_URL", raising=False)
    return sent


def test_registers_the_configured_agent_with_harness_and_workdir(monkeypatch, posts, tmp_path):
    monkeypatch.setattr(ar.cli, "_load_config", lambda: {"agentId": "alan_repo_1", "url": "http://localhost:8000"})
    assert ar.register_self("grok") == "alan_repo_1"
    (method, url, path, body), = posts
    assert (method, url, path) == ("POST", "http://localhost:8000", "/api/agents")
    assert body["agent_id"] == "alan_repo_1" and body["harness"] == "grok"
    assert body["owner"] == "alan" and body["repo"] == tmp_path.name and body["branch"] == "develop"
    assert body["workdirs"]


def test_does_nothing_without_a_configured_identity(monkeypatch, posts):
    monkeypatch.setattr(ar.cli, "_load_config", lambda: {})
    assert ar.register_self("agy") is None
    assert posts == []


def test_an_unreachable_line_does_not_stop_the_launch(monkeypatch, posts):
    monkeypatch.setattr(ar.cli, "_load_config", lambda: {"agentId": "alan_repo_1", "url": "http://localhost:8000"})

    def boom(*a, **k):
        raise SystemExit("Connection failed")

    monkeypatch.setattr(ar.cli, "_request", boom)
    assert ar.register_self("codex") is None
