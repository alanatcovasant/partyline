"""Optional harness/model on register + mid-session runtime update.

Not every harness can report a model (or even a harness name). Both fields are
optional; re-register without them must preserve previously stored values.
"""

import pytest


@pytest.mark.anyio
async def test_register_stores_optional_harness_and_model(client):
    resp = await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_reg_1",
            "owner": "alan",
            "repo": "partyline",
            "harness": "cursor",
            "model": "claude-opus-4-6",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["harness"] == "cursor"
    assert data["model"] == "claude-opus-4-6"

    listed = (await client.get("/api/agents")).json()
    row = next(a for a in listed if a["agent_id"] == "runtime_reg_1")
    assert row["harness"] == "cursor"
    assert row["model"] == "claude-opus-4-6"


@pytest.mark.anyio
async def test_register_without_runtime_fields_is_null(client):
    resp = await client.post(
        "/api/agents",
        json={"agent_id": "runtime_reg_bare", "owner": "alan", "repo": "partyline"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("harness") is None
    assert data.get("model") is None


@pytest.mark.anyio
async def test_reregister_omitting_runtime_preserves_prior_values(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_preserve",
            "owner": "alan",
            "repo": "partyline",
            "harness": "qwen",
            "model": "qwen3-coder",
        },
    )
    resp = await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_preserve",
            "owner": "alan",
            "repo": "partyline",
            "branch": "develop",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["branch"] == "develop"
    assert data["harness"] == "qwen"
    assert data["model"] == "qwen3-coder"


@pytest.mark.anyio
async def test_reregister_can_overwrite_runtime_when_provided(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_overwrite",
            "owner": "alan",
            "repo": "partyline",
            "harness": "cursor",
            "model": "old-model",
        },
    )
    resp = await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_overwrite",
            "owner": "alan",
            "repo": "partyline",
            "harness": "claude-code",
            "model": "claude-sonnet-4-6",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["harness"] == "claude-code"
    assert resp.json()["model"] == "claude-sonnet-4-6"


@pytest.mark.anyio
async def test_update_model_mid_session(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_update",
            "owner": "alan",
            "repo": "partyline",
            "harness": "cursor",
            "model": "composer-2",
        },
    )
    resp = await client.patch(
        "/api/agents/runtime_update/runtime",
        json={"model": "gpt-5.4"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "gpt-5.4"
    assert data["harness"] == "cursor"  # untouched


@pytest.mark.anyio
async def test_update_harness_and_model_together(client):
    await client.post(
        "/api/agents",
        json={"agent_id": "runtime_both", "owner": "alan", "repo": "partyline"},
    )
    resp = await client.patch(
        "/api/agents/runtime_both/runtime",
        json={"harness": "codex", "model": "o3"},
    )
    assert resp.status_code == 200
    assert resp.json()["harness"] == "codex"
    assert resp.json()["model"] == "o3"


@pytest.mark.anyio
async def test_update_runtime_can_clear_with_null(client):
    await client.post(
        "/api/agents",
        json={
            "agent_id": "runtime_clear",
            "owner": "alan",
            "repo": "partyline",
            "model": "to-be-cleared",
        },
    )
    resp = await client.patch(
        "/api/agents/runtime_clear/runtime",
        json={"model": None},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] is None


@pytest.mark.anyio
async def test_update_runtime_404_when_unregistered(client):
    resp = await client.patch(
        "/api/agents/no_such_agent/runtime",
        json={"model": "x"},
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_update_runtime_422_when_body_empty(client):
    await client.post(
        "/api/agents",
        json={"agent_id": "runtime_empty", "owner": "alan", "repo": "partyline"},
    )
    resp = await client.patch("/api/agents/runtime_empty/runtime", json={})
    assert resp.status_code == 422
