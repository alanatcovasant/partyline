import io
import json

from covasant_partyline import check_unread, harness_runner as hr


def test_grok_hook_is_a_stop_gate_in_grok_stop_format():
    gate = hr.grok_hooks()["hooks"]["Stop"][0]["hooks"][0]
    assert gate["command"].endswith("-m covasant_partyline.check_unread")
    assert gate["env"] == {"PARTYLINE_HOOK_FORMAT": "grok_stop"}


def test_install_is_idempotent(tmp_path):
    path = tmp_path / "hooks" / "partyline.json"
    assert hr.install_grok_hooks(path) is True
    assert hr.install_grok_hooks(path) is False
    assert json.loads(path.read_text())["hooks"]["Stop"]


def test_agy_install_merges_and_keeps_the_users_other_hooks(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"mine": {"Stop": [{"type": "command", "command": "x"}]}}))
    hr.install_agy_hooks(path)
    data = json.loads(path.read_text())
    assert data["mine"] == {"Stop": [{"type": "command", "command": "x"}]}
    assert data["partyline"]["PreInvocation"][0]["command"].endswith("-m covasant_partyline.agy_hook")


def test_agy_install_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("not json")
    hr.install_agy_hooks(path)
    assert "partyline" in json.loads(path.read_text())


def test_launch_installs_then_runs_the_cli_with_args_untouched(monkeypatch):
    calls = []
    monkeypatch.setattr(hr.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setitem(hr.HARNESSES, "grok", ("grok", lambda: calls.append("install")))
    monkeypatch.setattr(hr, "register_self", lambda h: calls.append(f"register:{h}"))

    class R:
        returncode = 7

    monkeypatch.setattr(hr.subprocess, "run", lambda cmd: calls.append(cmd) or R())
    try:
        hr.launch("grok", ["--always-approve"])
    except SystemExit as e:
        assert e.code == 7
    assert calls == ["install", "register:grok", ["/bin/grok", "--always-approve"]]


def test_launch_exits_when_the_cli_is_missing(monkeypatch):
    monkeypatch.setattr(hr.shutil, "which", lambda n: None)
    try:
        hr.launch("agy", [])
    except SystemExit as e:
        assert "not found" in str(e.code)


def _run_peek(monkeypatch, tmp_path, payload, msgs):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "grok_stop")
    monkeypatch.setattr(check_unread, "SEEN_DIR", tmp_path)
    monkeypatch.setattr(check_unread.cli, "_load_config", lambda: {})
    monkeypatch.setattr(check_unread.cli, "resolve_url", lambda *a, **k: "http://localhost:8000")
    monkeypatch.setattr(check_unread.cli, "resolve_agent_id", lambda *a, **k: "me_1")
    monkeypatch.setattr(check_unread.cli, "is_local_url", lambda u: True)
    monkeypatch.setattr(check_unread, "_token", lambda p: None)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(check_unread.urllib.request, "urlopen", lambda *a, **k: Resp(json.dumps(msgs).encode()))
    monkeypatch.setattr(check_unread.sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(check_unread.sys, "stdout", out)
    check_unread.main()
    return out.getvalue()


MAIL = [{"id": 5, "agent_id": "bob", "message_type": "question", "summary": "ready?"}]


def test_stop_blocks_once_with_the_mail_as_the_reason(monkeypatch, tmp_path):
    first = json.loads(_run_peek(monkeypatch, tmp_path, {"reason": "end_turn"}, MAIL))
    assert first["decision"] == "block" and "#5" in first["reason"]
    # the peek never acks, so the same unread mail must not block the next turn end too
    assert _run_peek(monkeypatch, tmp_path, {"reason": "end_turn"}, MAIL) == ""


def test_stop_is_silent_for_session_end_and_for_continuation_rounds(monkeypatch, tmp_path):
    assert _run_peek(monkeypatch, tmp_path, {"reason": "shutdown"}, MAIL) == ""
    assert _run_peek(monkeypatch, tmp_path, {"reason": "end_turn", "stopHookActive": True}, MAIL) == ""


def test_stop_is_silent_on_a_quiet_line(monkeypatch, tmp_path):
    assert _run_peek(monkeypatch, tmp_path, {"reason": "end_turn"}, []) == ""


def test_grok_command_uses_the_call_operator_only_where_powershell_runs_it(monkeypatch):
    monkeypatch.setattr(hr.sys, "executable", r"C:\py\python.exe")
    monkeypatch.setattr(hr.os, "name", "nt")
    assert hr.grok_hooks()["hooks"]["Stop"][0]["hooks"][0]["command"].startswith(r'& "C:\py\python.exe" -m')
    monkeypatch.setattr(hr.os, "name", "posix")
    assert hr.grok_hooks()["hooks"]["Stop"][0]["hooks"][0]["command"].startswith(r'"C:\py\python.exe" -m')


def test_agy_command_is_shell_agnostic(monkeypatch):
    monkeypatch.setattr(hr.sys, "executable", "C:/py/python.exe")
    assert hr.agy_hook()["PreInvocation"][0]["command"] == "C:/py/python.exe -m covasant_partyline.agy_hook"
    monkeypatch.setattr(hr.sys, "executable", "C:/Program Files/py/python.exe")
    assert hr.agy_hook()["PreInvocation"][0]["command"].startswith('"C:/Program Files/py/python.exe" -m')
