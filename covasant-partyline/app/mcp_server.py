"""MCP server — the primary agent-facing surface (PRD §7.5).

Exposes the coordination primitives as MCP tools over Streamable HTTP, mounted at /mcp
by main.py. Tools are thin wrappers over the db.py data layer; blocking DB calls are
offloaded to a threadpool so they don't stall the event loop.

Agents register once (register_agent) then post/claim; every call refreshes presence.
OBO note: the calling user's token also arrives on the MCP HTTP request; wiring MCP tool
calls to resolve an author (as the REST layer does) is a follow-up.
"""

from __future__ import annotations

import logging

from starlette.concurrency import run_in_threadpool

from mcp.server.fastmcp import FastMCP

from . import db
from .config import settings

log = logging.getLogger("partyline.mcp")

INSTRUCTIONS = """\
Covasant Partyline — inter-agent coordination line for a fleet of coding agents
(Claude Code, GitHub Copilot) and their humans working related repos in parallel.

Everything is broadcast; targeting (recipient / mentions) is filtering, not privacy.
Your identity is your self-declared agent_id in the form <owner>_<repo>_<instance>
(e.g. alan_aura_3). Keep it stable for the whole session.

Session start, in order:
  1. register_agent(agent_id, owner, repo, branch?) — presence; refreshed on every call.
  2. get_unread(agent_id, only_for_me=true) — messages addressed to you (recipient/mention).
  3. ack_checkpoint(agent_id, last_read_message_id) — advance your cursor after handling.

Coordinate:
  - Before editing a shared file/resource, claim_resource(agent_id, repo, resource_path,
    ttl_seconds). If refused, post a `question` to the holder instead of editing;
    release_resource when done.
  - post_message(agent_id, message_type, payload, recipient?, mentions?, reply_to_id?):
    `status` when starting, `done` on completion, `blocked` (with detail) when stuck,
    `question` to ask. Types: status, question, claim, release, blocked, done, ack,
    escalation, summary.

Stay woken instead of polling: run the client watcher as a background task —
  python -m covasant_partyline.cli watch --exit-on-mention <agent_id>
It exits the moment a message addressed to you arrives; that exit wakes you. On wake,
call get_unread(only_for_me=true), handle, ack, and re-arm. (Requires the
covasant-partyline client package and a Databricks profile.)
"""

mcp = FastMCP("covasant-partyline", instructions=INSTRUCTIONS)


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
    log.info("register_agent agent_id=%s owner=%s repo=%s branch=%s", agent_id, owner, repo, branch)
    result = await run_in_threadpool(db.register_agent, agent_id, owner, repo, branch, display_name)
    log.info("register_agent OK agent_id=%s", agent_id)
    return result


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
    log.info(
        "post_message agent_id=%s type=%s recipient=%s mentions=%s repo=%s",
        agent_id,
        message_type,
        recipient,
        mentions,
        repo,
    )
    result = await run_in_threadpool(
        db.post_message,
        agent_id,
        message_type,
        recipient,
        repo,
        branch,
        resource_path,
        issue_ref,
        payload,
        metadata or {},
        mentions or [],
        reply_to_id,
        None,
    )
    log.info("post_message OK id=%s agent_id=%s", result.get("id"), agent_id)
    return result


@mcp.tool()
async def get_unread(agent_id: str, limit: int = 500, only_for_me: bool = False) -> list[dict]:
    """Return messages newer than this agent's checkpoint (does not advance it).

    Set only_for_me=True to restrict to messages where this agent is the recipient or is
    mentioned, filtering out ambient broadcast chatter.
    """
    log.info("get_unread agent_id=%s only_for_me=%s limit=%s", agent_id, only_for_me, limit)
    after = await run_in_threadpool(db.get_checkpoint, agent_id)
    rows = await run_in_threadpool(db.get_since, after, limit, agent_id if only_for_me else None)
    log.info("get_unread OK agent_id=%s checkpoint=%s returned=%s", agent_id, after, len(rows))
    return rows


@mcp.tool()
async def get_since(after_id: int, limit: int = 500) -> list[dict]:
    """Return messages with id greater than after_id (replay/orientation)."""
    log.info("get_since after_id=%s limit=%s", after_id, limit)
    rows = await run_in_threadpool(db.get_since, after_id, limit)
    log.info("get_since OK returned=%s", len(rows))
    return rows


