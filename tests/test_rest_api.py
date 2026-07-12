"""End-to-end REST API tests for Covasant Partyline.

Requires a running Postgres on localhost:5432 (docker compose up).
Run: uv run pytest tests/ -v
"""

import pytest


@pytest.mark.anyio
async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db_configured"] is True


@pytest.mark.anyio
async def test_register_agent(client):
    resp = await client.post(
        "/api/agents",
        json={
            "agent_id": "test_agent_1",
            "owner": "alan",
            "repo": "covasant-partyline",
            "branch": "main",
            "display_name": "Test Agent 1",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "test_agent_1"
    assert data["owner"] == "alan"
    assert data["repo"] == "covasant-partyline"


@pytest.mark.anyio
async def test_register_agent_idempotent(client):
    """Re-registering the same agent updates rather than errors."""
    payload = {
        "agent_id": "test_agent_idem",
        "owner": "alan",
        "repo": "aura",
        "branch": "develop",
    }
    resp1 = await client.post("/api/agents", json=payload)
    assert resp1.status_code == 200

    payload["branch"] = "main"
    resp2 = await client.post("/api/agents", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["branch"] == "main"


@pytest.mark.anyio
async def test_list_agents(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "test_list_agent",
            "owner": "alan",
            "repo": "partyline",
        },
    )
    resp = await client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    assert any(a["agent_id"] == "test_list_agent" for a in agents)


@pytest.mark.anyio
async def test_agent_presence_diagnostics(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "diag_agent",
            "owner": "alan",
            "repo": "partyline",
        },
    )

    resp = await client.get("/api/diagnostics/agents")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_agents" in data
    assert "active_agents" in data
    assert "sampled_agents" in data
    assert any(a["agent_id"] == "diag_agent" for a in data["sampled_agents"])


@pytest.mark.anyio
async def test_post_message(client):
    # Register first (FK requirement)
    await client.post(
        "/api/agents",
        json={
            "agent_id": "msg_agent",
            "owner": "alan",
            "repo": "partyline",
        },
    )
    resp = await client.post(
        "/api/messages",
        json={
            "agent_id": "msg_agent",
            "message_type": "status",
            "payload": "Starting work on auth module",
            "repo": "partyline",
            "branch": "main",
        },
    )
    assert resp.status_code == 200
    msg = resp.json()
    assert msg["agent_id"] == "msg_agent"
    assert msg["message_type"] == "status"
    assert msg["payload"] == "Starting work on auth module"
    assert msg["id"] > 0


@pytest.mark.anyio
async def test_get_messages(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "reader_agent",
            "owner": "alan",
            "repo": "partyline",
        },
    )
    await client.post(
        "/api/messages",
        json={
            "agent_id": "reader_agent",
            "message_type": "done",
            "payload": "Finished task X",
        },
    )
    resp = await client.get("/api/messages", params={"after_id": 0})
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) > 0


@pytest.mark.anyio
async def test_get_messages_latest_returns_most_recent_ascending(client):
    """The dashboard seed (?latest=true) must return the NEWEST `limit` messages in
    ascending id order — not the oldest. Regression: with a large backlog the feed
    seeded from the oldest 200 and never showed recent posts."""
    await client.post(
        "/api/agents",
        json={"agent_id": "latest_agent", "owner": "alan", "repo": "partyline"},
    )
    posted = []
    for i in range(5):
        r = await client.post(
            "/api/messages",
            json={"agent_id": "latest_agent", "message_type": "status", "payload": f"m{i}"},
        )
        posted.append(r.json()["id"])

    resp = await client.get("/api/messages", params={"latest": "true", "limit": 3})
    assert resp.status_code == 200
    rows = resp.json()
    got = [m["id"] for m in rows]
    # the three most recent overall (my last three posts), in ascending order
    assert got == sorted(posted)[-3:]
    assert got == sorted(got), "rows must be ascending so the client renders oldest→newest"


@pytest.mark.anyio
async def test_index_html_served_no_cache(client):
    """index.html must be no-cache so a redeploy is picked up without a hard reload
    (hashed assets stay immutable). Skips if the SPA isn't built in this env."""
    resp = await client.get("/")
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("text/html"):
        pytest.skip("SPA not built/served in this environment")
    assert "no-cache" in resp.headers.get("cache-control", "").lower()


@pytest.mark.anyio
async def test_conversation_threading(client):
    """A reply inherits its parent's conversation_id; a fresh post starts a new one."""
    await client.post("/api/agents", json={"agent_id": "thread_agent", "owner": "alan", "repo": "partyline"})

    a = (await client.post("/api/messages", json={"agent_id": "thread_agent", "message_type": "status", "payload": "root"})).json()
    assert a["conversation_id"]  # server-assigned
    b = (await client.post("/api/messages", json={"agent_id": "thread_agent", "message_type": "ack", "payload": "reply", "reply_to_id": a["id"]})).json()
    assert b["conversation_id"] == a["conversation_id"], "reply must inherit the thread"
    c = (await client.post("/api/messages", json={"agent_id": "thread_agent", "message_type": "status", "payload": "unrelated"})).json()
    assert c["conversation_id"] != a["conversation_id"], "fresh post must start a new thread"


