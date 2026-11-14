"""`omni-pl` command construction, fork selection, and mail injection."""

import pytest
from covasant_partyline import omni_runner as omni


def which_ok(name: str) -> str | None:
    return {
        "omnigent": "/bin/omnigent",
        "claude-pl": "/bin/claude-pl",
        "codex-pl": "/bin/codex-pl",
        "grok-pl": "/bin/grok-pl",
    }.get(name)


def test_fresh_claude_points_omnigent_at_claude_pl():
    plan = omni.prepare(["claude"], which_ok)
    assert plan.command == ("/bin/omnigent", "claude")
    assert dict(plan.env_updates)["OMNIGENT_CLAUDE_PATH"] == "/bin/claude-pl"
    assert plan.watch is False
    assert plan.hooks is None


def test_fork_uses_the_api_then_resumes_and_passes_extra_args():
    plan = omni.prepare(["claude", "--fork", "conv_abc", "--", "--permission-mode", "bypass"], which_ok)
    assert plan.api_fork is True
    assert plan.fork_agent == "claude-native-ui"
    assert plan.command == ("/bin/omnigent", "claude", "--resume")
    assert plan.extra == ("--permission-mode", "bypass")
    assert plan.fork_id == "conv_abc"
    assert "--fork" not in plan.command


def test_resume_stays_on_the_shortcut():
    plan = omni.prepare(["codex", "--resume", "conv_9"], which_ok)
    assert plan.command == ("/bin/omnigent", "codex", "--resume", "conv_9")
    assert dict(plan.env_updates)["OMNIGENT_CODEX_PATH"] == "/bin/codex-pl"


def test_grok_is_native_and_points_at_grok_pl():
    plan = omni.prepare(["grok"], which_ok)
    assert plan.command == ("/bin/omnigent", "grok")
    env = dict(plan.env_updates)
    assert env["OMNIGENT_GROK_PATH"] == "/bin/grok-pl"
    assert "OMNIGENT_GROK_PATH" in env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"]
    assert plan.hooks == "grok"
    assert plan.watch is False


def test_grok_fork_uses_the_api():
    plan = omni.prepare(["grok", "--fork", "conv_g"], which_ok)
    assert plan.api_fork is True
    assert plan.fork_agent == "grok-native-ui"
    assert plan.command == ("/bin/omnigent", "grok", "--resume")


def test_cmdc_points_omnigent_at_cmdc_pl():
    def which(name: str) -> str | None:
        return {"omnigent": "/bin/omnigent", "cmdc-pl": "/bin/cmdc-pl"}.get(name)

    plan = omni.prepare(["cmdc", "--resume", "conv_c"], which)
    assert plan.command == ("/bin/omnigent", "cmdc", "--resume", "conv_c")
    assert dict(plan.env_updates)["OMNIGENT_COMMANDCODE_PATH"] == "/bin/cmdc-pl"
    assert plan.hooks == "cmdc"
    assert plan.watch is False


def test_cline_watches_the_session_and_pins_no_binary():
    plan = omni.prepare(["cline"], which_ok)
    assert plan.command == ("/bin/omnigent", "cline")
    assert plan.env_updates == ()
    assert plan.warnings == ()
    assert plan.hooks is None
    assert plan.watch is True
    fork = omni.prepare(["cline", "--fork", "conv_x"], which_ok)
    assert fork.api_fork is True
    assert fork.fork_agent == "cline-native-ui"


def test_missing_pl_binary_warns_and_still_launches():
    plan = omni.prepare(["claude"], lambda name: "/bin/omnigent" if name == "omnigent" else None)
    assert plan.env_updates == ()
    assert plan.command[0] == "/bin/omnigent"
    assert any("claude-pl" in warning for warning in plan.warnings)


def test_fork_and_resume_together_are_rejected():
    with pytest.raises(SystemExit, match="only one"):
        omni.prepare(["claude", "--fork", "a", "--resume", "b"], which_ok)


