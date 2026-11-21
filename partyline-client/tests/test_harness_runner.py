import io
import json
import os
import pytest

from covasant_partyline import check_unread, harness_runner as hr


def test_which_cli_finds_windows_exe_from_wsl(monkeypatch):
    monkeypatch.setattr(hr.os, "name", "posix")
    monkeypatch.setattr(hr.shutil, "which", lambda name: "/mnt/c/agy.exe" if name == "agy.exe" else None)
    assert hr.which_cli(("agy",)) == "/mnt/c/agy.exe"


def test_which_cli_does_not_append_exe_on_windows(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return None

    monkeypatch.setattr(hr.os, "name", "nt")
    monkeypatch.setattr(hr.shutil, "which", fake_which)
    assert hr.which_cli(("agy",)) is None
    assert seen == ["agy"]


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


@pytest.mark.parametrize("harness,flag", [("grok", "--always-approve"), ("cmdc", "--yolo"), ("cursor", "--yolo"), ("devin", "--permission-mode"), ("cline", "--version")])
def test_launch_installs_then_runs_the_cli_with_args_untouched(monkeypatch, harness, flag):
    calls = []
    if harness == "cline":
        # cline-pl installs hooks only when a clone identity resolves; give it one.
        monkeypatch.setattr(hr, "_config_from", lambda root: {"agentId": "me_1"})
    monkeypatch.setattr(hr.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(hr, "cursor_executable", lambda: "/bin/cursor")
    monkeypatch.setitem(hr.HARNESSES, harness, ((harness,), lambda: calls.append("install")))
    monkeypatch.setattr(hr, "register_self", lambda h: calls.append(f"register:{h}"))

    class R:
        returncode = 7

    monkeypatch.setattr(hr.subprocess, "run", lambda cmd: calls.append(cmd) or R())
    try:
        hr.launch(harness, [flag])
    except SystemExit as e:
        assert e.code == 7
    assert calls == ["install", f"register:{harness}", [f"/bin/{harness}", flag]]


def test_launch_exits_when_the_cli_is_missing(monkeypatch):
    monkeypatch.setattr(hr.shutil, "which", lambda n: None)
    try:
        hr.launch("agy", [])
    except SystemExit as e:
        assert "not found" in str(e.code)


def _fake_cline_run(monkeypatch, calls):
    monkeypatch.setattr(hr.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(hr, "register_self", lambda h: calls.append(f"register:{h}"))

    class R:
        returncode = 0

    monkeypatch.setattr(hr.subprocess, "run", lambda cmd: calls.append(cmd) or R())


def test_launch_cline_outside_a_clone_skips_hooks_warns_and_runs(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PARTYLINE_AGENT_ID", raising=False)
    calls = []
    _fake_cline_run(monkeypatch, calls)

    with pytest.raises(SystemExit) as exc:
        hr.launch("cline", ["--version"])
    assert exc.value.code == 0

    assert not (tmp_path / ".clinerules").exists()  # no hooks written
    assert "register:cline" in calls  # registration degrades quietly, no crash
    assert calls[-1] == ["/bin/cline", "--version"]
    err = capsys.readouterr().err
    assert ".partyline" in err and "hook" in err.lower()


def test_launch_cline_in_a_clone_writes_hooks_and_registers(monkeypatch, tmp_path):
    cfg = tmp_path / ".partyline"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"agentId": "alan_partyline_cline_1"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PARTYLINE_AGENT_ID", raising=False)
    calls = []
    _fake_cline_run(monkeypatch, calls)

    with pytest.raises(SystemExit) as exc:
        hr.launch("cline", [])
    assert exc.value.code == 0

    shims = sorted(p.name for p in (tmp_path / ".clinerules" / "hooks").iterdir())
    assert shims == ["TaskStart", "UserPromptSubmit"]
    assert "PARTYLINE_AGENT_ID='alan_partyline_cline_1'" in (tmp_path / ".clinerules" / "hooks" / "TaskStart").read_text()
    assert calls == ["register:cline", ["/bin/cline"]]


def _run_peek(monkeypatch, tmp_path, payload, msgs, fmt="grok_stop"):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", fmt)
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


def test_cmdc_install_preserves_settings_and_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    own = {"hooks": [{"type": "command", "command": "my-hook"}]}
    path.write_text(json.dumps({"permissions": {"allow": ["read"]}, "hooks": {"Stop": [own]}}))
    assert hr.install_cmdc_hooks(path) is True
    assert hr.install_cmdc_hooks(path) is False
    data = json.loads(path.read_text())
    assert data["permissions"] == {"allow": ["read"]}
    assert data["hooks"]["Stop"][0] == own
    for event in ("SessionStart", "Stop"):
        entry = data["hooks"][event][-1]
        assert "matcher" not in entry
        assert entry["hooks"][0]["command"].endswith("-m covasant_partyline.cmdc_hook")


def test_cmdc_install_refuses_to_overwrite_invalid_settings(tmp_path):
    import pytest

    path = tmp_path / "settings.json"
    path.write_text("broken JSON")
    with pytest.raises(ValueError):
        hr.install_cmdc_hooks(path)
    assert path.read_text() == "broken JSON"


def test_cmdc_stop_blocks_once_and_honors_snake_case_retry_flag(monkeypatch, tmp_path):
    assert _run_peek(monkeypatch, tmp_path, {"stop_hook_active": True}, MAIL, fmt="cmdc_stop") == ""
    out = json.loads(_run_peek(monkeypatch, tmp_path, {}, MAIL, fmt="cmdc_stop"))
    assert out["decision"] == "block" and "#5" in out["reason"]
    assert _run_peek(monkeypatch, tmp_path, {}, MAIL, fmt="cmdc_stop") == ""
    assert _run_peek(monkeypatch, tmp_path, {}, [], fmt="cmdc_stop") == ""


def test_cmdc_hook_dispatches_session_start_and_stop(monkeypatch):
    from covasant_partyline import cmdc_hook, hook

    called = []
    monkeypatch.setattr(hook, "main", lambda: called.append("start"))
    monkeypatch.setattr(check_unread, "main", lambda: called.append("stop"))
    for event, fmt in (("SessionStart", "cmdc"), ("Stop", "cmdc_stop")):
        monkeypatch.setattr(cmdc_hook.sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event})))
        cmdc_hook.main()
        assert cmdc_hook.os.environ["PARTYLINE_HOOK_FORMAT"] == fmt
    assert called == ["start", "stop"]


def test_cmdc_session_digest_uses_json_context(monkeypatch, capsys):
    from covasant_partyline import hook

    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cmdc")
    hook._emit("recent traffic")
    assert json.loads(capsys.readouterr().out) == {"additionalContext": "recent traffic"}


def test_devin_install_preserves_settings_and_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / "config.json"
    own = {"hooks": [{"type": "command", "command": "my-hook"}]}
    path.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [own]}}))
    assert hr.install_devin_hooks(path) is True
    assert hr.install_devin_hooks(path) is False
    data = json.loads(path.read_text())
    assert data["model"] == "opus"
    assert data["hooks"]["Stop"][0] == own
    for event in ("SessionStart", "Stop"):
        entry = data["hooks"][event][-1]
        assert entry["matcher"] == ""
        assert entry["hooks"][0]["command"].endswith("-m covasant_partyline.devin_hook")


