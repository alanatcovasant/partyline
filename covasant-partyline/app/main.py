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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from . import db
from .config import settings
from .mcp_server import mcp
from .notify import hub
from .rest import router as rest_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("partyline")

# Serve the MCP endpoint exactly at /mcp (not /mcp/mcp).
mcp.settings.streamable_http_path = "/"
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The MCP session manager must run for the mounted Streamable-HTTP app to work.
    async with mcp.session_manager.run():
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