def no_probe(omni: str, name: str) -> tuple[int, str]:
    raise AssertionError(f"probed {name}; names in the alias layer must not shell out")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "omni-harnesses.json"
    monkeypatch.setattr(omni, "_cache_path", lambda: path)
    return path


@pytest.mark.parametrize("name", ["cmdc", "cmd", "commandcode", "cline", "grok", "agy", "kimi"])
def test_alias_layer_names_reach_omnigent_without_a_probe(name, cache):
    plan = omni.prepare([name, "--yolo"], which_ok, no_probe)
    assert plan.command == ("/bin/omnigent", name, "--yolo")


@pytest.mark.parametrize(
    "name", ["cline-native", "native-cline", "commandcode-native", "grok-native", "pi-native", "opencode-native"]
)
def test_native_ids_pass_through_to_run_harness(name, cache):
    plan = omni.prepare([name, "--yolo"], which_ok, no_probe)
    assert plan.command == ("/bin/omnigent", "run", "--harness", name, "--yolo")


def test_native_spelling_keeps_the_partyline_attachment(cache):
    def which(name: str) -> str | None:
        return {"omnigent": "/bin/omnigent", "cmdc-pl": "/bin/cmdc-pl"}.get(name)

    plan = omni.prepare(["commandcode-native"], which, no_probe)
    assert dict(plan.env_updates)["OMNIGENT_COMMANDCODE_PATH"] == "/bin/cmdc-pl"
    assert plan.hooks == "cmdc"
    cline = omni.prepare(["native-cline"], which_ok, no_probe)
    assert cline.watch is True and cline.env_updates == ()


def test_a_harness_partyline_never_heard_of_launches_with_the_watcher(cache):
    asked = []

    def probe(o, name):
        asked.append(name)
        return 0, ""

    plan = omni.prepare(["goose", "--yolo"], which_ok, probe)
    assert plan.command == ("/bin/omnigent", "goose", "--yolo")
    assert plan.watch is True and plan.env_updates == () and plan.hooks is None
    assert asked == ["goose"]
    # Cached per Omnigent install: the second launch does not shell out.
    omni.prepare(["goose"], which_ok, no_probe)


def test_a_typo_fails_with_omnigents_own_message(cache, capsys):
    message = "Error: No such command 'clien'. (Did you mean one of: 'cline', 'login'?)\n"
    with pytest.raises(SystemExit) as exc:
        omni.prepare(["clien", "--yolo"], which_ok, lambda o, name: (2, message))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err == message
    assert "Known:" not in err and "unknown harness" not in err
    assert not cache.exists()


def test_an_unreachable_probe_passes_through(cache):
    plan = omni.prepare(["hermes"], which_ok, lambda o, name: (-1, "timed out"))
    assert plan.command == ("/bin/omnigent", "hermes")


def test_cache_is_dropped_when_omnigent_changes(cache):
    cache.write_text('{"stamp": "some-older-install", "names": ["clien"]}')
    with pytest.raises(SystemExit):
        omni.prepare(["clien"], which_ok, lambda o, name: (2, "Error: No such command 'clien'.\n"))


def test_fork_agent_follows_omnigents_naming():
    assert omni.fork_agent_for("goose") == "goose-native-ui"
    assert omni.fork_agent_for("native-pi") == "pi-native-ui"
    assert omni.fork_agent_for("agy-native") == "antigravity-native-ui"
    assert omni.fork_agent_for("cmd") == "commandcode-native-ui"


def test_native_fork_resumes_through_run_harness(cache):
    plan = omni.prepare(["cline-native", "--fork", "conv_x"], which_ok, no_probe)
    assert plan.api_fork and plan.fork_agent == "cline-native-ui"
    assert plan.command == ("/bin/omnigent", "run", "--harness", "cline-native", "--resume")


def test_a_flag_in_place_of_the_harness_is_rejected():
    with pytest.raises(SystemExit, match="harness name comes first"):
        omni.prepare(["--fork", "conv_x"], which_ok, no_probe)


