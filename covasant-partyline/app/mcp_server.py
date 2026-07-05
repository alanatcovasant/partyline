"""MCP server — the primary agent-facing surface (PRD §7.5).

Exposes the coordination primitives as MCP tools over Streamable HTTP, mounted at /mcp
by main.py. Tools are thin wrappers over the db.py data layer; blocking DB calls are
offloaded to a threadpool so they don't stall the event loop.

Agents register once (register_agent) then post/claim; every call refreshes presence.
OBO note: the calling user's token also arrives on the MCP HTTP request; wiring MCP tool
calls to resolve an author (as the REST layer does) is a follow-up.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool

from mcp.server.fastmcp import FastMCP

from . import db
from .config import settings

mcp = FastMCP("covasant-partyline")


@mcp.tool()
async def register_agent(
    agent_id: str,
    owner: str,
    repo: str,
    branch: str | None = None,
    display_name: str | None = None,
) -> dict:
    """Register (or refresh) this agent instance. Call once before posting/claiming.

    agent_id is a unique instance name (e.g. 'alan_aura_3'); owner is the developer/team.
    """
    return await run_in_threadpool(
        db.register_agent, agent_id, owner, repo, branch, display_name
    )


@mcp.tool()
async def post_message(
    agent_id: str,
    message_type: str = "status",
    recipient: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    resource_path: str | None = None,
    issue_ref: str | None = None,
    payload: str | None = None,
    metadata: dict | None = None,
    mentions: list[str] | None = None,
    reply_to_id: int | None = None,
) -> dict:
    """Post a structured coordination message.

    message_type is one of: status, question, claim, release, blocked, done, ack,
    escalation, summary. recipient is the primary addressee ("To:") but the message is
    still broadcast to the whole line. payload is freeform text; metadata is an opaque bag.
    """
    return await run_in_threadpool(
        db.post_message, agent_id, message_type, recipient, repo, branch, resource_path,
        issue_ref, payload, metadata or {}, mentions or [], reply_to_id, None,
    )


@mcp.tool()
async def get_unread(agent_id: str, limit: int = 500, only_for_me: bool = False) -> list[dict]:
    """Return messages newer than this agent's checkpoint (does not advance it).

    Set only_for_me=True to restrict to messages where this agent is the recipient or is
    mentioned, filtering out ambient broadcast chatter.
    """
    after = await run_in_threadpool(db.get_checkpoint, agent_id)
    return await run_in_threadpool(
        db.get_since, after, limit, agent_id if only_for_me else None
    )


@mcp.tool()
async def get_since(after_id: int, limit: int = 500) -> list[dict]:
    """Return messages with id greater than after_id (replay/orientation)."""
    return await run_in_threadpool(db.get_since, after_id, limit)


@mcp.tool()
async def ack_checkpoint(agent_id: str, last_read_message_id: int) -> dict:
    """Persist/advance this agent's read cursor."""
    return await run_in_threadpool(db.ack_checkpoint, agent_id, last_read_message_id)


@mcp.tool()
async def claim_resource(
    agent_id: str,
    repo: str,
    resource_path: str,
    branch: str | None = None,
    resource_type: str | None = None,
    claim_scope: str = "exclusive",
    reason: str | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """Atomically claim (repo, branch, resource_path) with a lease. Fails if held.

    Returns {acquired: bool, claim: {...}}. On failure, claim is the current holder.
    """
    ttl = ttl_seconds or settings.default_claim_ttl_seconds
    acquired, claim = await run_in_threadpool(
        db.claim_resource, agent_id, repo, branch, resource_path,
        resource_type, claim_scope, reason, ttl,
    )
    return {"acquired": acquired, "claim": claim}


@mcp.tool()
async def release_resource(
    agent_id: str, repo: str, resource_path: str, branch: str | None = None
) -> dict:
    """Release a resource claim held by this agent."""
    released = await run_in_threadpool(
        db.release_resource, agent_id, repo, branch, resource_path
    )
    return {"released": released}


@mcp.tool()
async def list_agents(active_within_seconds: int = 3600) -> list[dict]:
    """List agents seen recently (presence)."""
    return await run_in_threadpool(db.list_agents, active_within_seconds)


@mcp.tool()
async def list_claims() -> list[dict]:
    """List currently active (unexpired) resource claims."""
    return await run_in_threadpool(db.list_claims)
