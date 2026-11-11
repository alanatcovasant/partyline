"""`omni-pl` command construction, fork selection, and mail injection."""

import pytest

from covasant_partyline import omni_runner as omni


def which_ok(name: str) -> str | None:
    return {
        "omnigent": "/bin/omnigent",
        "claude-pl": "/bin/claude-pl",
        "codex-pl": "/bin/codex-pl",
        "grok-pl": "/bin/grok-pl",
    }.get(name)


def test_fresh_claude_points_omnigent_at_claude_pl():
    plan = omni.prepare(["claude"], which_ok)
    assert plan.command == ("/bin/omnigent", "claude")
    assert dict(plan.env_updates)["OMNIGENT_CLAUDE_PATH"] == "/bin/claude-pl"
    assert plan.watch is False
    assert plan.hooks is None


def test_fork_uses_native_harness_id_and_passes_extra_args():
    plan = omni.prepare(["claude", "--fork", "conv_abc", "--", "--permission-mode", "bypass"], which_ok)
    assert plan.command == (
        "/bin/omnigent",
        "run",
        "--fork",
        "conv_abc",
        "--harness",
        "claude-native",
        "--permission-mode",
        "bypass",
    )
    assert plan.fork_id == "conv_abc"


def test_resume_stays_on_the_shortcut():
    plan = omni.prepare(["codex", "--resume", "conv_9"], which_ok)
    assert plan.command == ("/bin/omnigent", "codex", "--resume", "conv_9")
    assert dict(plan.env_updates)["OMNIGENT_CODEX_PATH"] == "/bin/codex-pl"


def test_grok_uses_acp_and_does_not_replace_the_binary():
    plan = omni.prepare(["grok", "--always-approve"], which_ok)
    assert plan.command == ("/bin/omnigent", "run", "--harness", "grok", "--always-approve")
    assert "OMNIGENT_GROK_PATH" not in dict(plan.env_updates)
    assert plan.watch is True
    assert plan.hooks == "grok"


def test_missing_pl_binary_warns_and_still_launches():
    plan = omni.prepare(["claude"], lambda name: "/bin/omnigent" if name == "omnigent" else None)
    assert plan.env_updates == ()
    assert plan.command[0] == "/bin/omnigent"
    assert any("claude-pl" in warning for warning in plan.warnings)


def test_fork_and_resume_together_are_rejected():
    with pytest.raises(SystemExit, match="only one"):
        omni.prepare(["claude", "--fork", "a", "--resume", "b"], which_ok)


def test_unknown_harness_is_rejected():
    with pytest.raises(SystemExit, match="unknown harness"):
        omni.prepare(["nope"], which_ok)


def test_missing_omnigent_is_rejected():
    with pytest.raises(SystemExit, match="omnigent"):
        omni.prepare(["grok"], lambda name: None)


def test_choose_session_skips_the_fork_source_and_old_sessions():
    sessions = [
        {"id": "conv_old", "created_at": 10},
        {"id": "conv_source", "created_at": 100},
        {"id": "conv_new", "created_at": 200},
    ]
    assert omni.choose_session(sessions, started=150, resume_id=None, fork_id="conv_source") == "conv_new"
    assert omni.choose_session(sessions, started=150, resume_id="conv_source", fork_id=None) == "conv_source"
    assert omni.choose_session(sessions, started=500, resume_id=None, fork_id=None) is None


def test_old_unread_is_not_new_mail():
    assert omni.message_is_new({"event_time": "2020-01-01T00:00:00Z"}, started=1_700_000_000) is False
    assert omni.message_is_new({"event_time": "not-a-date"}, started=0) is False
    fresh = "2026-11-11T21:00:02+00:00"
    started = omni.datetime.fromisoformat(fresh).timestamp()
    assert omni.message_is_new({"event_time": fresh}, started=started) is True


def test_mail_becomes_an_omnigent_user_message():
    text = omni.mail_text(
        {"id": 7, "message_type": "status", "agent_id": "alan_build_1", "summary": "deploy done", "payload": None}
    )
    event = omni.omnigent_event(text)
    assert event["type"] == "message"
    assert event["data"]["content"][0]["text"].startswith("Partyline #7")
    assert "deploy done" in event["data"]["content"][0]["text"]


def test_launch_posts_the_fork_and_runs_omnigent(monkeypatch):
    posted = {}
    monkeypatch.setattr(omni, "register_self", lambda harness: "alan_partyline_1")
    monkeypatch.setitem(omni.HARNESSES, "agy", (("agy",), lambda: posted.setdefault("hooks", True)))

    def fake_post(harness, conv_id):
        posted["notice"] = (harness, conv_id)

    monkeypatch.setattr(omni, "post_fork_notice", fake_post)

    class Result:
        returncode = 0

    ran = {}

    def fake_run(cmd, env):
        ran["cmd"] = cmd
        ran["env"] = env
        return Result()

    monkeypatch.setattr(omni.subprocess, "run", fake_run)
    monkeypatch.setattr(
        omni.shutil,
        "which",
        lambda name: {"omnigent": "/bin/omnigent", "agy-pl": "/bin/agy-pl"}.get(name),
    )
    assert omni.launch(["agy", "--fork", "conv_z"]) == 0
    assert posted["notice"] == ("agy", "conv_z")
    assert ran["cmd"][:6] == ("/bin/omnigent", "run", "--fork", "conv_z", "--harness", "antigravity-native")
    assert ran["env"]["OMNIGENT_AGY_PATH"] == "/bin/agy-pl"
