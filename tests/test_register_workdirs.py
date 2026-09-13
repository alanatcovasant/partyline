"""Registration takes territory — and never fails because of a conflict."""

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean_test_claims(started_app):
    from app import db

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM claims WHERE resource_path LIKE '/t/%%'")
        conn.commit()
    yield


async def test_register_claims_its_workdir(client):
    r = await client.post("/api/agents", json={
        "agent_id": "wd_a", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["/t/wd1/domain"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["workdir_claims"][0]["acquired"] is True
    assert body["workdir_claims"][0]["claim"]["resource_type"] == "workdir"
    claims = (await client.get("/api/claims")).json()
    assert any(c["resource_path"] == "/t/wd1/domain" for c in claims)


async def test_overlapping_workdir_reports_holder_but_still_registers(client):
    await client.post("/api/agents", json={
        "agent_id": "wd_b", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["/t/wd2/src"],
    })
    r = await client.post("/api/agents", json={
        "agent_id": "wd_c", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["/t/wd2/src/tenant"],
    })
    assert r.status_code == 200          # presence must never fail
    assert r.json()["agent_id"] == "wd_c"
    wd = r.json()["workdir_claims"][0]
    assert wd["acquired"] is False
    assert wd["holder"]["agent_id"] == "wd_b"
    assert wd["claim"] is None
    claims = (await client.get("/api/claims")).json()
    assert not any(c["resource_path"] == "/t/wd2/src/tenant" for c in claims)


async def test_register_without_workdirs_is_unchanged(client):
    r = await client.post("/api/agents", json={
        "agent_id": "wd_d", "owner": "test", "repo": "aura", "branch": "main",
    })
    assert r.status_code == 200
    assert r.json()["workdir_claims"] == []


async def test_non_canonical_workdir_is_rejected_with_422(client):
    r = await client.post("/api/agents", json={
        "agent_id": "wd_e", "owner": "test", "repo": "aura", "branch": "main",
        "workdirs": ["src/domain"],
    })
    assert r.status_code == 422
    assert "workdirs" in r.text
