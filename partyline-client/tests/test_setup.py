"""`partyline init` id allocation and idempotency."""

import json

from covasant_partyline import cli, setup


def test_next_agent_id_picks_lowest_free():
    ids = ["alan_aura_1", "alan_aura_3", "alan_other_1", "bob_aura_2"]
    assert setup.next_agent_id("alan", "aura", ids) == "alan_aura_2"
    assert setup.next_agent_id("alan", "new", ids) == "alan_new_1"


def test_next_agent_id_slugs_repo():
    assert setup.next_agent_id("Alan", "Aura Web", []) == "alan_aura-web_1"


def test_agent_ids_accepts_list_or_wrapped():
    assert setup._agent_ids([{"agent_id": "a"}, {"x": 1}]) == ["a"]
    assert setup._agent_ids({"agents": [{"agent_id": "b"}]}) == ["b"]
    assert setup._agent_ids(None) == []


def test_init_writes_config_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup, "USER_FILE", tmp_path / "user.json")
    monkeypatch.setattr(setup, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("PARTYLINE_TOKEN", "t")
    calls = []

    def fake_request(method, base, path, body=None):
        calls.append((method, path))
        return [{"agent_id": f"alan_{setup._slug(tmp_path.name)}_1"}] if method == "GET" else {}

    monkeypatch.setattr(cli, "_request", fake_request)
    args = cli.build_parser().parse_args(["init", "--owner", "alan"])
    args.url = "https://app.example"
    args.profile = None
    setup.cmd_init(args)
    cfg = json.loads((tmp_path / ".partyline" / "config.json").read_text())
    assert cfg["agentId"].endswith("_2") and cfg["url"] == "https://app.example"
    assert ".partyline/" in (tmp_path / ".gitignore").read_text()

    n = len(calls)
    setup.cmd_init(args)  # already initialised: no network, no rewrite
    assert len(calls) == n
