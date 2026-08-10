import asyncio
from covasant_partyline import sender, outbox


def test_verb_body_builders_shape():
    # post_message body carries event_time; reply maps to /api/messages too
    path, build = sender.VERB_ENDPOINTS["post_message"]
    assert path == "/api/messages"
    body = build({"agent_id": "a", "message_type": "status", "payload": "hi"}, "2026-07-16T00:00:00Z")
    assert body["agent_id"] == "a" and body["event_time"] == "2026-07-16T00:00:00Z"

    path, build = sender.VERB_ENDPOINTS["ack_checkpoint"]
    assert path == "/api/checkpoints"
    body = build({"agent_id": "a", "last_read_message_id": 5}, "2026-07-16T00:00:00Z")
    assert body["last_read_message_id"] == 5


def test_verb_bodies_validate_against_rest_models():
    """Each send verb's body-builder must produce a dict that VALIDATES against the
    corresponding app REST Pydantic model — the uncovered seam where a client-side body
    could silently drift from the server-side model and raise at delivery time."""
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "covasant-partyline"))
    from app.models import PostMessage, RegisterAgent, Checkpoint, ReleaseRequest
    from covasant_partyline import sender

    et = "2026-07-16T00:00:00Z"
    cases = {
        "post_message": (PostMessage, {"agent_id": "a", "message_type": "status", "payload": "hi"}),
        "reply":        (PostMessage, {"agent_id": "a", "message_type": "ack", "payload": "ok"}),
        "register_agent": (RegisterAgent, {"agent_id": "a", "owner": "o", "repo": "r"}),
        "ack_checkpoint": (Checkpoint, {"agent_id": "a", "last_read_message_id": 5}),
        "release_resource": (ReleaseRequest, {"agent_id": "a", "repo": "r", "resource_path": "/t/x"}),
    }
    for verb, (model, args) in cases.items():
        path, build = sender.VERB_ENDPOINTS[verb]
        body = build(args, et)
        model(**body)  # raises pydantic ValidationError if the shape is wrong


