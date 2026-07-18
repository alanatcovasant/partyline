"""Covasant Partyline — single Databricks App entrypoint.

One FastAPI process serves three surfaces:
  - /mcp   the MCP server (Streamable HTTP)  — primary agent interface
  - /api   the REST API + SSE stream          — CLI, dashboard, non-MCP consumers
  - /      the static ops SPA (if built)       — monitoring/maintenance

Startup opens the Lakebase pool, ensures the schema (FR9), and starts the LISTEN
listener. All of this is a no-op when the DB isn't configured (e.g. bare local boot),
so the app still serves /api/health for smoke checks.

SINGLE REPLICA / SINGLE WORKER ONLY. This process keeps per-process in-memory state
that is NOT shared across replicas: the SSE subscriber set (notify.py) and the MCP
Streamable-HTTP session manager. Running more than one worker/replica would break SSE
fan-out (a client on replica A never receives NOTIFYs delivered to replica B's hub) and
MCP session affinity (a session created on A is invisible on B). Keep uvicorn at one
worker (see __main__.py — no --workers) and the app at one replica (resources/*.app.yml —
no autoscale). If horizontal scale is ever needed, move SSE fan-out to a shared broker
(Postgres LISTEN already broadcasts to all listeners) and make MCP sessions sticky or
externalized first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from uuid import uuid4
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from . import __build_timestamp__, __git_commit__, __version__, db
from .config import settings
from .mcp_server import mcp
from .notify import hub
from .rest import router as rest_router

_log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("partyline")

_otel_enabled = os.getenv("PARTYLINE_OTEL_ENABLED", "true").lower() == "true"
_otel_local = os.getenv("PARTYLINE_OTEL_LOCAL", "false").lower() == "true"
_slow_request_ms = float(os.getenv("PARTYLINE_SLOW_REQUEST_MS", "2000"))

# Serve the MCP endpoint exactly at /mcp (not /mcp/mcp).
mcp.settings.streamable_http_path = "/"
mcp_app = mcp.streamable_http_app()


async def _bring_up_db() -> None:
    """Open the pool, run migrations, and start the LISTEN hub. Call once, when the DB
    is configured."""
    await asyncio.to_thread(db.open_pool)
    if settings.auto_migrate:
        await asyncio.to_thread(db.init_schema)
    hub.start()
    log.info(
        "Partyline ready (version=%s build=%s commit=%s db=%s).",
        __version__,
        __build_timestamp__,
        __git_commit__,
        settings.pg_host or os.getenv("PGHOST"),
    )


async def _self_heal_db(max_wait_secs: float = 600.0) -> None:
    """Wait for late PG* env injection, then bring up the DB layer.

    Databricks Apps can inject the bound Postgres resource's env vars *after* the process
    starts (binding race). Without this, an app that boots before injection would serve
    health/UI only — with a constructed-but-closed pool and no listener — until a manual
    restart. Polling here lets it self-heal whenever the env finally appears.
    """
    waited = 0.0
    while waited < max_wait_secs:
        await asyncio.sleep(3)
        waited += 3
        if settings.db_configured:
            log.info("DB env present after %.0fs; bringing up DB layer", waited)
            await _bring_up_db()
            return
        log.warning(
            "DB not configured yet (%.0fs/%.0fs); waiting for env injection ... "
            "PGHOST=%r PGUSER=%r ENDPOINT_NAME=%r",
            waited,
            max_wait_secs,
            os.getenv("PGHOST"),
            os.getenv("PGUSER"),
            os.getenv("ENDPOINT_NAME"),
        )
    log.error(
        "DB not configured after %.0fs; serving health/UI only. "
        "PGHOST=%r PGUSER=%r ENDPOINT_NAME=%r local=%r",
        max_wait_secs,
        os.getenv("PGHOST"),
        os.getenv("PGUSER"),
        os.getenv("ENDPOINT_NAME"),
        settings.local,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The MCP session manager must run for the mounted Streamable-HTTP app to work.
    async with mcp.session_manager.run():
        if _otel_enabled:
            try:
                from .telemetry import init_telemetry

                init_telemetry(local=_otel_local)
                log.info("Telemetry initialized (local=%s)", _otel_local)
            except ModuleNotFoundError as exc:
                log.warning(
                    "Telemetry requested but dependency missing (%s); continuing without OTEL",
                    exc,
                )
            except Exception:
                log.exception("Telemetry initialization failed; continuing without OTEL")

        heal_task: asyncio.Task | None = None
        if settings.db_configured:
            log.info(
                "DB configured: PGHOST=%s PGUSER=%s ENDPOINT_NAME=%s",
                settings.pg_host or os.getenv("PGHOST"),
                settings.pg_user or os.getenv("PGUSER"),
                settings.endpoint_name or os.getenv("ENDPOINT_NAME"),
            )
            await _bring_up_db()
        else:
            log.warning(
                "DB not configured at startup (PGHOST=%r PGUSER=%r ENDPOINT_NAME=%r); "
                "starting self-heal poller for late env injection.",
                os.getenv("PGHOST"),
                os.getenv("PGUSER"),
                os.getenv("ENDPOINT_NAME"),
            )
            heal_task = asyncio.create_task(_self_heal_db(), name="partyline-db-selfheal")

        try:
            yield
        finally:
            if heal_task is not None:
                heal_task.cancel()
                try:
                    await heal_task
                except asyncio.CancelledError:
                    pass
            await hub.stop()
            await asyncio.to_thread(db.close_pool)


app = FastAPI(title="Covasant Partyline", version=__version__, lifespan=lifespan)
app.include_router(rest_router)
app.mount("/mcp", mcp_app)


# The canonical using-partyline skill, served so any agent (or human) can fetch it on
# demand instead of carrying it in context. deploy.sh snapshots the plugin skill file to
# app/using-partyline.md (gitignored + force-synced like __version__.py — see databricks.yml
# sync.include). Registered before the SPA mount at "/" so this path wins.
_SKILL_MD = Path(__file__).resolve().parent / "using-partyline.md"


@app.get("/skills/using-partyline.md")
async def using_partyline_skill() -> Response:
    if not _SKILL_MD.is_file():
        raise HTTPException(status_code=404, detail="skill not published in this build")
    return Response(
        _SKILL_MD.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


@app.middleware("http")
async def _request_logging(request, call_next):
    request_id = request.headers.get("x-request-id") or uuid4().hex[:12]
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.exception(
            "request failed request_id=%s method=%s path=%s elapsed_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000
    status = response.status_code
    level = logging.INFO if status < 500 else logging.ERROR
    if elapsed_ms >= _slow_request_ms:
        log.warning(
            "slow request request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f threshold_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            status,
            elapsed_ms,
            _slow_request_ms,
        )
    log.log(
        level,
        "request completed request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
        request_id,
        request.method,
        request.url.path,
        status,
        elapsed_ms,
    )
    response.headers["x-request-id"] = request_id
    return response


@app.middleware("http")
async def _mcp_trailing_slash(request, call_next):
    # The Starlette mount serves the MCP endpoint at /mcp/ only; a bare POST /mcp would
    # 405 (and a 307 redirect wouldn't replay the POST body in most clients). Rewrite so
    # the documented URL <app>/mcp works for every MCP client.
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
    return await call_next(request)


class _SpaStatic(StaticFiles):
    """StaticFiles that stamps cache headers so a redeploy is picked up without a manual
    hard reload. Vite emits content-hashed asset filenames (assets/index-<hash>.js), so
    those are immutable and cached for a year; index.html references the current hashes
    and MUST always be revalidated (no-cache), or the browser serves a stale SPA that
    points at old bundles after every deploy."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif path.startswith("assets/") or "/assets/" in path:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


# Static SPA (built into client/dist). Mounted last so /api and /mcp win.
_spa = Path(__file__).resolve().parent.parent / "client" / "dist"
if _spa.is_dir():
    app.mount("/", _SpaStatic(directory=str(_spa), html=True), name="spa")
else:
    log.info("No built SPA at %s; run `npm --prefix client run build`.", _spa)
