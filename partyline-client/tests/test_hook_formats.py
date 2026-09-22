"""Unit tests for the hook output formats (no live network, no live host).

Locks in the per-runtime stdout contracts of check_unread (UserPromptSubmit peek) and
hook (SessionStart digest): Qwen Code requires hookEventName inside the JSON envelope,
VS Code / Claude Code keep the envelope without it, Cline reads its own envelope,
Cursor uses top-level additional_context / followup_message, and Kimi Code reads plain
markdown. Sync tests, matching the rest of this suite.
"""

import io
import json as _json
from datetime import datetime, timezone

from covasant_partyline import check_unread, hook


# --------------------------------------------------------------------------- #
# check_unread._emit
# --------------------------------------------------------------------------- #


def test_check_unread_qwen_format_includes_hook_event_name(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "qwen")
    check_unread._emit("ctx", "UserPromptSubmit")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}}


def test_check_unread_qwen_format_echoes_session_start(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "qwen")
    check_unread._emit("ctx", "SessionStart")
    out = _json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_check_unread_default_format_unchanged(monkeypatch, capsys):
    """The VS Code / Claude Code envelope ships without hookEventName today; adding it
    silently would change a contract two other runtimes depend on."""
    monkeypatch.delenv("PARTYLINE_HOOK_FORMAT", raising=False)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    monkeypatch.delenv("KIMI_PLUGIN_ROOT", raising=False)
    check_unread._emit("ctx")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"hookSpecificOutput": {"additionalContext": "ctx"}}


def test_check_unread_cline_format(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cline")
    check_unread._emit("ctx")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"cancel": False, "contextModification": "ctx"}


def test_check_unread_cursor_format(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cursor")
    check_unread._emit("ctx")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"additional_context": "ctx"}


def test_check_unread_cursor_stop_format(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cursor_stop")
    check_unread._emit("ctx")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"followup_message": "ctx"}


def test_check_unread_antigravity_format(monkeypatch, capsys):
    """Antigravity injects context only from injectSteps; an ephemeralMessage keeps the
    peek out of the transcript, where a userMessage would read as the human asking."""
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "antigravity")
    check_unread._emit("ctx")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"injectSteps": [{"ephemeralMessage": "ctx"}]}


def test_check_unread_antigravity_ignores_hook_event_name(monkeypatch, capsys):
    """Antigravity has one pre-turn event, so the envelope carries no event name to echo."""
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "antigravity")
    check_unread._emit("ctx", "SessionStart")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"injectSteps": [{"ephemeralMessage": "ctx"}]}


def test_check_unread_kimi_emits_plain_markdown(monkeypatch, capsys):
    monkeypatch.delenv("PARTYLINE_HOOK_FORMAT", raising=False)
    monkeypatch.setenv("KIMI_CODE_HOME", "/home/x/.kimi-code")
    check_unread._emit("# ctx")
    assert capsys.readouterr().out == "# ctx\n"


# --------------------------------------------------------------------------- #
# hook._emit (SessionStart digest)
# --------------------------------------------------------------------------- #


def test_hook_qwen_format_wraps_digest_in_envelope(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "qwen")
    hook._emit("## Partyline digest")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "## Partyline digest"}}


def test_hook_default_format_is_plain_markdown(monkeypatch, capsys):
    """Claude Code / Codex SessionStart hooks read plain markdown from stdout —
    the JSON envelope would render as literal JSON in their context."""
    monkeypatch.delenv("PARTYLINE_HOOK_FORMAT", raising=False)
    hook._emit("## Partyline digest")
    assert capsys.readouterr().out == "## Partyline digest\n"


def test_hook_cline_format(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cline")
    hook._emit("digest")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"cancel": False, "contextModification": "digest"}


def test_hook_cursor_format(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cursor")
    hook._emit("## Partyline digest")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"additional_context": "## Partyline digest"}


def test_hook_antigravity_format(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "antigravity")
    hook._emit("digest")
    out = _json.loads(capsys.readouterr().out)
    assert out == {"injectSteps": [{"ephemeralMessage": "digest"}]}


# --------------------------------------------------------------------------- #
# check_unread.main — the full qwen path, stubbed network
# --------------------------------------------------------------------------- #


