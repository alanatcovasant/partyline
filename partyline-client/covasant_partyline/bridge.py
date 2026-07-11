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
import logging
import sys
import time
from contextlib import AsyncExitStack

from databricks.sdk.core import Config

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("partyline.bridge")

# Diagnostics thresholds (seconds)
_LOCK_WARN_SECS = 5.0
_CALL_WARN_SECS = 10.0
# Hard wall-clock ceiling for one proxied operation (connect + tool call, incl. one
# reconnect retry). The op runs in a shielded task and is ABANDONED past this deadline:
# asyncio.wait_for alone is not enough because an anyio stream read inside the mcp session
# can swallow the cancellation on a half-open socket (app mid-restart), which is exactly
# how a single tool call was observed hanging the client's full ~1800s idle timeout.
_CALL_TIMEOUT_SECS = 120.0
# Never wait on the per-remote lock longer than this. The previous unbounded acquire meant
# one wedged call blocked EVERY later call until the client's ~1800s idle timeout. Slightly
# above _CALL_TIMEOUT_SECS so a legitimately in-flight call can finish and release first.
_LOCK_ACQUIRE_TIMEOUT_SECS = 150.0
# Bound the connect/initialize handshake so a cold or mid-restart app (Databricks Apps
# scale to zero) can't stall the bridge forever — without this, a hung initialize() blocks
# until the MCP client's own idle timeout (~30min) and reads as "Connection closed".
_CONNECT_TIMEOUT_SECS = 30.0


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
        log.info("Connecting to %s ...", self._url)
        t0 = time.monotonic()
        await self.close()
        stack = AsyncExitStack()
        headers = _auth_headers(self._cfg)  # fresh token every (re)connect
        log.debug("Auth headers resolved (auth_type=%s)", getattr(self._cfg, "auth_type", "unknown"))
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(self._url, headers=headers))
            session = await stack.enter_async_context(ClientSession(read, write))
            # Only the initialize() RPC is timeout-wrapped: it awaits a server response
            # (where a cold app stalls) but doesn't own the streams' anyio cancel scopes,
            # so cancelling it is safe. The transport entry above relies on httpx's own
            # connect timeout. On failure we close the stack from *this* task — the one
            # that entered the contexts — to avoid anyio's cross-task-exit error.
            init = await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT_SECS)
        except BaseException as exc:
            try:
                await stack.aclose()
            except Exception:
                log.warning("Error cleaning up failed connect", exc_info=True)
            if isinstance(exc, asyncio.TimeoutError):
                log.error("Connect TIMEOUT after %.0fs to %s", _CONNECT_TIMEOUT_SECS, self._url)
            raise
        # Capture the upstream server's how-to text so the proxy can advertise it to the
        # agent's MCP client (FR: teach any client how to use the line, no skill needed).
        self.instructions = getattr(init, "instructions", None) or self.instructions
        self._stack, self._session = stack, session
        elapsed = time.monotonic() - t0
        log.info("Connected in %.2fs", elapsed)
        return session

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                log.warning("Error closing remote session", exc_info=True)
        self._stack = self._session = None

    async def call(self, op, label: str = "?"):
        """Run ``op(session)``, reconnecting with fresh credentials on failure.

        Hard-bounded end to end: a call can block at most _LOCK_ACQUIRE_TIMEOUT_SECS
        (waiting its turn) plus _CALL_TIMEOUT_SECS (the op itself) — both well under the
        MCP client's ~1800s idle timeout. A wedged remote therefore surfaces as a fast
        error the client can reconnect from, instead of a silent 30-minute hang.
        """
        log.debug("[%s] acquiring lock ...", label)
        t_wait = time.monotonic()
        try:
            # Bounded acquire (was unbounded): if a prior call wedged the connection, fail
            # fast so the client reconnects rather than queueing behind it until 1800s.
            await asyncio.wait_for(self._lock.acquire(), timeout=_LOCK_ACQUIRE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            log.error(
                "[%s] bridge busy >%.0fs (prior call wedged) — failing fast to trigger reconnect",
                label,
                _LOCK_ACQUIRE_TIMEOUT_SECS,
            )
            raise RuntimeError(
                f"partyline bridge busy for >{_LOCK_ACQUIRE_TIMEOUT_SECS:.0f}s; the remote app "
                "may be restarting — please retry."
            )
        lock_wait = time.monotonic() - t_wait
        if lock_wait > _LOCK_WARN_SECS:
            log.warning("[%s] lock acquired after %.2fs", label, lock_wait)
        else:
            log.debug("[%s] lock acquired in %.3fs", label, lock_wait)

        try:
            return await self._call_bounded(op, label)
        finally:
            self._lock.release()

    async def _call_bounded(self, op, label: str):
        """Attempt ``op`` (reconnecting once on error) under a hard wall-clock deadline.

        The attempt runs in its own task and is ``shield``-ed: if the deadline fires we
        ABANDON that (possibly cancellation-deaf) task rather than awaiting it, drop the
        session so the next call rebuilds a fresh transport, and raise. This is the part
        plain ``wait_for(op, ...)`` could not guarantee — an anyio read on a half-open
        socket can ignore the cancellation and keep the coroutine alive indefinitely.
        """
        t0 = time.monotonic()

        async def _attempt():
            try:
                session = self._session or await self._connect()
                return await op(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[%s] error after %.2fs (%s: %s) — reconnecting once",
                            label, time.monotonic() - t0, type(exc).__name__, exc)
                session = await self._connect()
                return await op(session)

        task = asyncio.ensure_future(_attempt())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=_CALL_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            log.error(
                "[%s] HARD DEADLINE %.0fs exceeded (remote wedged) — dropping session for reconnect",
                label,
                _CALL_TIMEOUT_SECS,
            )
            # Best-effort cancel and detach: do NOT await it (it may be ignoring the
            # cancel). Drop only the session ref so the NEXT call reconnects into a fresh
            # transport; _connect()'s close() reaps the old (leaked) stack then, guarded.
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._session = None
            raise
        elapsed = time.monotonic() - t0
        if elapsed > _CALL_WARN_SECS:
            log.warning("[%s] slow call: %.2fs", label, elapsed)
        else:
            log.debug("[%s] completed in %.2fs", label, elapsed)
        return result


async def _serve(cfg: Config, mcp_url: str) -> None:
    remote = _Remote(cfg, mcp_url)
    # Try to connect eagerly to surface misconfiguration early and to capture the upstream
    # instructions — but do NOT die if it fails. When the bridge (re)spawns while the app
    # is mid-restart, a fatal startup connect would exit the process, so the MCP client
    # burns a 30s connect-timeout and retries repeatedly. Serving regardless lets the
    # bridge stay up and reconnect lazily on the first real call once the app is back.
    try:
        await remote.call(lambda s: s.list_tools(), label="startup/list_tools")
    except Exception:
        log.warning(
            "Startup connect failed (app may be starting/restarting) — serving anyway; "
            "will connect on the first tool call.",
            exc_info=True,
        )
    # Forward the upstream server's instructions so the agent's client sees the how-to.
    proxy = Server("covasant-partyline-bridge", instructions=remote.instructions)

    @proxy.list_tools()
    async def list_tools():
        return (await remote.call(lambda s: s.list_tools(), label="list_tools")).tools

    @proxy.call_tool()
    async def call_tool(name: str, arguments: dict):
        log.info("-> call_tool(%s) args=%s", name, list(arguments.keys()) if arguments else [])
        result = await remote.call(lambda s: s.call_tool(name, arguments), label=f"call_tool/{name}")
        log.info("<- call_tool(%s) OK", name)
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
    log.info("Auth type: %s", getattr(cfg, "auth_type", "unknown"))
    _auth_headers(cfg)  # fail fast on PAT/missing credentials before serving
    mcp_url = _resolve_mcp_url(cfg, args.url, args.app_name)
    log.info("Bridge starting -> %s", mcp_url)
    asyncio.run(_serve(cfg, mcp_url))


if __name__ == "__main__":
    main()
