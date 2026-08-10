"""DB-layer coverage for db.query_messages (the shared field-filtered query).

Seeds messages through the REST client fixture (which brings up a live local Postgres
and the connection pool), then asserts directly against db.query_messages. Skips cleanly
when no local Postgres is listening (see tests/conftest.py).
"""

import uuid

import pytest

from app import db


async def _post(client, agent_id, **fields):
    async def _register(who):
        await client.post("/api/agents", json={"agent_id": who, "owner": "alan", "repo": "partyline"})

    await _register(agent_id)
    # Register the addressee too: posting to an id that never registered is now a 400
    # (it is a typo, and a typo'd handoff is undeliverable forever). These tests are about
    # the addressing FILTER, so they need a recipient that legitimately exists.
    if fields.get("recipient"):
        await _register(fields["recipient"])
    body = {"agent_id": agent_id, "message_type": fields.pop("message_type", "status")}
    body.update(fields)
    return (await client.post("/api/messages", json=body)).json()


@pytest.mark.anyio
async def test_query_by_issue_ref(client):
    tag = uuid.uuid4().hex[:8]
    posted = await _post(client, "qm_issue", issue_ref=tag, payload="issue-tagged")
    rows = db.query_messages(issue_ref=tag)
    assert [r["id"] for r in rows] == [posted["id"]]
    assert rows[0]["source"] == "live"


@pytest.mark.anyio
async def test_query_by_conversation_id(client):
    posted = await _post(client, "qm_conv", payload="conv-root")
    rows = db.query_messages(conversation_id=posted["conversation_id"])
    assert posted["id"] in [r["id"] for r in rows]


@pytest.mark.anyio
async def test_query_q_matches_summary_only(client):
    """New behavior: full-text q now spans summary as well as payload."""
    token = "smrytok" + uuid.uuid4().hex[:6]
    posted = await _post(client, "qm_summary", summary=f"marker {token}", payload="unrelated body")
    rows = db.query_messages(q=token)
    assert posted["id"] in [r["id"] for r in rows]


@pytest.mark.anyio
async def test_query_mention_array_contains(client):
    posted = await _post(client, "qm_mention", payload="hi", mentions=["target_x"])
    rows = db.query_messages(mention="target_x")
    assert posted["id"] in [r["id"] for r in rows]


@pytest.mark.anyio
async def test_query_two_filters_anded(client):
    tag = uuid.uuid4().hex[:8]
    match = await _post(client, "qm_and", message_type="done", repo=tag, payload="a")
    await _post(client, "qm_and", message_type="status", repo=tag, payload="b")
    rows = db.query_messages(repo=tag, message_type="done")
    assert [r["id"] for r in rows] == [match["id"]]


@pytest.mark.anyio
async def test_get_since_for_agent_matches_mention_when_recipient_is_someone_else(client):
    """Regression lock on the filter the unread *peek* actually uses.

    The peek delegates addressing to the server (`only_for_me=true` → get_since(for_agent=...)),
    so this SQL predicate — not channel.addressed_to_me — is what decides whether queued mail
    reaches a session. From the live specimen: alan_aura_0's 6 unread were 6/6 via `mentions`,
    several with `recipient` set to another agent. Narrowing this to `recipient = %s` would
    make every one of them invisible while the peek still looked healthy.
    """
    me = "qm_addr_" + uuid.uuid4().hex[:8]
    posted = await _post(client, "qm_sender", payload="hi", recipient="qm_someone_else",
                         mentions=[me])

    mine = db.get_since(after_id=0, for_agent=me)

    assert posted["id"] in [r["id"] for r in mine]


@pytest.mark.anyio
async def test_get_since_for_agent_excludes_messages_addressing_nobody_relevant(client):
    """The other half of the invariant: `for_agent` must still filter, not pass everything."""
    bystander = "qm_bystander_" + uuid.uuid4().hex[:8]
    posted = await _post(client, "qm_sender", payload="hi", recipient="qm_someone_else",
                         mentions=["qm_third_party"])

    theirs = db.get_since(after_id=0, for_agent=bystander)

    assert posted["id"] not in [r["id"] for r in theirs]


@pytest.mark.anyio
async def test_query_no_filters_returns_newest_within_limit(client):
    ids = [(await _post(client, "qm_recent", payload=f"n{i}"))["id"] for i in range(3)]
    rows = db.query_messages(limit=2)
    assert len(rows) == 2
    assert rows == sorted(rows, key=lambda r: r["id"], reverse=True)  # newest-first
    assert rows[0]["id"] == max(ids)


@pytest.mark.anyio
async def test_query_limit_respected(client):
    for i in range(4):
        await _post(client, "qm_limit", repo="qm_limit_repo", payload=f"m{i}")
    rows = db.query_messages(repo="qm_limit_repo", limit=2)
    assert len(rows) == 2


@pytest.mark.anyio
async def test_mcp_search_messages_accepts_filters(client):
    """The MCP tool delegates to query_messages and accepts structured filters."""
    from app import mcp_server

    tag = uuid.uuid4().hex[:8]
    posted = await _post(client, "mcp_filter_agent", issue_ref=tag, payload="mcp-filtered")
    rows = await mcp_server.search_messages(issue_ref=tag)
    assert posted["id"] in [r["id"] for r in rows]
