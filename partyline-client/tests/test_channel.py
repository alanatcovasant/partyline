"""Unit tests for the partyline → Claude Code channel (no live network / no live Claude).

Lock in the contract: only addressed messages are pushed, as a
`notifications/claude/channel` event with identifier-safe meta; the reply tool posts the
right body; the server declares the claude/channel capability. Sync tests drive
asyncio.run so they don't depend on pytest-asyncio config.
"""
import asyncio
import json as _json

import anyio

from covasant_partyline import channel


def test_addressed_to_me_recipient():
    assert channel.addressed_to_me({"agent_id": "x", "recipient": "me", "mentions": []}, "me") is True


def test_addressed_to_me_mention():
    assert channel.addressed_to_me({"agent_id": "x", "recipient": None, "mentions": ["me"]}, "me") is True


def test_not_addressed_broadcast():
    assert channel.addressed_to_me({"agent_id": "x", "recipient": None, "mentions": []}, "me") is False


def test_skip_own_message():
    assert channel.addressed_to_me({"agent_id": "me", "recipient": "me", "mentions": ["me"]}, "me") is False


def test_addressed_to_me_mention_only_when_recipient_is_someone_else():
    """Regression lock, from a live specimen: alan_aura_0 had 6 unread, 6/6 via mentions,
    three of them with `recipient` pointing at a *different* agent. A predicate that
    narrowed to `recipient == me` would surface 0 of 6 — totally deaf, not half deaf.
    """
    msg = {"agent_id": "sender", "recipient": "other_agent", "mentions": ["me"]}
    assert channel.addressed_to_me(msg, "me") is True


def test_channel_params_basic():
    msg = {"id": 344, "agent_id": "alan_web_1", "message_type": "question",
           "repo": "web", "branch": "develop", "payload": "review please"}
    p = channel.channel_params(msg)
    assert p["content"] == "review please"
    assert p["meta"] == {"msg_id": "344", "from_agent": "alan_web_1",
                         "type": "question", "repo": "web", "branch": "develop"}


def test_channel_params_omits_empty_and_stringifies():
    p = channel.channel_params({"id": 5, "agent_id": "x", "message_type": "status",
                                "repo": None, "branch": None, "payload": None})
    assert p["content"] == ""
    assert p["meta"] == {"msg_id": "5", "from_agent": "x", "type": "status"}
    assert all(isinstance(v, str) for v in p["meta"].values())
    assert all(k.replace("_", "").isalnum() for k in p["meta"])


def test_push_event_writes_channel_notification():
    async def body():
        send, recv = anyio.create_memory_object_stream(1)
        await channel.push_event(send, {"content": "hi", "meta": {"msg_id": "1"}})
        session_msg = recv.receive_nowait()
        notif = session_msg.message.root
        assert notif.method == channel.CHANNEL_METHOD == "notifications/claude/channel"
        assert notif.params == {"content": "hi", "meta": {"msg_id": "1"}}
    # asyncio.run, NOT anyio.run — matching this file's docstring and test_bridge.py.
    # anyio.run() asks sniffio whether we're already in an async context, and sniffio reads
    # a ContextVar first. A session-scoped anyio fixture in tests/conftest.py leaves
    # `current_async_library_cvar` set to "asyncio" for the rest of the pytest session, so
    # anyio.run() raised "Already running asyncio in this thread" with no loop running at
    # all. asyncio.run() checks for a genuinely running loop, so it is immune.
    asyncio.run(body())


def test_post_reply_builds_expected_request(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": 999}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = _json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(channel.urllib.request, "urlopen", fake_urlopen)
    out = channel.post_reply("https://app.example/", "tok", "me", "ack", "done",
                             recipient="alan_web_1", reply_to_id=344)
    assert out == {"id": 999}
    assert captured["url"] == "https://app.example/api/messages"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer tok"
    assert captured["body"] == {"agent_id": "me", "message_type": "ack", "payload": "done",
                                "recipient": "alan_web_1", "reply_to_id": 344}


def test_capability_and_reply_tool_declared():
    assert channel.CHANNEL_CAPABILITY == {"claude/channel": {}}
    assert channel.REPLY_TOOL.name == "reply"
    props = channel.REPLY_TOOL.inputSchema["properties"]
    assert {"message_type", "payload"}.issubset(props)
    assert channel.REPLY_TOOL.inputSchema["required"] == ["message_type", "payload"]


def test_build_server_sets_experimental_channel_capability():
    server, opts = channel.build_server(channel.Settings(agent_id="me", url="http://x", token=None))
    assert opts.capabilities.experimental == {"claude/channel": {}}


def test_iter_data_events_parses_data_lines_only():
    lines = [
        "event: hello",
        "data: {}",
        ": keep-alive",
        'data: {"id": 1, "agent_id": "x", "payload": "hi"}',
        "",
        "data: not-json",
    ]
    assert list(channel.iter_data_events(lines)) == [{}, {"id": 1, "agent_id": "x", "payload": "hi"}]


def test_resolve_settings_prefers_env(monkeypatch):
    monkeypatch.setenv("PARTYLINE_AGENT_ID", "me")
    monkeypatch.setenv("PARTYLINE_URL", "http://envhost")
    monkeypatch.setenv("PARTYLINE_TOKEN", "envtok")
    s = channel.resolve_settings([])
    assert (s.agent_id, s.url, s.token) == ("me", "http://envhost", "envtok")