def test_launch_source_has_no_frozen_harness_gate():
    import inspect

    source = inspect.getsource(omni.prepare) + inspect.getsource(omni._parse)
    assert "not in ATTACH" not in source and "Known:" not in source


def test_missing_omnigent_is_rejected():
    with pytest.raises(SystemExit, match="omnigent"):
        omni.prepare(["grok"], lambda name: None)


def test_wsl_finds_omnigent_exe(monkeypatch):
    monkeypatch.setattr(omni.os, "name", "posix")

    def which(name: str) -> str | None:
        return "/mnt/c/omnigent.exe" if name == "omnigent.exe" else None

    plan = omni.prepare(["grok"], which)
    assert plan.command[0] == "/mnt/c/omnigent.exe"


def test_cursor_forwards_the_path_the_runner_would_drop():
    def which(name: str) -> str | None:
        return {"omnigent": "/bin/omnigent", "cursor-agent-pl": "/bin/cursor-agent-pl"}.get(name)

    plan = omni.prepare(["cursor"], which)
    env = dict(plan.env_updates)
    assert env["OMNIGENT_CURSOR_PATH"] == "/bin/cursor-agent-pl"
    assert "OMNIGENT_CURSOR_PATH" in env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"]
    assert any("runner drops" in warning for warning in plan.warnings)


def test_choose_session_skips_the_fork_source_and_old_sessions():
    sessions = [
        {"id": "conv_old", "created_at": 10},
        {"id": "conv_source", "created_at": 100},
        {"id": "conv_new", "created_at": 200},
    ]
    assert omni.choose_session(sessions, started=150, resume_id=None, fork_id="conv_source") == "conv_new"
    assert omni.choose_session(sessions, started=150, resume_id="conv_source", fork_id=None) == "conv_source"
    assert omni.choose_session(sessions, started=500, resume_id=None, fork_id=None) is None
    assert (
        omni.choose_session(
            sessions, resume_id=None, fork_id="conv_source", known_ids={"conv_old", "conv_source"}
        )
        == "conv_new"
    )


def test_old_unread_is_not_new_mail():
    assert omni.message_is_new({"event_time": "2020-01-01T00:00:00Z"}, started=1_700_000_000) is False
    assert omni.message_is_new({"event_time": "not-a-date"}, started=0) is False
    fresh = "2026-11-11T21:00:02+00:00"
    started = omni.datetime.fromisoformat(fresh).timestamp()
    assert omni.message_is_new({"event_time": fresh}, started=started) is True


def test_fork_session_binds_the_named_agent(monkeypatch):
    def fake(method, url, body=None):
        if method == "GET":
            return 200, {"data": [{"name": "claude-native-ui", "id": "ag_claude"}]}
        assert method == "POST"
        assert url.endswith("/v1/sessions/conv_src/fork")
        assert body == {"agent_id": "ag_claude"}
        return 201, {"id": "conv_new"}

    monkeypatch.setattr(omni, "_request_json", fake)
    assert omni.fork_session("http://127.0.0.1:6767", "conv_src", "claude-native-ui") == "conv_new"


def test_fork_session_rejects_a_missing_agent(monkeypatch):
    monkeypatch.setattr(omni, "_request_json", lambda method, url, body=None: (200, {"data": []}))
    with pytest.raises(SystemExit, match="no agent"):
        omni.fork_session("http://127.0.0.1:6767", "conv_src", "claude-native-ui")


def test_mail_becomes_an_omnigent_user_message():
    text = omni.mail_text(
        {"id": 7, "message_type": "status", "agent_id": "alan_build_1", "summary": "deploy done", "payload": None}
    )
    event = omni.omnigent_event(text)
    assert event["type"] == "message"
    assert event["data"]["content"][0]["text"].startswith("Partyline #7")
    assert "deploy done" in event["data"]["content"][0]["text"]