def test_devin_install_refuses_to_overwrite_invalid_config(tmp_path):
    import pytest

    path = tmp_path / "config.json"
    path.write_text("broken JSON")
    with pytest.raises(ValueError):
        hr.install_devin_hooks(path)
    assert path.read_text() == "broken JSON"


def test_devin_stop_blocks_once_and_honors_snake_case_retry_flag(monkeypatch, tmp_path):
    assert _run_peek(monkeypatch, tmp_path, {"stop_hook_active": True}, MAIL, fmt="devin_stop") == ""
    out = json.loads(_run_peek(monkeypatch, tmp_path, {}, MAIL, fmt="devin_stop"))
    assert out["decision"] == "block" and "#5" in out["reason"]
    assert _run_peek(monkeypatch, tmp_path, {}, MAIL, fmt="devin_stop") == ""
    assert _run_peek(monkeypatch, tmp_path, {}, [], fmt="devin_stop") == ""


def test_devin_hook_resolves_project_dir_and_restores_process(monkeypatch, tmp_path):
    from covasant_partyline import devin_hook, hook

    original = os.getcwd()
    calls = []
    monkeypatch.setattr(hook, "main", lambda: calls.append((os.getcwd(), os.environ["PARTYLINE_HOOK_FORMAT"])))
    monkeypatch.setattr(check_unread, "main", lambda: calls.append((os.getcwd(), os.environ["PARTYLINE_HOOK_FORMAT"])))
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(tmp_path))
    for event in ("SessionStart", "Stop"):
        monkeypatch.setattr(devin_hook.sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event})))
        devin_hook.main()
        assert os.getcwd() == original
    assert calls == [(str(tmp_path), "devin"), (str(tmp_path), "devin_stop")]


def test_devin_session_digest_uses_hook_specific_output(monkeypatch, capsys):
    from covasant_partyline import hook

    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "devin")
    hook._emit("recent traffic")
    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "recent traffic"}}


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


def test_kimi_install_replaces_the_empty_default_and_is_idempotent(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('default_model = "m"\nhooks = []\ntheme = "dark"\n')
    assert hr.install_kimi_hooks(path) is True
    text = path.read_text()
    assert "\nhooks = []" not in text and text.count("[[hooks]]") == 1
    assert 'event = "Stop"' in text and "covasant_partyline.kimi_hook" in text
    assert hr.install_kimi_hooks(path) is False
    assert path.read_text() == text


def test_kimi_install_keeps_the_users_own_hook_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[hooks]]\nevent = "Stop"\ncommand = "x"\n')
    hr.install_kimi_hooks(path)
    hr.install_kimi_hooks(path)
    import tomllib

    hooks = tomllib.loads(path.read_text())["hooks"]
    assert [h["command"] for h in hooks][0] == "x" and len(hooks) == 2
    assert "covasant_partyline.kimi_hook" in hooks[1]["command"]


