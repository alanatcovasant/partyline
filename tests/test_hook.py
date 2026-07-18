"""Unit tests for the SessionStart orientation hook (covasant_partyline.hook).

The hook must be fail-silent (never break a session) and must highlight
messages addressed to this repo's agents.
"""

import sys

import pytest

from covasant_partyline import hook


AGENTS = [
    {"agent_id": "alan_partyline_1", "repo": "covasant-partyline"},
    {"agent_id": "alan_frontend_1", "repo": "frontend"},
]
MESSAGES = [
    {
        "id": 7, "agent_id": "alan_frontend_1", "message_type": "status",
        "recipient": None, "mentions": [], "payload": "ambient chatter",
        "created_at": "2099-01-01T00:00:00Z",
    },
    {
        "id": 8, "agent_id": "alan_frontend_1", "message_type": "question",
        "recipient": "alan_partyline_1", "mentions": ["alan_partyline_1"],
        "payload": "addressed to this repo", "created_at": "2099-01-01T00:01:00Z",
    },
]


def _run_hook(monkeypatch, capsys, agents, messages, argv_extra=()):
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")
    monkeypatch.setattr(
        hook, "_get",
        lambda url, token: agents if "/agents" in url else messages,
    )
    monkeypatch.setattr(
        sys, "argv",
        ["hook", "--url", "https://x", "--owner", "alan", "--repo", "partyline",
         *argv_extra],
    )
    hook.main()
    return capsys.readouterr().out


def test_highlights_addressed_messages(monkeypatch, capsys):
    out = _run_hook(monkeypatch, capsys, AGENTS, MESSAGES)
    assert "ADDRESSED TO THIS REPO'S AGENTS" in out
    lines = out.splitlines()
    addressed = [l for l in lines if "#8" in l]
    ambient = [l for l in lines if "#7" in l]
    assert addressed and "ADDRESSED" in addressed[0]
    assert ambient and "ADDRESSED" not in ambient[0]


def test_lists_active_agents(monkeypatch, capsys):
    out = _run_hook(monkeypatch, capsys, AGENTS, MESSAGES)
    assert "alan_partyline_1" in out and "alan_frontend_1" in out


def test_silent_when_line_is_quiet(monkeypatch, capsys):
    out = _run_hook(monkeypatch, capsys, [], [])
    assert out == ""


def test_silent_when_token_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(hook, "_token", lambda profile: None)
    monkeypatch.setattr(sys, "argv", ["hook", "--url", "https://x"])
    hook.main()  # must not raise
    assert capsys.readouterr().out == ""


def test_silent_on_api_failure(monkeypatch, capsys):
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")

    def boom(url, token):
        raise OSError("network down")

    monkeypatch.setattr(hook, "_get", boom)
    monkeypatch.setattr(sys, "argv", ["hook", "--url", "https://x"])
    hook.main()  # fail-silent: a broken hook must never block a session
    assert capsys.readouterr().out == ""


def test_old_messages_filtered_by_window(monkeypatch, capsys):
    stale = [dict(MESSAGES[0], created_at="2001-01-01T00:00:00Z")]
    out = _run_hook(monkeypatch, capsys, AGENTS, stale)
    assert "#7" not in out  # outside the 24h window
