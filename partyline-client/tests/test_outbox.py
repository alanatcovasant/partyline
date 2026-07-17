import json
import os
from covasant_partyline import outbox
from covasant_partyline.outbox import TicketTable


def test_spool_path_default_is_cwd_parent(tmp_path):
    cwd = tmp_path / "repos" / "aura_3"
    cwd.mkdir(parents=True)
    p = outbox.resolve_spool_path("alan_aura_3", None, str(cwd))
    assert p == os.path.join(str(tmp_path / "repos"), "partyline_alan_aura_3.yaml")


def test_spool_path_directory_value(tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    p = outbox.resolve_spool_path("alan_aura_3", str(d), os.getcwd())
    assert p == os.path.join(str(d), "partyline_alan_aura_3.yaml")


def test_spool_path_file_value_verbatim(tmp_path):
    f = str(tmp_path / "shared.yaml")
    p = outbox.resolve_spool_path("alan_aura_3", f, os.getcwd())
    assert p == f


def test_spool_path_agent_id_token(tmp_path):
    val = str(tmp_path / "partyline_{agent_id}.yaml")
    p = outbox.resolve_spool_path("alan_aura_3", val, os.getcwd())
    assert p == str(tmp_path / "partyline_alan_aura_3.yaml")


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
    p = outbox.resolve_spool_path("alan_aura_3", nonexistent_dir, os.getcwd())
    expected = os.path.join(nonexistent_dir.rstrip("/" + os.sep), "partyline_alan_aura_3.yaml")
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
