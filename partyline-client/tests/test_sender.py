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
