import json
import os
from covasant_partyline import outbox
from covasant_partyline.outbox import TicketTable, spool_message


def test_spool_path_default_is_cwd_parent(tmp_path):
    cwd = tmp_path / "repos" / "web_1"
    cwd.mkdir(parents=True)
    p = outbox.resolve_spool_path("alan_web_1", None, str(cwd))
    assert p == os.path.join(str(tmp_path / "repos"), "partyline_alan_web_1.yaml")


def test_spool_path_directory_value(tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    p = outbox.resolve_spool_path("alan_web_1", str(d), os.getcwd())
    assert p == os.path.join(str(d), "partyline_alan_web_1.yaml")


def test_spool_path_file_value_verbatim(tmp_path):
    f = str(tmp_path / "shared.yaml")
    p = outbox.resolve_spool_path("alan_web_1", f, os.getcwd())
    assert p == f


def test_spool_path_agent_id_token(tmp_path):
    val = str(tmp_path / "partyline_{agent_id}.yaml")
    p = outbox.resolve_spool_path("alan_web_1", val, os.getcwd())
    assert p == str(tmp_path / "partyline_alan_web_1.yaml")


def test_resolve_agent_id_prefers_flag(monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "from_env")
    assert outbox.resolve_agent_id("from_flag") == "from_flag"


def test_resolve_agent_id_env_then_config(monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "from_env")
    assert outbox.resolve_agent_id(None) == "from_env"


def test_resolve_agent_id_falls_back_to_config(monkeypatch, tmp_path):
    """Test that resolve_agent_id reads from .partyline/config.json when no flag or env var."""
    monkeypatch.delenv("PARTYLINE_AGENT_ID", raising=False)
    config_dir = tmp_path / ".partyline"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"agentId": "from_config"}))
    monkeypatch.chdir(tmp_path)
    assert outbox.resolve_agent_id(None) == "from_config"


def test_resolve_agent_id_falls_back_to_config_snake_case(monkeypatch, tmp_path):
    """Test that resolve_agent_id reads agent_id (snake_case) key from config."""
    monkeypatch.delenv("PARTYLINE_AGENT_ID", raising=False)
    config_dir = tmp_path / ".partyline"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps({"agent_id": "snake_config"}))
    monkeypatch.chdir(tmp_path)
    assert outbox.resolve_agent_id(None) == "snake_config"


def test_spool_path_directory_trailing_slash(tmp_path):
    """Test that a non-existent directory with trailing / is still treated as a directory."""
    nonexistent_dir = str(tmp_path / "nonexistent" / "dir") + "/"
    p = outbox.resolve_spool_path("alan_web_1", nonexistent_dir, os.getcwd())
    expected = os.path.join(nonexistent_dir.rstrip("/" + os.sep), "partyline_alan_web_1.yaml")
    assert p == expected


def test_ticket_lifecycle_pending_to_sent():
    t = TicketTable()
    tk = t.new("post_message")
    assert t.get(tk)["status"] == "pending"
    t.mark_sent(tk, 42)
    got = t.get(tk)
    assert got["status"] == "sent" and got["message_id"] == 42


def test_ticket_failed_records_spool():
    t = TicketTable()
    tk = t.new("reply")
    t.mark_failed(tk, "connect timeout", "/tmp/partyline_a.yaml")
    got = t.get(tk)
    assert got["status"] == "failed"
    assert got["spooled_to"] == "/tmp/partyline_a.yaml"
    assert got["error"] == "connect timeout"


def test_unknown_ticket():
    assert TicketTable().get("nope")["status"] == "unknown"


def test_ticket_table_is_bounded():
    from covasant_partyline import outbox
    t = TicketTable()
    tickets = [t.new("post_message") for _ in range(outbox._TICKET_BOUND + 10)]
    assert t.get(tickets[0])["status"] == "unknown"    # oldest evicted
    assert t.get(tickets[-1])["status"] == "pending"   # newest retained


def test_spool_writes_appendable_yaml(tmp_path):
    p = str(tmp_path / "spool.yaml")
    rec1 = {"ticket": "t1", "agent_id": "a", "verb": "post_message",
            "message": {"message_type": "status", "summary": "hi", "payload": "line1\nline2"}}
    rec2 = {"ticket": "t2", "agent_id": "a", "verb": "reply",
            "message": {"message_type": "ack", "summary": None, "payload": "ok"}}
    assert spool_message(p, rec1) is True
    assert spool_message(p, rec2) is True
    text = open(p, encoding="utf-8").read()
    # Two list items appended
    assert text.count("- ticket:") == 2
    assert "t1" in text and "t2" in text
    # Multiline payloads must not corrupt the doc (round-trips through a YAML loader if present)
    try:
        import yaml
        docs = yaml.safe_load(text)
        assert len(docs) == 2
        assert docs[0]["message"]["payload"] == "line1\nline2"
    except ImportError:
        pass  # PyYAML not required at runtime; structural check above suffices


def test_spool_never_raises_on_bad_path():
    # A directory path that can't be written must return False, not raise.
    assert spool_message("/nonexistent-root-xyz/\x00bad", {"ticket": "t"}) is False