def test_launch_posts_the_fork_and_runs_omnigent(monkeypatch):
    posted = {}
    monkeypatch.setattr(omni, "register_self", lambda harness: "alan_partyline_1")
    monkeypatch.setitem(omni.HARNESSES, "agy", (("agy",), lambda: posted.setdefault("hooks", True)))

    def fake_post(harness, conv_id):
        posted["notice"] = (harness, conv_id)

    monkeypatch.setattr(omni, "post_fork_notice", fake_post)
    monkeypatch.setattr(omni, "fork_session", lambda base, source, agent: "conv_new")

    class Result:
        returncode = 0

    ran = {}

    def fake_run(cmd, env, check=False):
        ran["cmd"] = cmd
        ran["env"] = env
        return Result()

    monkeypatch.setattr(omni.subprocess, "run", fake_run)
    monkeypatch.setattr(
        omni.shutil,
        "which",
        lambda name: {"omnigent": "/bin/omnigent", "agy-pl": "/bin/agy-pl"}.get(name),
    )
    assert omni.launch(["agy", "--fork", "conv_z"]) == 0
    assert posted["notice"] == ("agy", "conv_z")
    assert ran["cmd"] == ("/bin/omnigent", "agy", "--resume", "conv_new")
    assert ran["env"]["OMNIGENT_ANTIGRAVITY_PATH"] == "/bin/agy-pl"


def test_cline_fork_watches_the_forked_session(monkeypatch):
    """The API fork creates the session before the watcher snapshots the list,
    so the watcher must be handed the new id rather than discover it."""
    monkeypatch.setattr(omni, "register_self", lambda harness: "alan_partyline_1")
    monkeypatch.setattr(omni, "post_fork_notice", lambda harness, conv_id: None)
    monkeypatch.setattr(omni, "fork_session", lambda base, source, agent: "conv_new")
    monkeypatch.setattr(omni.cli, "_load_config", dict)
    monkeypatch.setattr(omni.cli, "resolve_agent_id", lambda cfg=None: "alan_partyline_1")
    monkeypatch.setattr(omni.cli, "resolve_url", lambda cfg=None: "http://line")
    monkeypatch.setattr(omni, "prime_delivered", lambda agent_id, url: set())
    monkeypatch.setattr(omni, "snapshot_session_ids", lambda base: {"conv_x", "conv_new"})
    watched = {}

    class FakeThread:
        def __init__(self, target, args, daemon):
            watched["plan"] = args[1]

        def start(self):
            pass

    monkeypatch.setattr(omni.threading, "Thread", FakeThread)

    class Result:
        returncode = 0

    monkeypatch.setattr(omni.subprocess, "run", lambda cmd, env, check=False: Result())
    monkeypatch.setattr(omni.shutil, "which", lambda name: "/bin/omnigent" if name == "omnigent" else None)
    assert omni.launch(["cline", "--fork", "conv_x"]) == 0
    assert watched["plan"].resume_id == "conv_new"


def test_fork_failure_surfaces_omnigents_error(monkeypatch):
    detail = "invalid agent spec synthesized from omnigent YAML: must be one of [...], got 'cline-native'"

    def fake(method, url, body=None):
        if method == "GET":
            return 200, {"data": [{"name": "cline-native-ui", "id": "ag_cline"}]}
        return 400, {"detail": detail}

    monkeypatch.setattr(omni, "_request_json", fake)
    with pytest.raises(SystemExit) as exc:
        omni.fork_session("http://127.0.0.1:6767", "conv_src", "cline-native-ui")
    assert "HTTP 400" in str(exc.value) and detail in str(exc.value)


def test_unreachable_omnigent_is_named(monkeypatch):
    monkeypatch.setattr(omni, "_request_json", lambda method, url, body=None: (0, None))
    with pytest.raises(SystemExit, match="not reachable at http://127.0.0.1:6767"):
        omni.fork_session("http://127.0.0.1:6767", "conv_src", "claude-native-ui")


def test_http_error_body_is_kept(monkeypatch):
    import io
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"detail": "got \'cline-native\'"}'))

    monkeypatch.setattr(omni.urllib.request, "urlopen", boom)
    assert omni._request_json("POST", "http://x/v1/sessions/a/fork", {}) == (400, {"detail": "got 'cline-native'"})
