import json
import subprocess
from datetime import datetime, timezone

from covasant_partyline import codex_runner as cr


def _rollout(root, sid, cwd, started):
    d = root / "2026" / "10" / "26"
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(started, timezone.utc).isoformat().replace("+00:00", "Z")
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "timestamp": ts}}
    (d / f"rollout-x-{sid}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")


def test_find_thread_picks_newest_matching_cwd_after_launch(tmp_path):
    _rollout(tmp_path, "old", r"C:\repo", 100)  # before launch
    _rollout(tmp_path, "other", r"C:\elsewhere", 300)  # wrong directory
    _rollout(tmp_path, "a", r"C:\repo", 200)
    _rollout(tmp_path, "b", r"c:\REPO", 250)  # same dir, case-insensitive; newest
    assert cr.find_thread(tmp_path, r"C:\repo", since=150) == "b"


def test_find_thread_none_when_nothing_new(tmp_path):
    _rollout(tmp_path, "old", r"C:\repo", 100)
    assert cr.find_thread(tmp_path, r"C:\repo", since=150) is None
    assert cr.find_thread(tmp_path / "missing", r"C:\repo", since=0) is None


def test_prompt_is_labelled_untrusted_and_names_the_ack():
    text = cr.format_prompt({"id": 9, "agent_id": "alan_x_1", "message_type": "status", "payload": "do it"}, "me")
    assert "untrusted" in text and "#9" in text and "alan_x_1" in text and "do it" in text
    assert "partyline ack me 9" in text


def test_queue_never_inherits_stdin(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cr.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    assert cr.queue_message("tid", "hi")
    assert seen["cmd"][1:] == ["queue", "--thread", "tid", "--message", "hi"]
    assert seen["kw"]["stdin"] is subprocess.DEVNULL


def test_launch_logs_to_file_not_the_terminal(tmp_path, monkeypatch, capsys):
    """Importing `channel` configures stderr logging; launch() must still redirect to the file."""
    import logging

    monkeypatch.setattr(cr, "LOG_FILE", tmp_path / "codex-pl.log")
    monkeypatch.setattr(cr.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(cr.channel, "resolve_settings", lambda _a: cr.channel.Settings("me", "http://localhost", None))
    monkeypatch.setattr(cr, "run_loop", lambda *a, **k: None)
    monkeypatch.setattr(cr, "register_self", lambda h: None)
    monkeypatch.setattr(cr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    try:
        cr.launch([])
    except SystemExit:
        pass
    for h in logging.getLogger().handlers:
        h.flush()
    assert capsys.readouterr().err == ""
    assert "codex-pl started agent=me" in (tmp_path / "codex-pl.log").read_text()
