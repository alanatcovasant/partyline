"""Local MCP bridge (PRD FR11 / §10) — stdio <-> HTTP proxy to the Partyline App.

Databricks platform auth (OAuth2 M2M, service principals, token refresh) is awkward for an
agent to handle inline. This bridge runs as a subprocess spawned by the MCP client: it
speaks MCP stdio to the agent and transparently proxies every request to the remote App's
`/mcp` (Streamable HTTP) endpoint, injecting a Databricks bearer token resolved by the
`databricks-sdk` ``Config`` (env vars, CLI profile, or service principal credentials).

The agent's only setup is a `.mcp.json` entry — no Databricks SDK knowledge required:

    {
      "mcpServers": {
        "partyline": {
          "command": "python",
          "args": ["-m", "covasant_partyline.bridge", "--app-name", "mcp-covasant-partyline"],
          "env": {"DATABRICKS_CLIENT_ID": "...", "DATABRICKS_CLIENT_SECRET": "...",
                  "DATABRICKS_HOST": "https://<workspace>.databricks.net"}
        }
      }
    }

Pass either --url (full https URL of the app's /mcp endpoint) or --app-name (resolved to
the app URL via the SDK).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from contextlib import AsyncExitStack

from databricks.sdk.core import Config

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# Diagnostics thresholds (seconds)
_LOCK_WARN_SECS = 5.0
_CALL_WARN_SECS = 10.0
_CALL_TIMEOUT_SECS = 120.0


def _log(msg: str) -> None:
    print(f"partyline bridge: {msg}", file=sys.stderr, flush=True)


def _auth_headers(cfg: Config) -> dict[str, str]:
    """Resolve Databricks OAuth auth headers (Authorization: Bearer ...).

    Databricks Apps do **not** accept personal access tokens — they authenticate callers
    via OAuth. So we require an OAuth strategy (M2M service principal or U2M) and refuse a
    PAT outright, rather than sending a token the app will reject with 401.
    """
    auth_type = (getattr(cfg, "auth_type", "") or "").lower()
    if auth_type == "pat" or cfg.token:
        raise SystemExit(
            "Databricks Apps do not accept PATs. Use OAuth: set DATABRICKS_CLIENT_ID + "
            "DATABRICKS_CLIENT_SECRET (M2M) or run `databricks auth login` (U2M). "
            "Unset DATABRICKS_TOKEN."
        )
    headers = cfg.authenticate()  # dict of headers for the current credential strategy
    if "Authorization" not in headers:
        raise SystemExit(
            "Could not resolve Databricks OAuth credentials. Set DATABRICKS_HOST and "
            "DATABRICKS_CLIENT_ID/SECRET, or authenticate a U2M OAuth profile."
        )
    return dict(headers)


def _resolve_mcp_url(cfg: Config, url: str | None, app_name: str | None) -> str:
    if url:
        return url
    if not app_name:
        raise SystemExit("Provide --url or --app-name.")
    from databricks.sdk import WorkspaceClient

    app = WorkspaceClient(config=cfg).apps.get(name=app_name)
    if not app.url:
        raise SystemExit(f"App '{app_name}' has no URL yet (is it deployed and running?).")
    return app.url.rstrip("/") + "/mcp"


class _Remote:
    """Lazily-connected remote MCP session that reconnects with fresh credentials.

    The workspace OAuth token expires after ~1h, so a session opened once at bridge
    start silently dies mid-session (observed in the field). Every proxied call goes
    through ``call()``, which (re)connects on demand and retries once on failure —
    minting fresh auth headers from the SDK ``Config`` on each (re)connect.
    """

    def __init__(self, cfg: Config, mcp_url: str) -> None:
        self._cfg = cfg
        self._url = mcp_url
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._lock = asyncio.Lock()
        self.instructions: str | None = None  # server how-to, captured at initialize()

    async def _connect(self) -> ClientSession:
        _log("connecting...")
        t0 = time.monotonic()
        await self.close()
        stack = AsyncExitStack()
        headers = _auth_headers(self._cfg)  # fresh token every (re)connect
        read, write, _ = await stack.enter_async_context(streamablehttp_client(self._url, headers=headers))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        # Capture the upstream server's how-to text so the proxy can advertise it to the
        # agent's MCP client (FR: teach any client how to use the line, no skill needed).
        self.instructions = getattr(init, "instructions", None) or self.instructions
        self._stack, self._session = stack, session
        elapsed = time.monotonic() - t0
        _log(f"connected in {elapsed:.2f}s")
        return session

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = self._session = None

    async def call(self, op, label: str = "?"):
        """Run ``op(session)``, reconnecting with fresh credentials on failure."""
        t_wait = time.monotonic()
        lock_acquired = False
        try:
            lock_acquired = await asyncio.wait_for(self._lock.acquire(), timeout=_LOCK_WARN_SECS)
        except asyncio.TimeoutError:
            _log(f"[{label}] waiting for lock ({_LOCK_WARN_SECS:.0f}s elapsed, another call in progress)")
            await self._lock.acquire()
            lock_acquired = True
        lock_wait = time.monotonic() - t_wait
        if lock_wait > _LOCK_WARN_SECS:
            _log(f"[{label}] lock acquired after {lock_wait:.2f}s")

        t0 = time.monotonic()
        try:
            try:
                session = self._session or await self._connect()
                result = await asyncio.wait_for(op(session), timeout=_CALL_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                _log(f"[{label}] TIMEOUT after {elapsed:.1f}s — forcing reconnect")
                self._session = None
                raise
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                elapsed = time.monotonic() - t0
                _log(f"[{label}] error after {elapsed:.2f}s ({type(exc).__name__}: {exc}) — reconnecting")
                session = await self._connect()
                result = await asyncio.wait_for(op(session), timeout=_CALL_TIMEOUT_SECS)
            elapsed = time.monotonic() - t0
            if elapsed > _CALL_WARN_SECS:
                _log(f"[{label}] slow call: {elapsed:.2f}s")
            return result
        finally:
            if lock_acquired:
                self._lock.release()


async def _serve(cfg: Config, mcp_url: str) -> None:
    remote = _Remote(cfg, mcp_url)
    # Connect eagerly so misconfiguration fails at startup, not at the first tool call.
    # This also populates remote.instructions from the upstream initialize().
    await remote.call(lambda s: s.list_tools(), label="startup/list_tools")
    # Forward the upstream server's instructions so the agent's client sees the how-to.
    proxy = Server("covasant-partyline-bridge", instructions=remote.instructions)

    @proxy.list_tools()
    async def list_tools():
        return (await remote.call(lambda s: s.list_tools(), label="list_tools")).tools

    @proxy.call_tool()
    async def call_tool(name: str, arguments: dict):
        _log(f"-> call_tool({name})")
        result = await remote.call(lambda s: s.call_tool(name, arguments), label=f"call_tool/{name}")
        _log(f"<- call_tool({name}) OK")
        # Forward content plus any structured payload the tool returned.
        return result.content, getattr(result, "structuredContent", None)

    @proxy.list_prompts()
    async def list_prompts():
        return (await remote.call(lambda s: s.list_prompts(), label="list_prompts")).prompts

    @proxy.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None):
        return await remote.call(lambda s: s.get_prompt(name, arguments), label=f"get_prompt/{name}")

    opts = proxy.create_initialization_options()
    try:
        async with stdio_server() as (r, w):
            await proxy.run(r, w, opts)
    finally:
        await remote.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="covasant_partyline.bridge", description=__doc__.splitlines()[0])
    p.add_argument("--url", help="Full https URL of the app's /mcp endpoint")
    p.add_argument("--app-name", help="Databricks App name (resolved to URL via SDK)")
    p.add_argument("--host", help="Databricks workspace host (overrides DATABRICKS_HOST)")
    args = p.parse_args(argv)

    cfg = Config(host=args.host) if args.host else Config()
    _auth_headers(cfg)  # fail fast on PAT/missing credentials before serving
    mcp_url = _resolve_mcp_url(cfg, args.url, args.app_name)
    print(f"partyline bridge -> {mcp_url}", file=sys.stderr)
    asyncio.run(_serve(cfg, mcp_url))


if __name__ == "__main__":
    main()
