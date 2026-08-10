"""Send-time delivery feedback (needs local Postgres: docker compose up -d).

The bug these lock down: a partyline post is a durable write with an *unobservable*
delivery. Posting to a recipient with no listener returned 200, so the sender learned
nothing and read the ensuing silence as agreement. Reporting at write time is the fix —
an unknown recipient is refused outright, a cold one comes back with a warning.
"""

import uuid

import pytest

from app import db

pytestmark = pytest.mark.anyio


async def _register(client, agent_id):
    r = await client.post("/api/agents", json={
        "agent_id": agent_id, "owner": "test", "repo": "partyline",
    })
    assert r.status_code == 200


async def _post(client, sender, **fields):
    body = {"agent_id": sender, "message_type": "question"}
    body.update(fields)
    return await client.post("/api/messages", json=body)


def _age_heartbeat(agent_id: str, seconds: int) -> None:
    """Backdate an agent's clocks so staleness is testable without waiting for it."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agents SET last_heartbeat = now() - make_interval(secs => %s) "
            "WHERE agent_id = %s",
            (seconds, agent_id),
        )
        conn.execute(
            "UPDATE checkpoints SET updated_at = now() - make_interval(secs => %s) "
            "WHERE agent_id = %s",
            (seconds, agent_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Unknown recipient — a typo, refused
# --------------------------------------------------------------------------- #


async def test_unknown_recipient_is_refused_with_a_400(client):
    await _register(client, "dlv_sender")
    r = await _post(client, "dlv_sender", recipient="dlv_never_existed", payload="handoff")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "unknown_recipient"
    assert detail["recipient"] == "dlv_never_existed"


async def test_refusal_suggests_the_id_the_sender_probably_meant(client):
    await _register(client, "dlv_sender")
    await _register(client, "alan_aura_2")
    r = await _post(client, "dlv_sender", recipient="alan_aura2", payload="typo")
    assert r.status_code == 400
    assert "alan_aura_2" in r.json()["detail"]["suggestions"]


async def test_refused_message_is_not_written(client):
    """A rejected post must leave no row — a half-delivered handoff is worse than none."""
    await _register(client, "dlv_sender")
    marker = "dlvmark" + uuid.uuid4().hex[:8]
    r = await _post(client, "dlv_sender", recipient="dlv_ghost", payload=marker)
    assert r.status_code == 400
    assert db.query_messages(q=marker) == []


async def test_an_idle_recipient_is_not_refused(client):
    """Idle is a timing problem, not a typo: the message waits and is eventually read."""
    await _register(client, "dlv_sender")
    await _register(client, "dlv_idle")
    _age_heartbeat("dlv_idle", 68400)  # 19h, the incident's number
    r = await _post(client, "dlv_sender", recipient="dlv_idle", payload="still deliverable")
    assert r.status_code == 200


async def test_override_allows_addressing_a_session_that_has_not_started(client):
    await _register(client, "dlv_sender")
    r = await _post(
        client, "dlv_sender",
        recipient="dlv_future_session",
        payload="pre-registration handoff",
        allow_unregistered_recipient=True,
    )
    assert r.status_code == 200
    assert any("not registered" in w for w in r.json()["delivery_warnings"])


# --------------------------------------------------------------------------- #
# Cold recipient — a timing problem, warned about
# --------------------------------------------------------------------------- #


async def test_stale_recipient_warns_the_sender_at_write_time(client):
    await _register(client, "dlv_sender")
    await _register(client, "dlv_stale")
    _age_heartbeat("dlv_stale", 68400)

    body = (await _post(client, "dlv_sender", recipient="dlv_stale", payload="hello?")).json()

    assert body["recipient_presence"]["status"] == "stale"
    assert body["recipient_presence"]["heartbeat_age_seconds"] >= 68000
    warning = " ".join(body["delivery_warnings"])
    assert "dlv_stale" in warning
    # The write IS durable; a warning implying loss would push senders into pointless resends.
    assert "stored" in warning
    assert "silence" in warning


async def test_live_recipient_that_never_reads_still_warns(client):
    """The signal the roster could never give: present, and not listening.

    'Recently ran a command' is what the heartbeat means — any call refreshes it. Only the
    checkpoint says 'consumed the line'. An agent posting busily while never reading is
    exactly the case where a sender is owed a warning.
    """
    await _register(client, "dlv_sender")
    await _register(client, "dlv_deaf")  # registered, never acked a checkpoint

    body = (await _post(client, "dlv_sender", recipient="dlv_deaf", payload="hi")).json()

    assert body["recipient_presence"]["status"] == "live"
    assert body["recipient_presence"]["reading"] is False
    assert body["recipient_presence"]["read_age_seconds"] is None
    assert body["delivery_warnings"]


async def test_live_reading_recipient_produces_no_noise(client):
    await _register(client, "dlv_sender")
    await _register(client, "dlv_attentive")
    await client.post("/api/checkpoints", json={
        "agent_id": "dlv_attentive", "last_read_message_id": 1,
    })

    body = (await _post(client, "dlv_sender", recipient="dlv_attentive", payload="hi")).json()

    assert body["recipient_presence"]["status"] == "live"
    assert body["recipient_presence"]["reading"] is True
    assert body["delivery_warnings"] == []


async def test_unregistered_mentions_warn_but_never_refuse(client):
    """Mentions are parsed out of prose ('@name'), so a false positive must not block a post."""
    await _register(client, "dlv_sender")
    await _register(client, "dlv_attentive")
    await client.post("/api/checkpoints", json={
        "agent_id": "dlv_attentive", "last_read_message_id": 1,
    })

    r = await _post(
        client, "dlv_sender",
        recipient="dlv_attentive",
        mentions=["dlv_attentive", "dlv_not_a_real_agent"],
        payload="hi",
    )

    assert r.status_code == 200
    warning = " ".join(r.json()["delivery_warnings"])
    assert "dlv_not_a_real_agent" in warning
    assert "dlv_attentive" not in warning


async def test_unaddressed_broadcast_needs_no_feedback(client):
    await _register(client, "dlv_sender")
    body = (await _post(client, "dlv_sender", message_type="status", payload="working")).json()
    assert body["recipient_presence"] is None
    assert body["delivery_warnings"] == []
