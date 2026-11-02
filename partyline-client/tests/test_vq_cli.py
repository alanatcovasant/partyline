import sys

import pytest

from covasant_partyline import cli
from covasant_partyline.vq import run_script

PY = f'"{sys.executable}"'


def _exit(code):
    return f'{PY} -c "raise SystemExit({code})"'


@pytest.fixture()
def captured(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "resolve_agent_id", lambda: "alan_x_1")
    monkeypatch.setattr(cli, "_request", lambda *a: calls.append(a) or {"script": _exit(0)})
    return calls


def _go(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def test_record_is_never_marked_executed(captured):
    _go(["vq", "record", "--target", "dev-eastus2", "--issue", "o/r#1", "--exit-code", "0"])
    method, _, path, body = captured[0]
    assert (method, path) == ("POST", "/api/vq/runs") and body["executed"] is False and body["operation_id"]


def test_run_marks_executed_and_exits_with_the_script_code(captured):
    with pytest.raises(SystemExit) as e:
        _go(["vq", "run", "--target", "dev-eastus2", "--issue", "o/r#1"])
    assert e.value.code == 0
    body = captured[-1][3]
    assert body["executed"] is True and body["exit_code"] == 0


def test_run_exit_codes_and_inconclusive_mapping():
    assert run_script(_exit(1), 30)[0] == 1
    assert run_script(_exit(2), 30)[0] == 2
    assert run_script(_exit(7), 30)[0] == 2  # an unknown code is never a PASS
    assert run_script(f'{PY} -c "import time; time.sleep(5)"', 1)[0] == 2  # a timeout is never a PASS


def test_run_refuses_an_item_without_a_script(monkeypatch):
    monkeypatch.setattr(cli, "resolve_agent_id", lambda: "a")
    monkeypatch.setattr(cli, "_request", lambda *a: {"script": None})
    with pytest.raises(SystemExit, match="no script"):
        _go(["vq", "run", "--target", "t", "--issue", "o/r#1"])


def test_list_and_show_are_read_only(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_request", lambda *a: seen.append(a) or [])
    _go(["vq", "list", "--target", "dev-eastus2", "--state", "pending"])
    _go(["vq", "show", "--target", "dev-eastus2", "--issue", "o/r#1"])
    assert all(r[0] == "GET" for r in seen) and "state=pending" in seen[0][2]
