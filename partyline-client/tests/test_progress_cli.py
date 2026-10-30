from covasant_partyline import cli


def test_progress_start_uses_folder_config_identity_and_task_context(monkeypatch, tmp_path):
    folder = tmp_path / "Aura_1"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setattr(cli, "resolve_agent_id", lambda: "alan_aura_1")
    captured = []
    monkeypatch.setattr(cli, "_request", lambda *args: captured.append(args) or {})
    args = cli.build_parser().parse_args(["progress", "start", "Fix retries", "--workstream", "release", "--issue", "owner/repo#1"])
    args.func(args)
    method, _, path, body = captured[0]
    assert method == "POST" and path == "/api/progress"
    assert body["folder"] == "aura_1" and body["agent_id"] == "alan_aura_1"
    assert body["status"] == "started" and body["issue_ref"] == "owner/repo#1"
    assert body["workdir"] == str(folder) and body["operation_id"]


def test_progress_show_and_list_are_read_only(monkeypatch):
    captured = []
    monkeypatch.setattr(cli, "_request", lambda *args: captured.append(args) or [])
    for argv in (["progress", "show", "--folder", "aura_1"], ["progress", "list", "--all"]):
        args = cli.build_parser().parse_args(argv)
        args.func(args)
    assert all(row[0] == "GET" for row in captured)
    assert "folder=aura_1" in captured[0][2] and "include_done=true" in captured[1][2]