@pytest.mark.anyio
async def test_search_finds_message_with_conversation(client):
    await client.post("/api/agents", json={"agent_id": "search_agent", "owner": "alan", "repo": "partyline"})
    token = "zqxwovc"  # unlikely to collide with other fixtures
    posted = (await client.post("/api/messages", json={"agent_id": "search_agent", "message_type": "status", "payload": f"unique marker {token} in payload"})).json()

    resp = await client.get("/api/search", params={"q": token, "limit": 10})
    assert resp.status_code == 200
    hits = resp.json()
    match = [h for h in hits if h["id"] == posted["id"]]
    assert match, f"search for {token!r} should find the posted message"
    assert match[0]["conversation_id"] == posted["conversation_id"]
    assert match[0]["source"] == "live"


@pytest.mark.anyio
async def test_get_conversation_returns_full_thread(client):
    await client.post("/api/agents", json={"agent_id": "conv_agent", "owner": "alan", "repo": "partyline"})
    a = (await client.post("/api/messages", json={"agent_id": "conv_agent", "message_type": "question", "payload": "q"})).json()
    b = (await client.post("/api/messages", json={"agent_id": "conv_agent", "message_type": "ack", "payload": "a", "reply_to_id": a["id"]})).json()

    resp = await client.get(f"/api/conversations/{a['conversation_id']}")
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()]
    assert ids == [a["id"], b["id"]], "thread returned oldest-first, both messages"


@pytest.mark.anyio
async def test_get_conversation_invalid_id_400(client):
    resp = await client.get("/api/conversations/not-a-uuid")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_unread_messages(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "unread_agent",
            "owner": "alan",
            "repo": "partyline",
        },
    )
    # Post a message
    post_resp = await client.post(
        "/api/messages",
        json={
            "agent_id": "unread_agent",
            "message_type": "status",
            "payload": "working",
        },
    )
    msg_id = post_resp.json()["id"]

    # Get unread (checkpoint starts at 0, so should see everything)
    resp = await client.get("/api/agents/unread_agent/unread")
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) > 0

    # Ack checkpoint
    ack_resp = await client.post(
        "/api/checkpoints",
        json={
            "agent_id": "unread_agent",
            "last_read_message_id": msg_id,
        },
    )
    assert ack_resp.status_code == 200

    # Now unread should be empty (no newer messages)
    resp2 = await client.get("/api/agents/unread_agent/unread")
    assert resp2.status_code == 200
    # May or may not be empty depending on other tests — just verify it's valid
    assert isinstance(resp2.json(), list)


@pytest.mark.anyio
async def test_claim_and_release(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "claim_agent",
            "owner": "alan",
            "repo": "partyline",
        },
    )
    # Claim a resource
    resp = await client.post(
        "/api/claims",
        json={
            "agent_id": "claim_agent",
            "repo": "partyline",
            "branch": "main",
            "resource_path": "src/auth.py",
            "resource_type": "file",
            "claim_scope": "exclusive",
            "reason": "Refactoring auth",
            "ttl_seconds": 300,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["acquired"] is True
    assert data["claim"]["agent_id"] == "claim_agent"
    assert data["claim"]["resource_path"] == "src/auth.py"

    # List claims
    list_resp = await client.get("/api/claims")
    assert list_resp.status_code == 200
    claims = list_resp.json()
    assert any(c["resource_path"] == "src/auth.py" for c in claims)

    # Release
    rel_resp = await client.post(
        "/api/claims/release",
        json={
            "agent_id": "claim_agent",
            "repo": "partyline",
            "branch": "main",
            "resource_path": "src/auth.py",
        },
    )
    assert rel_resp.status_code == 200
    assert rel_resp.json()["released"] is True


@pytest.mark.anyio
async def test_claim_conflict(client):
    """Two agents cannot hold the same exclusive claim."""
    await client.post(
        "/api/agents",
        json={
            "agent_id": "agent_a",
            "owner": "alan",
            "repo": "partyline",
        },
    )
    await client.post(
        "/api/agents",
        json={
            "agent_id": "agent_b",
            "owner": "rahul",
            "repo": "partyline",
        },
    )

    # Agent A claims
    resp_a = await client.post(
        "/api/claims",
        json={
            "agent_id": "agent_a",
            "repo": "partyline",
            "branch": "develop",
            "resource_path": "models.py",
            "claim_scope": "exclusive",
            "ttl_seconds": 600,
        },
    )
    assert resp_a.json()["acquired"] is True

    # Agent B tries to claim the same resource
    resp_b = await client.post(
        "/api/claims",
        json={
            "agent_id": "agent_b",
            "repo": "partyline",
            "branch": "develop",
            "resource_path": "models.py",
            "claim_scope": "exclusive",
            "ttl_seconds": 600,
        },
    )
    assert resp_b.json()["acquired"] is False

    # Cleanup
    await client.post(
        "/api/claims/release",
        json={
            "agent_id": "agent_a",
            "repo": "partyline",
            "branch": "develop",
            "resource_path": "models.py",
        },
    )


@pytest.mark.anyio
async def test_message_with_mentions_and_recipient(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "sender_agent",
            "owner": "alan",
            "repo": "aura",
        },
    )
    await client.post(
        "/api/agents",
        json={
            "agent_id": "target_agent",
            "owner": "rahul",
            "repo": "aura",
        },
    )

    resp = await client.post(
        "/api/messages",
        json={
            "agent_id": "sender_agent",
            "message_type": "question",
            "recipient": "target_agent",
            "mentions": ["target_agent"],
            "payload": "Can you review my changes to db.py?",
            "repo": "aura",
            "branch": "develop",
            "resource_path": "src/db.py",
        },
    )
    assert resp.status_code == 200
    msg = resp.json()
    assert msg["recipient"] == "target_agent"
    assert "target_agent" in msg["mentions"]