def test_sender_marks_sent_on_success(monkeypatch):
    tickets = outbox.TicketTable()

    def fake_send_once(base_url, token_fn, verb, args, event_time):
        return {"id": 99}

    monkeypatch.setattr(sender, "send_once", fake_send_once)
    bs = sender.BackgroundSender("http://x", lambda: "tok", tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        await bs.drain_and_stop(grace=2.0)
        task.cancel()
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "sent"
    assert tickets.get(tk)["message_id"] == 99


def test_sender_spills_after_giveup(monkeypatch):
    tickets = outbox.TicketTable()
    spills = []

    def always_fail(*a, **k):
        raise RuntimeError("app down")

    monkeypatch.setattr(sender, "send_once", always_fail)
    monkeypatch.setattr(outbox, "spool_message", lambda p, r: spills.append((p, r)) or True)
    # shrink timings so the test is fast
    monkeypatch.setattr(sender, "_GIVEUP_SECS", 0.3)
    monkeypatch.setattr(sender, "_BACKOFF_START", 0.05)
    monkeypatch.setattr(sender, "_BACKOFF_CAP", 0.1)
    bs = sender.BackgroundSender("http://x", lambda: "tok", tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        await asyncio.sleep(1.0)
        task.cancel()
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "failed"
    assert spills, "message must be spooled after give-up"
    assert bs.spill_flag["pending"] >= 1


def test_stop_during_backoff_spills_inflight(monkeypatch):
    """Shutdown mid-backoff must spill the in-flight message, not lose it.

    send_once always fails, so _deliver enters its backoff sleep after the first
    attempt. drain_and_stop() sets _stop first (queue is already empty by then),
    which must wake the interruptible backoff wait and force a spill — proving
    the STOP path (not give-up) is what spills.
    """
    tickets = outbox.TicketTable()
    spills = []

    def always_fail(*a, **k):
        raise RuntimeError("app down")

    monkeypatch.setattr(sender, "send_once", always_fail)
    monkeypatch.setattr(outbox, "spool_message", lambda p, r: spills.append((p, r)) or True)
    # Give-up window is large so it can't be what spills; backoff is short so we
    # reliably land inside the sleep before triggering stop.
    monkeypatch.setattr(sender, "_GIVEUP_SECS", 60.0)
    monkeypatch.setattr(sender, "_BACKOFF_START", 0.05)
    monkeypatch.setattr(sender, "_BACKOFF_CAP", 5.0)
    bs = sender.BackgroundSender("http://x", lambda: "tok", tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        # Let the first attempt fail and enter backoff.
        await asyncio.sleep(0.15)
        await bs.drain_and_stop(grace=1.0)
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            raise
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "failed", "ticket must not stay pending forever"
    assert spills, "in-flight message must be spilled, not lost"
    assert bs.spill_flag["pending"] >= 1


def test_token_fn_systemexit_does_not_kill_loop(monkeypatch):
    """A SystemExit from token_fn (e.g. bridge._auth_headers on PAT/unresolvable creds)
    must be treated like any other delivery failure — retried, then spilled on give-up —
    never allowed to escape _deliver/run() and silently kill the drain loop."""
    tickets = outbox.TicketTable()
    spills = []

    def token_fn():
        raise SystemExit("no creds")

    monkeypatch.setattr(outbox, "spool_message", lambda p, r: spills.append((p, r)) or True)
    monkeypatch.setattr(sender, "_GIVEUP_SECS", 0.3)
    monkeypatch.setattr(sender, "_BACKOFF_START", 0.05)
    monkeypatch.setattr(sender, "_BACKOFF_CAP", 0.1)
    bs = sender.BackgroundSender("http://x", token_fn, tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        await asyncio.sleep(1.0)
        assert not task.done(), "run() task must still be alive after a token SystemExit"
        task.cancel()
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "failed"
    assert spills, "message must be spooled after give-up"
    assert bs.spill_flag["pending"] >= 1


def test_drain_token_systemexit_spills(monkeypatch):
    """drain_and_stop's in-flight send attempt must guard token_fn() against SystemExit
    too, mirroring the _deliver hardening: a SystemExit from token_fn (e.g. bridge
    ._auth_headers on PAT/unresolvable creds) during the shutdown-grace window must
    result in a spill, not propagate out of drain_and_stop."""
    tickets = outbox.TicketTable()
    spills = []

    def token_fn():
        raise SystemExit("no creds")

    monkeypatch.setattr(outbox, "spool_message", lambda p, r: spills.append((p, r)) or True)
    bs = sender.BackgroundSender("http://x", token_fn, tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        await bs.drain_and_stop(grace=1.0)
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "failed"
    assert spills, "message must be spooled after a token SystemExit during drain"


def test_cancel_inflight_spills(monkeypatch):
    """Cancelling run() while a delivery is in flight must spill, not lose, the message."""
    tickets = outbox.TicketTable()
    spills = []

    def always_fail(*a, **k):
        raise RuntimeError("app down")

    monkeypatch.setattr(sender, "send_once", always_fail)
    monkeypatch.setattr(outbox, "spool_message", lambda p, r: spills.append((p, r)) or True)
    monkeypatch.setattr(sender, "_GIVEUP_SECS", 60.0)
    monkeypatch.setattr(sender, "_BACKOFF_START", 5.0)
    monkeypatch.setattr(sender, "_BACKOFF_CAP", 5.0)
    bs = sender.BackgroundSender("http://x", lambda: "tok", tickets, spool_path="/tmp/x.yaml")

    async def body():
        tk = bs.enqueue("post_message", {"agent_id": "a", "payload": "hi"}, "2026-07-16T00:00:00Z")
        task = asyncio.ensure_future(bs.run())
        # Let the first attempt fail and settle into the (long) backoff sleep.
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return tk

    tk = asyncio.run(body())
    assert tickets.get(tk)["status"] == "failed", "ticket must not stay pending forever"
    assert spills, "in-flight message must be spilled, not lost"
    assert bs.spill_flag["pending"] >= 1
