import json
import os
from covasant_partyline import outbox


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
