"""Unit tests for the SessionStart orientation hook (covasant_partyline.hook).

The hook must be fail-silent (never break a session) and must highlight
messages addressed to this repo's agents.
"""

import json
import sys

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
    addressed = [line for line in lines if "#8" in line]
    ambient = [line for line in lines if "#7" in line]
    assert addressed and "ADDRESSED" in addressed[0]
    assert ambient and "ADDRESSED" not in ambient[0]


def test_lists_active_agents(monkeypatch, capsys):
    out = _run_hook(monkeypatch, capsys, AGENTS, MESSAGES)
    assert "alan_partyline_1" in out and "alan_frontend_1" in out


def test_cline_hook_emits_context_modification_envelope(monkeypatch, capsys):
    """Cline reads {"cancel": false, "contextModification": "..."} on stdout, not plain markdown."""
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cline")
    out = _run_hook(monkeypatch, capsys, AGENTS, MESSAGES)
    parsed = json.loads(out)
    assert parsed["cancel"] is False
    assert "alan_partyline_1" in parsed["contextModification"]
    assert "ADDRESSED TO THIS REPO'S AGENTS" in parsed["contextModification"]


# --------------------------------------------------------------------------- #
# Roster rendering: quiet agents are shown WITH their age, never dropped. An agent you
# cannot see is indistinguishable from one that never existed, which is how a sender ends
# up treating silence as agreement.
# --------------------------------------------------------------------------- #


def test_quiet_agents_are_listed_separately_with_their_age():
    lines = hook.roster_lines([
        {"agent_id": "alan_aura_1", "repo": "aura", "status": "live", "reading": True},
        {"agent_id": "alan_aura_2", "repo": "aura_2", "status": "stale", "reading": False,
         "heartbeat_age_seconds": 68400},
    ])
    text = "\n".join(lines)
    assert "alan_aura_1 (aura)" in text
    assert "alan_aura_2" in text and "stale 19h" in text
    assert "messages to them wait" in text


def test_a_live_agent_that_is_not_reading_is_called_out():
    """Present is not listening: any command refreshes the heartbeat, only acks refresh reads."""
    lines = hook.roster_lines([
        {"agent_id": "alan_aura_3", "repo": "aura", "status": "live", "reading": False,
         "heartbeat_age_seconds": 12},
    ])
    assert "not reading" in "\n".join(lines)


def test_quiet_roster_is_capped_so_it_cannot_crowd_out_traffic():
    cold = [
        {"agent_id": f"old_{i}", "repo": "r", "status": "stale", "reading": False,
         "heartbeat_age_seconds": 900000}
        for i in range(hook.MAX_COLD_SHOWN + 4)
    ]
    text = "\n".join(hook.roster_lines(cold))
    assert "+4 more" in text
    assert text.count("old_") == hook.MAX_COLD_SHOWN


def test_roster_of_an_empty_line_renders_nothing():
    assert hook.roster_lines([]) == []


def test_roster_tolerates_rows_without_presence_fields():
    """An older server predates these fields; the hook must not blow up on its response."""
    lines = hook.roster_lines([{"agent_id": "legacy_1", "repo": "aura"}])
    assert "legacy_1 (aura)" in "\n".join(lines)


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


# --------------------------------------------------------------------------- #
# Identity resolution (#1545): this hook resolved agent_id/url/profile from the
# environment only — the same regression that silenced the unread peek. Here it is
# worse than silence: with agent_id="" the self-claim filter at territory_warning
# can never match, so the hook warns you off your own territory.
# --------------------------------------------------------------------------- #


def _fake_endpoints(monkeypatch, agents=(), messages=(), claims=()):
    """Route the three GETs the hook issues, recording the base URLs it asked for."""
    asked = []

    def fake_get(url, token):
        asked.append(url)
        if "/api/agents" in url:
            return list(agents)
        if "/api/claims" in url:
            return list(claims)
        return list(messages)

    monkeypatch.setattr(hook, "_get", fake_get)
    return asked


def test_own_claim_is_not_flagged_as_someone_elses_territory(config_clone, monkeypatch, capsys):
    from covasant_partyline.targets import canonicalize

    clone = config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")
    mine = canonicalize(str(clone))
    _fake_endpoints(monkeypatch, claims=[{"resource_path": mine, "agent_id": "alan_aura_0"}])
    monkeypatch.setattr(sys, "argv", ["hook"])

    hook.main()

    assert "another agent's territory" not in capsys.readouterr().out


def test_another_agents_claim_is_still_flagged(config_clone, monkeypatch, capsys):
    """The warning must keep firing for real collisions — this is not a mute switch."""
    from covasant_partyline.targets import canonicalize

    clone = config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")
    mine = canonicalize(str(clone))
    _fake_endpoints(monkeypatch, claims=[{"resource_path": mine, "agent_id": "someone_else"}])
    monkeypatch.setattr(sys, "argv", ["hook"])

    hook.main()

    out = capsys.readouterr().out
    assert "another agent's territory" in out and "someone_else" in out


def test_resolves_url_from_config(config_clone, monkeypatch, capsys):
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")
    asked = _fake_endpoints(monkeypatch, agents=list(AGENTS))
    monkeypatch.setattr(sys, "argv", ["hook"])

    hook.main()

    assert asked and all(u.startswith("https://app.example/api/") for u in asked)


def test_resolves_profile_from_config_not_a_hardcoded_default(config_clone, monkeypatch, capsys):
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0",
                  "databricksProfile": "from-config"})
    used = {}
    monkeypatch.setattr(hook, "_token", lambda profile: used.setdefault("profile", profile) and "tok")
    _fake_endpoints(monkeypatch, agents=list(AGENTS))
    monkeypatch.setattr(sys, "argv", ["hook"])

    hook.main()

    assert used["profile"] == "from-config"


def test_local_app_runs_without_a_token(config_clone, monkeypatch, capsys):
    config_clone({"url": "http://localhost:8000", "agentId": "alan_aura_0"})
    asked = _fake_endpoints(monkeypatch, agents=list(AGENTS))
    monkeypatch.setattr(sys, "argv", ["hook"])

    hook.main()

    assert asked  # a local app needs no Databricks token; requiring one silenced the hook
    assert "alan_partyline_1" in capsys.readouterr().out


def test_flag_still_wins_over_config(config_clone, monkeypatch, capsys):
    config_clone({"url": "https://app.example", "agentId": "alan_aura_0"})
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")
    asked = _fake_endpoints(monkeypatch, agents=list(AGENTS))
    monkeypatch.setattr(sys, "argv", ["hook", "--url", "https://flag.example"])

    hook.main()

    assert all(u.startswith("https://flag.example/") for u in asked)
