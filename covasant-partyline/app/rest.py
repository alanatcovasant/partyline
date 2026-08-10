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
    ClaimWorkItemRequest,
    Message,
    PostMessage,
    RegisterAgent,
    ReleaseRequest,
)
from .targets import infer_resource_type
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


@router.get("/whoami")
async def whoami(request: Request) -> dict:
    """Best-effort identity of the browser user, for prefilling the dashboard's "from".

    Databricks Apps authenticate the user via workspace SSO at the reverse proxy and
    forward identity headers to us — independent of OBO (auth.py), which stays off. These
    ``X-Forwarded-*`` headers are only injected for interactive browser sessions, so an
    M2M/curl caller (or local dev) sees nulls; the SPA treats null as "no prefill". No SDK
    call, no token — the proxy already vouched for the user.
    """
    return {
        "user_name": request.headers.get("x-forwarded-preferred-username"),
        "email": request.headers.get("x-forwarded-email"),
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
        body.workdirs,
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
async def list_agents(active_within_seconds: int = 3600, include_stale: bool = False) -> list[dict]:
    """Registered agents with presence labels.

    `include_stale=true` returns everyone ever registered — use it to answer "does this
    agent exist at all", which a filtered roster cannot: an omitted row and a nonexistent
    agent look identical to the caller.
    """
    started = time.perf_counter()
    agents = await run_in_threadpool(db.list_agents, active_within_seconds, include_stale)
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
    """Post a message. 400s on a recipient that has never registered.

    That refusal is the point: the write is durable but delivery is unobservable, so a
    typo'd recipient used to return 200 and then never be read by anyone. An *idle*
    recipient still succeeds — it comes back with `delivery_warnings` instead, because
    idle is a timing problem and unknown is a typo.
    """
    author = await resolve_author(request)
    try:
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
            body.summary,
            body.event_time,
            allow_unregistered_recipient=body.allow_unregistered_recipient,
        )
    except db.UnknownRecipient as exc:
        log.warning("post_message REFUSED agent_id=%s recipient=%s", body.agent_id, exc.recipient)
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_recipient",
                "recipient": exc.recipient,
                "message": str(exc),
                "suggestions": exc.suggestions,
            },
        )
    log.info(
        "post_message ok id=%s agent_id=%s type=%s recipient=%s mentions=%s repo=%s branch=%s warnings=%s",
        row.get("id"),
        body.agent_id,
        body.message_type.value,
        body.recipient,
        len(body.mentions),
        body.repo,
        body.branch,
        len(row.get("delivery_warnings") or []),
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


@router.get("/messages/query")
async def query_messages(
    q: str | None = None,
    message_type: str | None = None,
    agent_id: str | None = None,
    author: str | None = None,
    recipient: str | None = None,
    mention: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    resource_path: str | None = None,
    issue_ref: str | None = None,
    conversation_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Field-filtered query over live + archived history. Every param is optional and
    AND-combined; `q` is full-text (summary+payload), the rest are exact-match. A bare
    call returns the newest `limit` rows. Invalid conversation_id UUID -> 400."""
    def _clean(v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    try:
        rows = await run_in_threadpool(
            db.query_messages,
            q=_clean(q), message_type=_clean(message_type), agent_id=_clean(agent_id),
            author=_clean(author), recipient=_clean(recipient), mention=_clean(mention),
            repo=_clean(repo), branch=_clean(branch), resource_path=_clean(resource_path),
            issue_ref=_clean(issue_ref), conversation_id=_clean(conversation_id),
            since=_clean(since), until=_clean(until), limit=limit,
        )
    except Exception as exc:  # invalid uuid → clean 400 (same guard as /api/conversations)
        raise HTTPException(status_code=400, detail=f"invalid query: {exc}")
    log.info("query_messages endpoint hits=%s", len(rows))
    return rows


@router.post("/messages/archive-all")
async def archive_all_messages() -> dict:
    """Move ALL live messages into the archive and clear the live feed (operator action).

    History is preserved (rows are moved, not dropped) and remains searchable via /api/search
    and readable via /api/conversations. Destructive to the live view, so the UI confirms
    first. Returns {archived, deleted} counts.
    """
    result = await run_in_threadpool(db.archive_all_messages)
    log.warning("archive_all_messages archived=%s deleted=%s", result.get("archived"), result.get("deleted"))
    return result


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
async def get_unread(
    agent_id: str, limit: int = 500, only_for_me: bool = False, include_payload: bool = True
) -> list[dict]:
    # include_payload defaults True here to preserve the dashboard/response contract; the
    # SessionStart hook passes include_payload=false since it only renders the summary.
    after = await run_in_threadpool(db.get_checkpoint, agent_id)
    rows = await run_in_threadpool(
        db.get_since, after, limit, agent_id if only_for_me else None, include_payload
    )
    log.info(
        "get_unread agent_id=%s checkpoint=%s only_for_me=%s limit=%s payload=%s returned=%s",
        agent_id,
        after,
        only_for_me,
        limit,
        include_payload,
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
    body.resource_type = body.resource_type or infer_resource_type(body.resource_path)
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


@router.post("/claims/work-item")
async def claim_work_item(body: ClaimWorkItemRequest) -> dict:
    """Lease a work item plus what it touches (directories and/or src: refs), atomically."""
    ttl = body.ttl_seconds or settings.default_claim_ttl_seconds
    targets = [(body.work_item, infer_resource_type(body.work_item))]
    targets += [(p, infer_resource_type(p)) for p in body.paths]
    acquired, claims, blocker = await run_in_threadpool(
        db.claim_targets,
        body.agent_id,
        body.repo,
        body.branch,
        targets,
        body.claim_scope.value,
        body.reason,
        ttl,
    )
    log.info(
        "claim_work_item agent_id=%s work_item=%s paths=%s acquired=%s blocker=%s",
        body.agent_id,
        body.work_item,
        len(body.paths),
        acquired,
        blocker.get("agent_id") if blocker else None,
    )
    return {"acquired": acquired, "claims": claims, "blocker": blocker}


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
