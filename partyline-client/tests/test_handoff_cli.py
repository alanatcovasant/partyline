import pytest

from covasant_partyline import cli

TASK = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "resolve_agent_id", lambda: "alan_aura_2")
    monkeypatch.setattr(cli, "_request", lambda *a: calls.append(a) or {})
    return calls


def _go(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def test_offer_posts_to_the_task_handoff_route(captured):
    _go(["progress", "handoff", "offer", "--task", TASK, "--to", "alan_aura_3", "--note", "out of tokens",
         "--next", "re-run step 3"])
    method, _, path, body = captured[0]
    assert (method, path) == ("POST", f"/api/progress/{TASK}/handoff")
    assert body["action"] == "offer" and body["to"] == "alan_aura_3" and body["agent_id"] == "alan_aura_2"
    assert body["next_action"] == "re-run step 3" and body["operation_id"] and "folder" not in body


def test_accept_and_takeover_move_the_task_into_this_folder(captured, monkeypatch, tmp_path):
    folder = tmp_path / "Aura_7"
    folder.mkdir()
    monkeypatch.chdir(folder)
    _go(["progress", "handoff", "accept", "--task", TASK])
    _go(["progress", "handoff", "takeover", "--task", TASK, "--note", "owner gone"])
    assert [c[3]["action"] for c in captured] == ["accept", "takeover"]
    assert all(c[3]["folder"] == "aura_7" for c in captured)
    assert captured[1][3]["note"] == "owner gone"


def test_offer_requires_a_recipient_and_a_task():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["progress", "handoff", "offer", "--task", TASK])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["progress", "handoff", "accept"])


def test_note_carries_the_next_action(captured, monkeypatch, tmp_path):
    folder = tmp_path / "aura_1"
    folder.mkdir()
    monkeypatch.chdir(folder)
    _go(["progress", "note", "tests pass", "--next", "run the deployed validator"])
    assert captured[0][3]["next_action"] == "run the deployed validator"