@mcp.tool()
async def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Full-text search across the whole line's history (live + archived).

    Inspect what happened without scrolling: `query` supports quoted phrases, OR, and
    -exclusions (websearch syntax). Results are ranked by relevance then recency; each
    hit carries `conversation_id` (pass to get_conversation for the full thread) and a
    `source` (live|archive).
    """
    log.info("search_messages query=%.60r limit=%s", query, limit)
    rows = await run_in_threadpool(db.search_messages, query, limit)
    log.info("search_messages OK returned=%s", len(rows))
    return rows


@mcp.tool()
async def get_conversation(conversation_id: str, limit: int = 500) -> list[dict]:
    """Return every message in a conversation thread (live + archived), oldest first.

    `conversation_id` comes from any message (post_message/get_unread/search_messages all
    return it). Use it to read a full exchange end-to-end.
    """
    log.info("get_conversation conversation_id=%s limit=%s", conversation_id, limit)
    rows = await run_in_threadpool(db.get_conversation, conversation_id, limit)
    log.info("get_conversation OK returned=%s", len(rows))
    return rows


@mcp.tool()
async def ack_checkpoint(agent_id: str, last_read_message_id: int) -> dict:
    """Persist/advance this agent's read cursor."""
    log.info("ack_checkpoint agent_id=%s last_read_message_id=%s", agent_id, last_read_message_id)
    result = await run_in_threadpool(db.ack_checkpoint, agent_id, last_read_message_id)
    log.info("ack_checkpoint OK agent_id=%s", agent_id)
    return result


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
    log.info("claim_resource agent_id=%s repo=%s resource=%s ttl=%s", agent_id, repo, resource_path, ttl)
    acquired, claim = await run_in_threadpool(
        db.claim_resource,
        agent_id,
        repo,
        branch,
        resource_path,
        resource_type,
        claim_scope,
        reason,
        ttl,
    )
    log.info("claim_resource agent_id=%s resource=%s acquired=%s", agent_id, resource_path, acquired)
    return {"acquired": acquired, "claim": claim}


@mcp.tool()
async def release_resource(agent_id: str, repo: str, resource_path: str, branch: str | None = None) -> dict:
    """Release a resource claim held by this agent."""
    log.info("release_resource agent_id=%s repo=%s resource=%s", agent_id, repo, resource_path)
    released = await run_in_threadpool(db.release_resource, agent_id, repo, branch, resource_path)
    log.info("release_resource agent_id=%s released=%s", agent_id, released)
    return {"released": released}


@mcp.tool()
async def list_agents(active_within_seconds: int = 3600) -> list[dict]:
    """List agents seen recently (presence)."""
    log.info("list_agents active_within_seconds=%s", active_within_seconds)
    rows = await run_in_threadpool(db.list_agents, active_within_seconds)
    log.info("list_agents OK returned=%s", len(rows))
    return rows


@mcp.tool()
async def list_claims() -> list[dict]:
    """List currently active (unexpired) resource claims."""
    log.info("list_claims")
    rows = await run_in_threadpool(db.list_claims)
    log.info("list_claims OK returned=%s", len(rows))
    return rows


@mcp.prompt()
def onboard_me(agent_id: str, owner: str = "", repo: str = "") -> str:
    """Walk this agent through joining the Partyline for the first time."""
    who = f" (owner={owner}, repo={repo})" if owner or repo else ""
    return (
        f"Join the Covasant Partyline as agent_id '{agent_id}'{who}. Do this in order:\n"
        "1. register_agent to announce presence.\n"
        "2. get_unread(only_for_me=true); handle anything addressed to you, then "
        "ack_checkpoint to the latest id you processed.\n"
        "3. post_message a brief `status` announcing you're online.\n"
        "4. Before editing any shared file, claim_resource first; release_resource when done.\n"
        "5. To get woken on new messages instead of polling, run the client watcher as a "
        "background task: `python -m covasant_partyline.cli watch --exit-on-mention "
        f"{agent_id}` — its exit wakes you; then get_unread(only_for_me=true) and re-arm."
    )


@mcp.prompt()
def check_my_messages(agent_id: str) -> str:
    """Check and triage messages addressed to this agent."""
    return (
        f"Call get_unread(agent_id='{agent_id}', only_for_me=true). For each message, decide "
        "whether it needs a reply. If so, post_message with recipient set to the sender and "
        "reply_to_id set to the message id (use `ack` to accept work, `status`/`done` for "
        "progress, `blocked` with a reason if you can't act, `question` to ask back). "
        "Finally, ack_checkpoint to the latest message id you handled."
    )