def test_kimi_install_leaves_a_users_inline_hooks_alone(tmp_path):
    path = tmp_path / "config.toml"
    original = 'hooks = [{ event = "Stop", command = "x" }]\n'
    path.write_text(original)
    assert hr.install_kimi_hooks(path) is False
    assert path.read_text() == original




def test_kimi_stop_denies_once_with_the_mail_as_the_reason(monkeypatch, tmp_path):
    out = json.loads(_run_peek(monkeypatch, tmp_path, {}, MAIL, fmt="kimi_stop"))
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny" and "addressed to you" in hook["permissionDecisionReason"]
    assert _run_peek(monkeypatch, tmp_path, {}, MAIL, fmt="kimi_stop") == ""  # deduped, no re-block loop


def _install_cline(tmp_path, monkeypatch, agent_id="alan_partyline_cline_1", nt=False):
    # nt is threaded through rather than patching os.name: flipping the real os.name
    # mid-test re-dispatches pathlib to WindowsPath and breaks tmp_path.
    monkeypatch.setattr(hr.cli, "resolve_agent_id", lambda *a, **k: agent_id)
    return hr.install_cline_hooks(tmp_path, nt=nt)


def test_cline_install_writes_unix_shims_and_is_idempotent(tmp_path, monkeypatch):
    assert _install_cline(tmp_path, monkeypatch) is True
    hooks = tmp_path / ".clinerules" / "hooks"
    start, peek = hooks / "TaskStart", hooks / "UserPromptSubmit"
    for shim, module in ((start, "covasant_partyline.hook"), (peek, "covasant_partyline.check_unread")):
        text = shim.read_text(encoding="utf-8")
        assert shim.suffix == ""  # Unix: extensionless, discovered by filename
        assert os.access(shim, os.X_OK)
        assert "PARTYLINE_HOOK_FORMAT=cline" in text
        assert "PARTYLINE_AGENT_ID='alan_partyline_cline_1'" in text
        assert f"-m {module}" in text
    assert _install_cline(tmp_path, monkeypatch) is False  # unchanged config: no-op
    # a changed identity re-stamps the shims
    assert _install_cline(tmp_path, monkeypatch, agent_id="alan_partyline_cline_2") is True
    assert "alan_partyline_cline_2" in (tmp_path / ".clinerules" / "hooks" / "TaskStart").read_text()


def test_cline_install_writes_powershell_shims_on_windows(tmp_path, monkeypatch):
    assert _install_cline(tmp_path, monkeypatch, nt=True) is True
    hooks = tmp_path / ".clinerules" / "hooks"
    for name in ("TaskStart.ps1", "UserPromptSubmit.ps1"):
        text = (hooks / name).read_text(encoding="utf-8")
        assert "$env:PARTYLINE_HOOK_FORMAT = 'cline'" in text
        assert "$env:PARTYLINE_AGENT_ID = 'alan_partyline_cline_1'" in text
        assert "exit 0" in text
    assert not (hooks / "TaskStart").exists()  # only the .ps1 pair on Windows
    assert _install_cline(tmp_path, monkeypatch, nt=True) is False


def test_cline_shims_refuse_gracefully_without_an_identity(tmp_path, monkeypatch):
    assert _install_cline(tmp_path, monkeypatch, agent_id=None) is True
    hooks = tmp_path / ".clinerules" / "hooks"
    assert '[ -n "$PARTYLINE_AGENT_ID" ] || exit 0' in (hooks / "TaskStart").read_text()
    # both unix shims carry the guard and still end silent-success
    for shim in ("TaskStart", "UserPromptSubmit"):
        text = (hooks / shim).read_text()
        assert "exit 0" in text.splitlines()[-1]


def test_cline_is_registered_as_a_harness():
    assert hr.HARNESSES["cline"][0] == ("cline",)
    assert hr.HARNESSES["cline"][1] is hr.install_cline_hooks


def test_cline_install_stamps_identity_from_the_clone_config(tmp_path, monkeypatch):
    # the installer must read .partyline/config.json next to the shims, not the
    # process cwd's config — a caller may install into another clone.
    monkeypatch.delenv("PARTYLINE_AGENT_ID", raising=False)
    cfg_dir = tmp_path / ".partyline"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text('{"agentId": "alan_partyline_cline_9"}', encoding="utf-8")
    assert hr.install_cline_hooks(tmp_path) is True
    text = (tmp_path / ".clinerules" / "hooks" / "TaskStart").read_text(encoding="utf-8")
    assert "PARTYLINE_AGENT_ID='alan_partyline_cline_9'" in text
