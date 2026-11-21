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


def test_ensure_login_noop_when_token_mints(monkeypatch):
    monkeypatch.setattr(cli, "_mint_token", lambda profile: "tok")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran login")))
    setup.ensure_login("dev-eastus2", "https://adb-1.azuredatabricks.net")


def test_ensure_login_runs_browser_login_then_retries(monkeypatch):
    tokens = iter([None, "tok"])
    monkeypatch.setattr(cli, "_mint_token", lambda profile: next(tokens))
    monkeypatch.setattr(setup.shutil, "which", lambda name: "databricks")
    ran = []
    monkeypatch.setattr(setup.subprocess, "run", lambda cmd, **k: ran.append(cmd))
    setup.ensure_login("dev-eastus2", "https://adb-1.azuredatabricks.net")
    assert ran == [["databricks", "auth", "login", "--host", "https://adb-1.azuredatabricks.net", "--profile", "dev-eastus2"]]


def test_ensure_login_without_host_gives_the_exact_command(monkeypatch):
    monkeypatch.setattr(cli, "_mint_token", lambda profile: None)
    monkeypatch.setattr(setup.shutil, "which", lambda name: "databricks")
    try:
        setup.ensure_login("dev-eastus2", None)
    except SystemExit as e:
        assert "databricks auth login --host <workspace-url> --profile dev-eastus2" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_ensure_login_exits_if_login_did_not_complete(monkeypatch):
    monkeypatch.setattr(cli, "_mint_token", lambda profile: None)
    monkeypatch.setattr(setup.shutil, "which", lambda name: "databricks")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: None)
    try:
        setup.ensure_login("p", "https://h")
    except SystemExit as e:
        assert "did not complete" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_launchers_write_every_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.sys, "executable", r"C:\py\python.exe")
    names = sorted(p.name for p in setup._launchers(tmp_path))
    assert names == sorted(f"{n}{ext}" for n in ("agy-pl", "claude-pl", "codex-pl", "genie-pl", "grok-pl", "kimi-pl", "cmdc-pl", "cursor-agent-pl", "devin-pl", "omni-pl") for ext in ("", ".cmd"))
    assert '-m covasant_partyline.harness_runner cursor "$@"' in (tmp_path / "cursor-agent-pl").read_text()
    assert '-m covasant_partyline.harness_runner cmdc "$@"' in (tmp_path / "cmdc-pl").read_text()
    assert '-m covasant_partyline.harness_runner devin "$@"' in (tmp_path / "devin-pl").read_text()
    assert "-m covasant_partyline.harness_runner grok %*" in (tmp_path / "grok-pl.cmd").read_text()
    assert '-m covasant_partyline.harness_runner agy "$@"' in (tmp_path / "agy-pl").read_text()
    assert "-m covasant_partyline.codex_runner" in (tmp_path / "codex-pl.cmd").read_text()
    assert r"C:\py\python.exe" in (tmp_path / "codex-pl").read_text()
    assert "server:partyline-channel" in (tmp_path / "claude-pl.cmd").read_text()
    agy = (tmp_path / "agy-pl").read_bytes()
    assert b"\r" not in agy
    assert b"/mnt/c/py/python.exe" in agy
    assert b"WSL_DISTRO_NAME" in agy
    assert b"\r\r\n" not in (tmp_path / "agy-pl.cmd").read_bytes()
    assert b"\r" not in (tmp_path / "claude-pl").read_bytes()


def test_posix_python_launcher_has_no_wsl_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.sys, "executable", "/usr/bin/python3")
    setup._launchers(tmp_path)
    text = (tmp_path / "grok-pl").read_bytes().decode()
    assert "WSL_DISTRO_NAME" not in text
    assert 'exec "/usr/bin/python3" -m covasant_partyline.harness_runner grok "$@"' in text


def test_windows_path_to_wsl():
    assert setup._windows_path_to_wsl(r"C:\Python314\python.exe") == "/mnt/c/Python314/python.exe"
    assert setup._windows_path_to_wsl("/usr/bin/python3") is None