def test_check_unread_main_qwen_end_to_end(monkeypatch, capsys):
    """Qwen format + a hook payload on stdin → one JSON envelope whose hookEventName is
    echoed from the payload and whose context lists the unread message."""
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "qwen")
    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    monkeypatch.delenv("KIMI_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(check_unread.sys, "stdin", io.StringIO('{"hook_event_name": "UserPromptSubmit"}'))
    monkeypatch.setattr(check_unread.cli, "_load_config", lambda: {})
    monkeypatch.setattr(check_unread.cli, "resolve_url", lambda cli_arg=None, cfg=None: "http://localhost:8000")
    monkeypatch.setattr(check_unread.cli, "resolve_agent_id", lambda cli_arg=None, cfg=None: "alan_partyline_qwen_1")
    monkeypatch.setattr(check_unread.cli, "resolve_profile", lambda cli_arg=None, url=None, cfg=None: None)

    msgs = [
        {
            "id": 42,
            "agent_id": "alan_web_1",
            "message_type": "question",
            "summary": "can you review the PR?",
            "payload": "long body",
        }
    ]

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps(msgs).encode()

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(check_unread.urllib.request, "urlopen", fake_urlopen)

    check_unread.main()
    out = _json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "1 message(s) addressed to you (`alan_partyline_qwen_1`)" in ctx
    assert "#42 [question] from alan_web_1: can you review the PR?" in ctx
    assert "/api/agents/alan_partyline_qwen_1/unread" in captured["url"]
    assert "only_for_me=true" in captured["url"]


def test_check_unread_main_cursor_stop_followup(monkeypatch, capsys):
    """Cursor stop → followup_message when status=completed and the inbox is non-empty."""
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cursor_stop")
    monkeypatch.delenv("PARTYLINE_TOKEN", raising=False)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    monkeypatch.delenv("KIMI_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(check_unread.sys, "stdin", io.StringIO('{"status": "completed", "loop_count": 0}'))
    monkeypatch.setattr(check_unread.cli, "_load_config", lambda: {})
    monkeypatch.setattr(check_unread.cli, "resolve_url", lambda cli_arg=None, cfg=None: "http://localhost:8000")
    monkeypatch.setattr(check_unread.cli, "resolve_agent_id", lambda cli_arg=None, cfg=None: "alan_partyline_cursor_1")
    monkeypatch.setattr(check_unread.cli, "resolve_profile", lambda cli_arg=None, url=None, cfg=None: None)

    msgs = [
        {
            "id": 7,
            "agent_id": "alan_web_1",
            "message_type": "question",
            "summary": "can you take the hooks PR?",
            "payload": "long body",
        }
    ]

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps(msgs).encode()

    monkeypatch.setattr(check_unread.urllib.request, "urlopen", lambda req, timeout=None: _Resp())

    check_unread.main()
    out = _json.loads(capsys.readouterr().out)
    assert "followup_message" in out
    assert "alan_partyline_cursor_1" in out["followup_message"]
    assert "#7 [question]" in out["followup_message"]
    assert "Partyline wake" in out["followup_message"]


def test_check_unread_main_cursor_stop_silent_when_aborted(monkeypatch, capsys):
    monkeypatch.setenv("PARTYLINE_HOOK_FORMAT", "cursor_stop")
    monkeypatch.setattr(check_unread.sys, "stdin", io.StringIO('{"status": "aborted", "loop_count": 0}'))
    monkeypatch.setattr(check_unread.cli, "_load_config", lambda: {})
    monkeypatch.setattr(check_unread.cli, "resolve_url", lambda cli_arg=None, cfg=None: "http://localhost:8000")
    monkeypatch.setattr(check_unread.cli, "resolve_agent_id", lambda cli_arg=None, cfg=None: "alan_partyline_cursor_1")
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not fetch when aborted")

    monkeypatch.setattr(check_unread.urllib.request, "urlopen", boom)
    check_unread.main()
    assert capsys.readouterr().out == ""
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# hook.main — the SessionStart digest fetch
# --------------------------------------------------------------------------- #


def test_session_start_digest_fetches_newest_window(monkeypatch, capsys):
    """Regression lock: the digest must request latest=true (the NEWEST rows). It used
    to fetch after_id=0&limit=500 — the OLDEST 500 — so once the line grew a backlog,
    the 24h window rendered empty and every session started blind."""
    monkeypatch.delenv("PARTYLINE_HOOK_FORMAT", raising=False)
    monkeypatch.setattr(hook.sys, "argv", ["hook", "--owner", "alan", "--repo", "partyline"])
    monkeypatch.setattr(hook.cli, "_load_config", lambda: {})
    monkeypatch.setattr(hook.cli, "resolve_url", lambda cli_arg=None, cfg=None: "https://app.example")
    monkeypatch.setattr(hook.cli, "resolve_agent_id", lambda cli_arg=None, cfg=None: "alan_partyline_1")
    monkeypatch.setattr(hook.cli, "resolve_profile", lambda cli_arg=None, url=None, cfg=None: "dev-eastus2")
    monkeypatch.setattr(hook, "_token", lambda profile: "tok")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    msgs = [
        {
            "id": 2950,
            "agent_id": "alan_web_1",
            "message_type": "status",
            "recipient": None,
            "mentions": [],
            "payload": "shipping it",
            "created_at": now,
        },
        # summary-only posts (the token-frugal shape) must not render as blank lines
        {
            "id": 2951,
            "agent_id": "alan_web_1",
            "message_type": "status",
            "recipient": None,
            "mentions": [],
            "payload": None,
            "summary": "summary only",
            "created_at": now,
        },
    ]
    seen = []

    def fake_get(url, token):
        seen.append(url)
        if "/api/agents" in url:
            return []
        if "/api/messages" in url:
            return msgs
        if "/api/claims" in url:
            return []
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(hook, "_get", fake_get)

    hook.main()
    out = capsys.readouterr().out
    msg_url = next(u for u in seen if "/api/messages" in u)
    assert "latest=true" in msg_url
    assert "after_id=0" not in msg_url
    assert "#2950 [status] alan_web_1: shipping it" in out
    assert "#2951 [status] alan_web_1: summary only" in out
