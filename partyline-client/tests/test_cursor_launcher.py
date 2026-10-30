import io
import json
import os
import subprocess

import pytest

from covasant_partyline import harness_runner as hr


def test_cursor_installs_preserving_idempotent_user_hooks(tmp_path):
    path = tmp_path / "hooks.json"
    own = {"command": "my-audit"}
    path.write_text(json.dumps({"version": 1, "hooks": {"stop": [own]}, "other": True}))
    assert hr.install_cursor_hooks(path)
    assert not hr.install_cursor_hooks(path)
    data = json.loads(path.read_text())
    assert data["other"] is True and data["hooks"]["stop"][0] == own
    assert data["hooks"]["stop"][-1]["loop_limit"] == 3
    for event in ("sessionStart", "stop"):
        assert data["hooks"][event][-1]["command"].endswith(f"-m covasant_partyline.cursor_hook {event}")


def test_cursor_invalid_settings_are_not_overwritten(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text("invalid")
    with pytest.raises(ValueError):
        hr.install_cursor_hooks(path)
    assert path.read_text() == "invalid"


def test_cursor_prefers_unambiguous_cli_name(monkeypatch):
    monkeypatch.setattr(hr.shutil, "which", lambda n: f"/bin/{n}")
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: pytest.fail("unnecessary probe"))
    assert hr.cursor_executable() == "/bin/cursor-agent"


@pytest.mark.parametrize("help_text,expected", [("Start the Cursor Agent", "/bin/agent"), ("Grok Build Agent", None)])
def test_cursor_checks_generic_agent_identity(monkeypatch, help_text, expected):
    monkeypatch.setattr(hr.shutil, "which", lambda n: "/bin/agent" if n == "agent" else None)
    monkeypatch.setattr(hr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, help_text, ""))
    assert hr.cursor_executable() == expected


def test_cursor_hook_resolves_workspace_from_payload_and_restores_process(monkeypatch, tmp_path):
    from covasant_partyline import cursor_hook, hook, check_unread

    original = os.getcwd()
    calls = []
    monkeypatch.setattr(hook, "main", lambda: calls.append((os.getcwd(), os.environ["PARTYLINE_HOOK_FORMAT"])))
    monkeypatch.setattr(check_unread, "main", lambda: calls.append((os.getcwd(), os.environ["PARTYLINE_HOOK_FORMAT"])))
    for event in ("sessionStart", "stop"):
        monkeypatch.setattr(cursor_hook.sys, "stdin", io.StringIO(json.dumps({"workspace_roots": [str(tmp_path)]})))
        cursor_hook.main(event)
        assert os.getcwd() == original
    assert calls == [(str(tmp_path), "cursor"), (str(tmp_path), "cursor_stop")]


def test_cursor_hook_does_not_use_user_hook_directory_as_workspace(monkeypatch):
    from covasant_partyline import cursor_hook, hook

    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    monkeypatch.setattr(cursor_hook.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(hook, "main", lambda: pytest.fail("no workspace identity"))
    cursor_hook.main("sessionStart")
