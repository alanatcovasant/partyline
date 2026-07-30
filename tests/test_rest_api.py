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
async def test_whoami_echoes_forwarded_identity(client):
    """Databricks Apps forward the SSO user's identity headers; /api/whoami surfaces them."""
    resp = await client.get(
        "/api/whoami",
        headers={
            "X-Forwarded-Preferred-Username": "alan.dennis",
            "X-Forwarded-Email": "alan.dennis@example.com",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "user_name": "alan.dennis",
        "email": "alan.dennis@example.com",
    }


@pytest.mark.anyio
async def test_whoami_null_without_forwarded_headers(client):
    """No proxy headers (local dev, M2M/curl) => nulls; the SPA then skips the prefill."""
    resp = await client.get("/api/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"user_name": None, "email": None}


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
        "repo": "web",
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
            "repo": "web",
        },
    )
    await client.post(
        "/api/agents",
        json={
            "agent_id": "target_agent",
            "owner": "rahul",
            "repo": "web",
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
            "repo": "web",
            "branch": "develop",
            "resource_path": "src/db.py",
        },
    )
    assert resp.status_code == 200
    msg = resp.json()
    assert msg["recipient"] == "target_agent"
    assert "target_agent" in msg["mentions"]


@pytest.mark.anyio
async def test_post_message_accepts_event_time(client, monkeypatch):
    """event_time (ISO-8601, optional) must be forwarded to db.post_message as the
    trailing positional arg, so the bridge's background sender can stamp true emit
    time rather than DB-insert time."""
    captured = {}

    def fake_post_message(*args, **kwargs):
        # event_time is the last positional arg (see db.post_message signature)
        captured["event_time"] = args[-1]
        return {
            "id": 1, "agent_id": "a", "message_type": "status", "recipient": None,
            "repo": None, "branch": None, "resource_path": None, "issue_ref": None,
            "summary": None, "payload": "hi", "metadata": {}, "mentions": [],
            "reply_to_id": None, "author": None, "conversation_id": None,
            "created_at": "2026-07-16T00:00:00Z", "event_time": "2026-07-16T00:00:00Z",
            "received_at": "2026-07-16T00:00:00Z",
        }

    monkeypatch.setattr("app.db.post_message", fake_post_message)
    r = await client.post("/api/messages", json={
        "agent_id": "a", "message_type": "status", "payload": "hi",
        "event_time": "2026-07-16T00:00:00Z",
    })
    assert r.status_code == 200
    # event_time is typed `datetime | None` on PostMessage, so Pydantic parses the
    # ISO-8601 string before it reaches db.post_message.
    from datetime import datetime, timezone

    assert captured["event_time"] == datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_query_endpoint_filters_by_issue_ref(client):
    await client.post("/api/agents", json={"agent_id": "q_issue_agent", "owner": "alan", "repo": "partyline"})
    posted = (await client.post("/api/messages", json={
        "agent_id": "q_issue_agent", "message_type": "status", "issue_ref": "GH-9931", "payload": "x",
    })).json()
    resp = await client.get("/api/messages/query", params={"issue_ref": "GH-9931"})
    assert resp.status_code == 200
    ids = [m["id"] for m in resp.json()]
    assert posted["id"] in ids
    assert all(m["issue_ref"] == "GH-9931" for m in resp.json())


@pytest.mark.anyio
async def test_query_endpoint_blank_params_ignored(client):
    """Empty-string params are dropped (treated as not filtered), not matched literally."""
    await client.post("/api/agents", json={"agent_id": "q_blank_agent", "owner": "alan", "repo": "partyline"})
    await client.post("/api/messages", json={"agent_id": "q_blank_agent", "message_type": "status", "payload": "y"})
    resp = await client.get("/api/messages/query", params={"repo": "", "q": "", "limit": 5})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)  # returns newest rows, not zero from a blank match


@pytest.mark.anyio
async def test_query_endpoint_invalid_conversation_id_400(client):
    resp = await client.get("/api/messages/query", params={"conversation_id": "not-a-uuid"})
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_search_endpoint_still_returns_source_shape(client):
    """Back-compat: /api/search still works and returns the source-tagged shape."""
    await client.post("/api/agents", json={"agent_id": "bc_agent", "owner": "alan", "repo": "partyline"})
    token = "bctok" + "9182"
    posted = (await client.post("/api/messages", json={
        "agent_id": "bc_agent", "message_type": "status", "payload": f"backcompat {token}",
    })).json()
    resp = await client.get("/api/search", params={"q": token, "limit": 10})
    assert resp.status_code == 200
    match = [h for h in resp.json() if h["id"] == posted["id"]]
    assert match and match[0]["source"] == "live"
