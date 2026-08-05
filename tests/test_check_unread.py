"""Unit tests for the unread-peek hook (covasant_partyline.check_unread).

The regression these lock (#1545): the peek resolved agent_id/url/profile from the
environment *only*, so on any clone that carries its identity in .partyline/config.json
— the post-#1545 supported shape — it resolved agent_id="" , hit its own
`if not url or not agent_id: return` guard, and emitted nothing. Output byte-identical
to a genuinely empty inbox, which is why three registered, heartbeating agents sat deaf
on queued mail with no error anywhere.

Fail-silent is still the contract: every failure path must emit nothing and not raise.
"""

import json
import urllib.request

import pytest

from covasant_partyline import check_unread


UNREAD = [
    {"id": 41, "agent_id": "sender", "message_type": "question",
     "summary": "can you take the migration?", "recipient": None, "mentions": ["alan_aura_0"]},
]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fake_http(monkeypatch):
    """Capture the request the peek issues; return whatever payload the test sets."""
    seen = {"requests": [], "payload": UNREAD}

    def fake_urlopen(req, timeout=None):
        seen["requests"].append(req)
        if isinstance(seen["payload"], Exception):
            raise seen["payload"]
        return _FakeResponse(seen["payload"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_resolves_identity_from_config_when_env_is_empty(config_clone, fake_http, monkeypatch, capsys):
    """The #1545 fix: identity in config.json alone must produce a peek."""
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0",
                  "databricksProfile": "dev-eastus2"})
    monkeypatch.setattr(check_unread, "_token", lambda profile: "tok")

    check_unread.main()

    assert len(fake_http["requests"]) == 1
    assert "/api/agents/alan_aura_0/unread" in fake_http["requests"][0].full_url
    assert fake_http["requests"][0].full_url.startswith("https://app.example/")
    out = capsys.readouterr().out
    assert "alan_aura_0" in out
    assert "can you take the migration?" in out


def test_env_agent_id_still_shadows_config(config_clone, fake_http, monkeypatch, capsys):
    """Precedence is deliberate (#1545): config is the per-clone default, env the override."""
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "alan_override_9")
    monkeypatch.setattr(check_unread, "_token", lambda profile: "tok")

    check_unread.main()

    assert "/api/agents/alan_override_9/unread" in fake_http["requests"][0].full_url


def test_honors_partyline_profile_for_token_acquisition(config_clone, fake_http, monkeypatch):
    """PARTYLINE_PROFILE is the hooks' documented knob — it must not be dropped."""
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0",
                  "databricksProfile": "from-config"})
    monkeypatch.setenv("PARTYLINE_PROFILE", "from-env")
    used = {}
    monkeypatch.setattr(check_unread, "_token", lambda profile: used.setdefault("profile", profile) and "tok")

    check_unread.main()

    assert used["profile"] == "from-env"


def test_profile_comes_from_config_when_env_unset(config_clone, fake_http, monkeypatch):
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0",
                  "databricksProfile": "from-config"})
    used = {}
    monkeypatch.setattr(check_unread, "_token", lambda profile: used.setdefault("profile", profile) and "tok")

    check_unread.main()

    assert used["profile"] == "from-config"


def test_local_app_peeks_without_a_token(config_clone, fake_http, capsys):
    """A local app needs no Databricks token; requiring one would silence the peek."""
    config_clone({"url": "http://localhost:8000", "agentId": "alan_aura_0"})

    check_unread.main()

    assert len(fake_http["requests"]) == 1
    assert fake_http["requests"][0].get_header("Authorization") is None
    assert "alan_aura_0" in capsys.readouterr().out


def test_config_url_trailing_slash_does_not_double(config_clone, fake_http, monkeypatch):
    config_clone({"url": "https://app.example/", "agentId": "alan_aura_0"})
    monkeypatch.setattr(check_unread, "_token", lambda profile: "tok")

    check_unread.main()

    assert "//api/agents" not in fake_http["requests"][0].full_url


def test_silent_when_identity_is_nowhere(config_clone, fake_http, capsys):
    config_clone({"url": "https://app.example"})  # url but no agentId

    check_unread.main()

    assert fake_http["requests"] == []
    assert capsys.readouterr().out == ""


def test_silent_when_remote_token_unavailable(config_clone, fake_http, monkeypatch, capsys):
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(check_unread, "_token", lambda profile: None)

    check_unread.main()

    assert fake_http["requests"] == []
    assert capsys.readouterr().out == ""


def test_silent_on_network_error(config_clone, fake_http, monkeypatch, capsys):
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(check_unread, "_token", lambda profile: "tok")
    fake_http["payload"] = OSError("network down")

    check_unread.main()  # fail-silent: a broken hook must never block a turn

    assert capsys.readouterr().out == ""


def test_silent_when_inbox_is_empty(config_clone, fake_http, monkeypatch, capsys):
    """The honest empty case must stay indistinguishable in output but reachable in code."""
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(check_unread, "_token", lambda profile: "tok")
    fake_http["payload"] = []

    check_unread.main()

    assert len(fake_http["requests"]) == 1  # it did ask — that's the difference that matters
    assert capsys.readouterr().out == ""
