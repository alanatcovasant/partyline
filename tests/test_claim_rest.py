"""REST-layer claim validation + work-item claims (needs local Postgres)."""

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean_test_claims(started_app):
    from app import db

    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM claims WHERE resource_path LIKE '/t/%%' "
            "OR resource_path LIKE 'issue:covasant/aura#%%'"
        )
        conn.commit()
    yield


async def _register(client, agent_id):
    await client.post("/api/agents", json={
        "agent_id": agent_id, "owner": "test", "repo": "aura", "branch": "main",
    })


@pytest.mark.parametrize("bad", ["db.py", "app/db.py", "C:\\Users\\x", "/a/b/", "/a/../b"])
async def test_non_canonical_target_is_rejected_with_422(client, bad):
    await _register(client, "rest_v")
    r = await client.post("/api/claims", json={
        "agent_id": "rest_v", "repo": "aura", "resource_path": bad,
    })
    assert r.status_code == 422
    assert "resource_path" in r.text


async def test_work_item_claim_leases_issue_and_paths_together(client):
    await _register(client, "rest_wi")
    r = await client.post("/api/claims/work-item", json={
        "agent_id": "rest_wi", "repo": "aura", "work_item": "issue:covasant/aura#901",
        "paths": ["/t/wi1/pkg", "/t/wi1/other"], "ttl_seconds": 300,
    })
    body = r.json()
    assert body["acquired"] is True
    assert {c["resource_path"] for c in body["claims"]} == {
        "issue:covasant/aura#901", "/t/wi1/pkg", "/t/wi1/other",
    }
    assert next(
        c for c in body["claims"] if c["resource_path"].startswith("issue:")
    )["resource_type"] == "issue"
    for p in ("issue:covasant/aura#901", "/t/wi1/pkg", "/t/wi1/other"):
        await client.post("/api/claims/release", json={
            "agent_id": "rest_wi", "repo": "aura", "resource_path": p,
        })


async def test_work_item_claim_is_all_or_nothing(client):
    """If any path is held, nothing is leased — not even the issue."""
    await _register(client, "rest_a")
    await _register(client, "rest_b")
    await client.post("/api/claims", json={
        "agent_id": "rest_a", "repo": "aura", "resource_path": "/t/wi2/taken",
        "ttl_seconds": 300,
    })
    r = await client.post("/api/claims/work-item", json={
        "agent_id": "rest_b", "repo": "aura", "work_item": "issue:covasant/aura#902",
        "paths": ["/t/wi2/free", "/t/wi2/taken/sub"], "ttl_seconds": 300,
    })
    body = r.json()
    assert body["acquired"] is False
    assert body["blocker"]["agent_id"] == "rest_a"
    claims = (await client.get("/api/claims")).json()
    paths = {c["resource_path"] for c in claims}
    assert "issue:covasant/aura#902" not in paths  # the issue was NOT leased
    assert "/t/wi2/free" not in paths
    await client.post("/api/claims/release", json={
        "agent_id": "rest_a", "repo": "aura", "resource_path": "/t/wi2/taken",
    })
