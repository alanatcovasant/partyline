"""A failed mint must be loud exactly once — the silent 'HTTP 401: {}' was undebuggable.

Every mint call site (cli, hook, check_unread, channel) collapses onto cli._mint_token,
so one warning implementation covers all of them.
"""

import json
import subprocess

from covasant_partyline import cli


def test_mint_failure_warns_once_with_reason(monkeypatch, capsys):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("databricks")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_mint_warned", False)

    assert cli._mint_token("dev-eastus2") is None
    assert cli._mint_token("dev-eastus2") is None  # second failure stays quiet

    err = capsys.readouterr().err
    assert err.count("token mint failed") == 1
    assert "dev-eastus2" in err
    assert "not found on PATH" in err
    assert "PARTYLINE_TOKEN" in err


def test_mint_failure_includes_cli_stderr(monkeypatch, capsys):
    def fake_run(cmd, **kw):
        raise subprocess.CalledProcessError(
            1, cmd, stderr="oauth2: token refresh failed\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_mint_warned", False)

    assert cli._mint_token("dev-eastus2") is None
    err = capsys.readouterr().err
    assert "exited 1" in err
    assert "token refresh failed" in err


def test_mint_tokenless_output_is_a_failure(monkeypatch, capsys):
    class _Done:
        stdout = json.dumps({"token_type": "Bearer"})  # no access_token

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(cli, "_mint_warned", False)

    assert cli._mint_token("p") is None
    assert "no access_token" in capsys.readouterr().err


def test_http_401_with_empty_body_explains_the_mint():
    msg = cli._explain_http_error(401, "{}")
    assert "unauthenticated" in msg
    assert "databricks auth token" in msg


def test_http_error_with_detail_untouched():
    raw = json.dumps({"detail": {"error": "unknown_recipient", "recipient": "alan_x_9"}})
    msg = cli._explain_http_error(404, raw)
    assert "unknown recipient" in msg


def _jwt(exp: float) -> str:
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"hdr.{payload.rstrip('=')}.sig"


def test_expired_env_token_is_dropped(monkeypatch, capsys):
    import os
    import time

    monkeypatch.setenv("PARTYLINE_TOKEN", _jwt(time.time() - 60))
    assert cli.expired_env_token_exp() is not None
    cli._drop_stale_env_token("test")
    assert "PARTYLINE_TOKEN" not in os.environ
    assert "expired" in capsys.readouterr().err


def test_valid_env_token_is_kept(monkeypatch):
    import time

    monkeypatch.setenv("PARTYLINE_TOKEN", _jwt(time.time() + 3600))
    assert cli.expired_env_token_exp() is None
