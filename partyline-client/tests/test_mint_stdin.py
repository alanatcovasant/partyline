"""`databricks auth token` must never inherit our stdin.

Under an MCP server (the channel) or a Claude Code hook, stdin is the live protocol/event
pipe. The CLI hangs reading it whenever it has to refresh an expired token, so every mint
timed out and the channel retried tokenless forever (silent idle-wake loss after ~1h).
"""

import json
import subprocess

import pytest

from covasant_partyline import check_unread, cli, hook


class _Done:
    stdout = json.dumps({"access_token": "tok"})


@pytest.mark.parametrize(
    "mint",
    [
        lambda: cli._mint_token("p"),
        lambda: check_unread._token("p"),
        lambda: hook._token("p"),
    ],
    ids=["cli", "check_unread", "hook"],
)
def test_mint_does_not_inherit_stdin(monkeypatch, mint):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda _: "databricks")
    assert mint() == "tok"
    assert seen.get("stdin") is subprocess.DEVNULL
