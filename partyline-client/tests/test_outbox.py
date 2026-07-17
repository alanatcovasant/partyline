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
