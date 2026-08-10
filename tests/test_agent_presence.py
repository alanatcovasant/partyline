"""Roster presence over REST (needs local Postgres: docker compose up -d).

The invariant: liveness decays, identity never does. An agent that has gone quiet must
stay findable and keep its read cursor — otherwise a returning agent re-reads its whole
backlog, and a sender cannot tell "not listening" from "no such agent".
"""

import pytest

from app import db

pytestmark = pytest.mark.anyio


async def _register(client, agent_id):
    r = await client.post("/api/agents", json={
        "agent_id": agent_id, "owner": "test", "repo": "partyline",
    })
    assert r.status_code == 200


def _age(agent_id: str, seconds: int) -> None:
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agents SET last_heartbeat = now() - make_interval(secs => %s) "
            "WHERE agent_id = %s",
            (seconds, agent_id),
        )
        conn.commit()


def _find(rows, agent_id):
    return next((r for r in rows if r["agent_id"] == agent_id), None)


async def test_every_row_carries_presence_labels(client):
    await _register(client, "pres_live")
    rows = (await client.get("/api/agents")).json()
    row = _find(rows, "pres_live")
    assert row["status"] == "live"
    assert row["heartbeat_age_seconds"] is not None
    assert "reading" in row and "read_age_seconds" in row


async def test_a_quiet_agent_is_labelled_not_deleted(client):
    await _register(client, "pres_quiet")
    _age("pres_quiet", 68400)  # 19h

    rows = (await client.get("/api/agents?include_stale=true")).json()
    row = _find(rows, "pres_quiet")

    assert row is not None, "the row must survive idleness — nothing deletes an agent"
    assert row["status"] == "stale"
    assert row["heartbeat_age_seconds"] >= 68000


async def test_the_default_window_still_trims_the_roster(client):
    """The SessionStart banner is a token budget; callers who want the truth ask for it."""
    await _register(client, "pres_quiet2")
    _age("pres_quiet2", 68400)
    assert _find((await client.get("/api/agents")).json(), "pres_quiet2") is None
    assert _find((await client.get("/api/agents?include_stale=true")).json(), "pres_quiet2")


async def test_read_cursor_survives_going_stale(client):
    """The documented failure mode: a returning agent must not re-read its whole backlog."""
    await _register(client, "pres_returning")
    await client.post("/api/checkpoints", json={
        "agent_id": "pres_returning", "last_read_message_id": 4242,
    })
    _age("pres_returning", 864000)  # 10 days quiet

    await _register(client, "pres_returning")  # comes back

    assert db.get_checkpoint("pres_returning") == 4242


async def test_idle_sits_between_live_and_stale(client):
    await _register(client, "pres_idle")
    _age("pres_idle", 1800)  # 30m: past live (5m), inside idle (1h)
    rows = (await client.get("/api/agents?include_stale=true")).json()
    assert _find(rows, "pres_idle")["status"] == "idle"


async def test_diagnostics_shares_one_definition_of_presence(client):
    """Two endpoints disagreeing about who is live is how the original confusion started."""
    await _register(client, "pres_diag")
    _age("pres_diag", 68400)

    roster = _find((await client.get("/api/agents?include_stale=true")).json(), "pres_diag")
    diag = (await client.get("/api/diagnostics/agents?sample_size=500")).json()
    sampled = _find(diag["sampled_agents"], "pres_diag")

    assert sampled["status"] == roster["status"] == "stale"
