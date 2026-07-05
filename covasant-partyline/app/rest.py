"""Thin REST API for non-MCP consumers (the CLI, the ops SPA, health checks).

Every message write resolves the OBO author (auth.py) and stamps it for the audit trail.
Mirrors the MCP tool surface over the same data layer.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from . import db
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


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "db_configured": settings.db_configured}


@router.post("/agents", response_model=Agent)
async def register_agent(body: RegisterAgent) -> dict:
    return await run_in_threadpool(
        db.register_agent, body.agent_id, body.owner, body.repo,
        body.branch, body.display_name,
    )


@router.get("/agents", response_model=list[Agent])
async def list_agents(active_within_seconds: int = 3600) -> list[dict]:
    return await run_in_threadpool(db.list_agents, active_within_seconds)


@router.post("/messages", response_model=Message)
async def post_message(body: PostMessage, request: Request) -> dict:
    author = await resolve_author(request)
    return await run_in_threadpool(
        db.post_message, body.agent_id, body.message_type.value, body.recipient,
        body.repo, body.branch, body.resource_path, body.issue_ref, body.payload,
        body.metadata, body.mentions, body.reply_to_id, author,
    )


@router.get("/messages", response_model=list[Message])
async def get_messages(after_id: int = 0, limit: int = 500) -> list[dict]:
    return await run_in_threadpool(db.get_since, after_id, limit)


@router.get("/agents/{agent_id}/unread", response_model=list[Message])
async def get_unread(agent_id: str, limit: int = 500, only_for_me: bool = False) -> list[dict]:
    after = await run_in_threadpool(db.get_checkpoint, agent_id)
    return await run_in_threadpool(
        db.get_since, after, limit, agent_id if only_for_me else None
    )


@router.post("/checkpoints", response_model=Checkpoint)
async def ack_checkpoint(body: Checkpoint) -> dict:
    return await run_in_threadpool(
        db.ack_checkpoint, body.agent_id, body.last_read_message_id
    )


@router.post("/claims")
async def claim_resource(body: ClaimRequest) -> dict:
    ttl = body.ttl_seconds or settings.default_claim_ttl_seconds
    acquired, claim = await run_in_threadpool(
        db.claim_resource, body.agent_id, body.repo, body.branch, body.resource_path,
        body.resource_type, body.claim_scope.value, body.reason, ttl,
    )
    return {"acquired": acquired, "claim": claim}


@router.post("/claims/release")
async def release_resource(body: ReleaseRequest) -> dict:
    released = await run_in_threadpool(
        db.release_resource, body.agent_id, body.repo, body.branch, body.resource_path
    )
    return {"released": released}


@router.get("/claims", response_model=list[Claim])
async def list_claims() -> list[dict]:
    return await run_in_threadpool(db.list_claims)


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream of live message notifications (push, FR2)."""

    async def event_gen():
        q = hub.subscribe()
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")
