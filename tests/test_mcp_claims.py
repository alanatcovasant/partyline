"""MCP claim tools return structured refusals an agent can act on."""

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


async def test_invalid_target_returns_structured_refusal_not_an_exception(started_app):
    from app import mcp_server

    out = await mcp_server.claim_resource(
        agent_id="mcp_x", repo="aura", resource_path="db.py",
    )
    assert out["acquired"] is False
    assert out["reason"] == "invalid_target"
    assert "not absolute" in out["message"]
    assert out["hint"]


async def test_claim_work_item_leases_issue_and_paths(started_app):
    from app import db, mcp_server

    db.register_agent("mcp_wi", "test", "aura", "main", None)
    out = await mcp_server.claim_work_item(
        agent_id="mcp_wi", repo="aura", work_item="issue:covasant/aura#903",
        paths=["/t/mcp1/pkg"], ttl_seconds=300,
    )
    assert out["acquired"] is True
    assert {c["resource_path"] for c in out["claims"]} == {
        "issue:covasant/aura#903", "/t/mcp1/pkg",
    }
    db.release_resource("mcp_wi", "aura", None, "issue:covasant/aura#903")
    db.release_resource("mcp_wi", "aura", None, "/t/mcp1/pkg")


async def test_claim_work_item_refuses_malformed_work_item(started_app):
    from app import mcp_server

    out = await mcp_server.claim_work_item(
        agent_id="mcp_wi", repo="aura", work_item="issue:aura", paths=[],
    )
    assert out["acquired"] is False
    assert out["reason"] == "invalid_target"
    assert "malformed work-item" in out["message"]


async def test_register_agent_tool_reports_refused_workdir(started_app):
    from app import db, mcp_server

    db.register_agent("mcp_wd_a", "test", "aura", "main", None, ["/t/mcpwd/src"])
    out = await mcp_server.register_agent(
        agent_id="mcp_wd_b", owner="test", repo="aura", branch="main",
        workdirs=["/t/mcpwd/src/inner"],
    )
    assert out["agent_id"] == "mcp_wd_b"        # presence never fails
    assert out["workdir_claims"][0]["acquired"] is False
    assert out["workdir_claims"][0]["holder"]["agent_id"] == "mcp_wd_a"
    db.release_resource("mcp_wd_a", "aura", None, "/t/mcpwd/src")
