"""Thin REST API for non-MCP consumers (the CLI, the ops SPA, health checks).

Every message write resolves the OBO author (auth.py) and stamps it for the audit trail.
Mirrors the MCP tool surface over the same data layer.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from . import __build_host__, __build_timestamp__, __git_commit__, __version__, db
from .auth import resolve_author
from .config import settings
from .models import (
    Agent,
    Checkpoint,
    Claim,
    ClaimRequest,
    Message,
    PostMessage,
    RegisterAgent,
    ReleaseRequest,
)
from .notify import hub

router = APIRouter(prefix="/api", tags=["partyline"])
log = logging.getLogger("partyline.rest")


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "db_configured": settings.db_configured,
        "version": __version__,
        "build_timestamp": __build_timestamp__,
        "git_commit": __git_commit__,
        "build_host": __build_host__,
    }


@router.post("/agents", response_model=Agent)
async def register_agent(body: RegisterAgent) -> dict:
    row = await run_in_threadpool(
        db.register_agent,
        body.agent_id,
        body.owner,
        body.repo,
        body.branch,
        body.display_name,
    )
    log.info(
        "register_agent ok agent_id=%s owner=%s repo=%s branch=%s",
        body.agent_id,
        body.owner,
        body.repo,
        body.branch,
    )
    return row


@router.get("/agents", response_model=list[Agent])
async def list_agents(active_within_seconds: int = 3600) -> list[dict]:
    started = time.perf_counter()
    agents = await run_in_threadpool(db.list_agents, active_within_seconds)
    elapsed_ms = (time.perf_counter() - started) * 1000

    if not agents:
        diag = await run_in_threadpool(db.get_agent_presence_diagnostics, active_within_seconds, 25)
        log.warning(
            "No active agents returned (active_within_seconds=%s elapsed_ms=%.1f total=%s active=%s stale=%s)",
            active_within_seconds,
            elapsed_ms,
            diag.get("total_agents"),
            diag.get("active_agents"),
            diag.get("stale_agents"),
        )
    else:
        log.info(
            "Active agents returned count=%s active_within_seconds=%s elapsed_ms=%.1f",
            len(agents),
            active_within_seconds,
            elapsed_ms,
        )

    return agents


@router.get("/diagnostics/agents")
async def agent_presence_diagnostics(
    active_within_seconds: int = 3600,
    sample_size: int = 50,
) -> dict:
    diag = await run_in_threadpool(db.get_agent_presence_diagnostics, active_within_seconds, sample_size)
    log.info(
        "agent_presence_diagnostics requested active_within_seconds=%s sample_size=%s total=%s active=%s stale=%s",
        active_within_seconds,
        sample_size,
        diag.get("total_agents"),
        diag.get("active_agents"),
        diag.get("stale_agents"),
    )
    return diag


@router.post("/messages", response_model=Message)
async def post_message(body: PostMessage, request: Request) -> dict:
    author = await resolve_author(request)
    row = await run_in_threadpool(
        db.post_message,
        body.agent_id,
        body.message_type.value,
        body.recipient,
        body.repo,
        body.branch,
        body.resource_path,
        body.issue_ref,
        body.payload,
        body.metadata,
        body.mentions,
        body.reply_to_id,
        author,
    )
    log.info(
        "post_message ok id=%s agent_id=%s type=%s recipient=%s mentions=%s repo=%s branch=%s",
        row.get("id"),
        body.agent_id,
        body.message_type.value,
        body.recipient,
        len(body.mentions),
        body.repo,
        body.branch,
    )
    return row


@router.get("/messages", response_model=list[Message])
async def get_messages(after_id: int = 0, limit: int = 500, latest: bool = False) -> list[dict]:
    # latest=true seeds the dashboard with the NEWEST `limit` messages (ascending id).
    # Without it, after_id=0 tails from the beginning and returns the OLDEST rows.
    if latest:
        return await run_in_threadpool(db.get_recent, limit)
    return await run_in_threadpool(db.get_since, after_id, limit)


@router.get("/search")
async def search_messages(q: str, limit: int = 50) -> list[dict]:
    """Full-text search over live + archived history. Each hit carries conversation_id
    (pivot to the thread via /api/conversations/{id}) and a source (live|archive)."""
    q = (q or "").strip()
    if not q:
        return []
    rows = await run_in_threadpool(db.search_messages, q, limit)
    log.info("search q=%.60r limit=%s hits=%s", q, limit, len(rows))
    return rows


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, limit: int = 500) -> list[dict]:
    """Every message in a conversation thread (live + archived), oldest first."""
    try:
        rows = await run_in_threadpool(db.get_conversation, conversation_id, limit)
    except Exception as exc:  # invalid uuid → clean 400 rather than a 500
        raise HTTPException(status_code=400, detail=f"invalid conversation_id: {exc}")
    log.info("get_conversation id=%s returned=%s", conversation_id, len(rows))
    return rows


@router.get("/agents/{agent_id}/unread", response_model=list[Message])
async def get_unread(agent_id: str, limit: int = 500, only_for_me: bool = False) -> list[dict]:
    after = await run_in_threadpool(db.get_checkpoint, agent_id)
    rows = await run_in_threadpool(db.get_since, after, limit, agent_id if only_for_me else None)
    log.info(
        "get_unread agent_id=%s checkpoint=%s only_for_me=%s limit=%s returned=%s",
        agent_id,
        after,
        only_for_me,
        limit,
        len(rows),
    )
    return rows


@router.post("/checkpoints", response_model=Checkpoint)
async def ack_checkpoint(body: Checkpoint) -> dict:
    row = await run_in_threadpool(db.ack_checkpoint, body.agent_id, body.last_read_message_id)
    log.info(
        "ack_checkpoint agent_id=%s last_read_message_id=%s",
        body.agent_id,
        body.last_read_message_id,
    )
    return row


@router.post("/claims")
async def claim_resource(body: ClaimRequest) -> dict:
    ttl = body.ttl_seconds or settings.default_claim_ttl_seconds
    acquired, claim = await run_in_threadpool(
        db.claim_resource,
        body.agent_id,
        body.repo,
        body.branch,
        body.resource_path,
        body.resource_type,
        body.claim_scope.value,
        body.reason,
        ttl,
    )
    log.info(
        "claim_resource agent_id=%s repo=%s branch=%s resource=%s acquired=%s ttl=%s",
        body.agent_id,
        body.repo,
        body.branch,
        body.resource_path,
        acquired,
        ttl,
    )
    return {"acquired": acquired, "claim": claim}


@router.post("/claims/release")
async def release_resource(body: ReleaseRequest) -> dict:
    released = await run_in_threadpool(db.release_resource, body.agent_id, body.repo, body.branch, body.resource_path)
    log.info(
        "release_resource agent_id=%s repo=%s branch=%s resource=%s released=%s",
        body.agent_id,
        body.repo,
        body.branch,
        body.resource_path,
        released,
    )
    return {"released": released}


@router.get("/claims", response_model=list[Claim])
async def list_claims() -> list[dict]:
    rows = await run_in_threadpool(db.list_claims)
    log.info("list_claims returned=%s", len(rows))
    return rows


@router.get("/metrics")
async def get_metrics() -> dict:
    """Real-time partyline metrics for the dashboard."""
    metrics = await run_in_threadpool(db.get_metrics)
    metrics["sse_subscribers"] = hub.subscriber_count()
    return metrics


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of live message notifications (push, FR2)."""

    async def event_gen():
        sub = hub.subscribe()
        client_host = request.client.host if request.client else "unknown"
        log.info("SSE stream connected client=%s subscribers=%s", client_host, hub.subscriber_count())
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                # `closed` is set when the hub drops this subscriber as a slow consumer;
                # honor it (and client disconnect) so we don't linger as a zombie stream.
                if sub.closed.is_set():
                    log.info("SSE stream reaped (slow consumer) client=%s", client_host)
                    break
                if await request.is_disconnected():
                    log.info("SSE stream disconnected client=%s", client_host)
                    break
                try:
                    payload = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    if sub.closed.is_set():
                        break
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(sub)
            log.info("SSE stream cleanup client=%s subscribers=%s", client_host, hub.subscriber_count())

    return StreamingResponse(event_gen(), media_type="text/event-stream")
