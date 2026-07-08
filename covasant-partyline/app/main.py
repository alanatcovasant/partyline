"""Covasant Partyline — single Databricks App entrypoint.

One FastAPI process serves three surfaces:
  - /mcp   the MCP server (Streamable HTTP)  — primary agent interface
  - /api   the REST API + SSE stream          — CLI, dashboard, non-MCP consumers
  - /      the static ops SPA (if built)       — monitoring/maintenance

Startup opens the Lakebase pool, ensures the schema (FR9), and starts the LISTEN
listener. All of this is a no-op when the DB isn't configured (e.g. bare local boot),
so the app still serves /api/health for smoke checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from uuid import uuid4
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from . import db
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

        if settings.db_configured:
            await asyncio.to_thread(db.open_pool)
            if settings.auto_migrate:
                await asyncio.to_thread(db.init_schema)
            hub.start()
            log.info("Partyline ready (db=%s).", settings.pg_host)
        else:
            log.warning("DB not configured; serving health/UI only. Set PG*/ENDPOINT_NAME.")
        try:
            yield
        finally:
            await hub.stop()
            if settings.db_configured:
                await asyncio.to_thread(db.close_pool)


app = FastAPI(title="Covasant Partyline", version="0.1.0", lifespan=lifespan)
app.include_router(rest_router)
app.mount("/mcp", mcp_app)


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


# Static SPA (built into client/dist). Mounted last so /api and /mcp win.
_spa = Path(__file__).resolve().parent.parent / "client" / "dist"
if _spa.is_dir():
    app.mount("/", StaticFiles(directory=str(_spa), html=True), name="spa")
else:
    log.info("No built SPA at %s; run `npm --prefix client run build`.", _spa)
