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

Observability
-------------
All logs go to stderr (captured by the MCP client, e.g. Claude Code's mcp-logs). Verbosity
is env-tunable via ``PARTYLINE_BRIDGE_LOG_LEVEL`` (DEBUG|INFO|WARNING|ERROR; default INFO).
Two invariants hold regardless of level:

  * **Every failure is loud.** Connect faults, per-call errors, the hard-deadline abort, a
    wedged-lock timeout, and even the *later* fate of an abandoned task are logged at
    WARNING/ERROR/CRITICAL with the exception type and message (full traceback at DEBUG).
  * **Every proxied call is traceable.** Each invocation gets a monotonic id, so the
    interleaved log lines for one call share a ``<label>#<n>`` tag end to end.

Set ``PARTYLINE_BRIDGE_LOG_LEVEL=DEBUG`` to also surface the underlying httpx/mcp wire
logs (handshake, 401s, stream reads) when tracking down a stubborn failure.

Two more diagnosis aids:
  * The startup banner logs the build version AND the module file path, so it is obvious
    which installed build is serving a given agent (catches version/deploy skew).
  * A local ``partyline_bridge_info`` MCP tool (handled here, not proxied) lets the agent
    query that same identity + live health at runtime — it works even when the upstream
    line is unreachable, which is exactly when you want it.

Token freshness is a *check*, not a guess, when possible: the token's real expiry is read
from the SDK (``Config.oauth_token().expiry``) and the connection is refreshed before it
lapses; the mint-age heuristic is only the fallback for auth strategies that expose no
expiry.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import itertools
import json
import logging
import os
import platform
import sys
import time
from contextlib import AsyncExitStack

from databricks.sdk.core import Config

from mcp import types as mcp_types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_LOG_LEVEL = os.getenv("PARTYLINE_BRIDGE_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("partyline.bridge")
# When deep-tracing, surface the transport's own logs too — this is where 401s, connection
# resets, and stalled stream reads actually show up.
if log.isEnabledFor(logging.DEBUG):
    for _noisy in ("httpx", "httpcore", "mcp"):
        logging.getLogger(_noisy).setLevel(logging.DEBUG)

# Monotonic per-call id: every proxied invocation's log lines share a "<label>#<n>" tag so
# an operation can be followed end to end even when calls interleave.
_call_seq = itertools.count(1)

# Process start (monotonic) for uptime reporting in the startup banner and bridge_info tool.
_STARTED_AT = time.monotonic()

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
# Fast-fail ceilings for the SYNCHRONOUS path (reads + claim_resource). A stale-token or
# cold-app read fails in seconds so the agent retries cheaply, instead of riding the long
# fire-and-forget-era 120s deadline. Sends no longer use this path at all.
_SYNC_CALL_TIMEOUT_SECS = 8.0
_SYNC_LOCK_ACQUIRE_TIMEOUT_SECS = 10.0
# Bound the connect/initialize handshake so a cold or mid-restart app (Databricks Apps
# scale to zero) can't stall the bridge forever — without this, a hung initialize() blocks
# until the MCP client's own idle timeout (~30min) and reads as "Connection closed".
_CONNECT_TIMEOUT_SECS = 30.0
# Proactively refresh the connection (re-mint the OAuth token) once it reaches this age.
# Databricks workspace OAuth tokens expire ~1h after minting, and this bridge bakes the
# token into the streamable-http transport ONCE at connect time — it is never refreshed in
# place. A call that arrives after the token has expired rides a stale, half-open session:
# the observed multi-minute hang (only broken by the hard deadline below). Reconnecting
# under this ceiling means every call uses a freshly-minted token — the same "always
# re-authenticate" guarantee Aura's per-request bridge (utils/mcp_bridge.py) gets for free.
_TOKEN_MAX_AGE_SECS = 40 * 60  # 2400s, comfortably under the ~1h token lifetime
# Preferred over the age heuristic when the SDK exposes the token's real expiry: reconnect
# once the baked token is within this margin of its ACTUAL expiry, so a call never rides a
# token that has (or is about to) expire — a check, not a guess.
_TOKEN_EXPIRY_SAFETY_SECS = 120.0


def _bridge_version() -> str:
    """Best-effort installed version, for the startup banner (diagnose build skew)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("covasant-partyline")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        return "unknown"


def _trace_tb() -> bool:
    """Include a full traceback only when debugging — keeps routine retry logs terse
    while still capturing everything on DEBUG."""
    return log.isEnabledFor(logging.DEBUG)


def _bridge_info(remote: "_Remote | None" = None) -> dict:
    """Identify THIS bridge build/process and (if given) its live connection state.

    Surfaced two ways so it's easy to tell which build is actually running in an agent's
    environment: logged in the startup banner, and callable at runtime via the local
    ``partyline_bridge_info`` MCP tool. ``module_path`` + ``version`` pin down exactly
    which installed file is serving the session (catches build/version skew)."""
    info: dict = {
        "component": "covasant-partyline MCP bridge",
        "version": _bridge_version(),
        "module_path": os.path.abspath(__file__),
        "pid": os.getpid(),
        "python": platform.python_version(),
        "host": platform.node(),
        "log_level": _LOG_LEVEL,
        "uptime_secs": round(time.monotonic() - _STARTED_AT, 1),
    }
    if remote is not None:
        info["mcp_url"] = remote._url
        info["auth_type"] = getattr(remote._cfg, "auth_type", None)
        info["connected"] = remote._session is not None
        if remote._connected_at is not None:
            info["connection_age_secs"] = round(time.monotonic() - remote._connected_at, 1)
        if remote._token_expiry is not None:
            remaining = (remote._token_expiry - dt.datetime.now(dt.timezone.utc)).total_seconds()
            info["token_expiry"] = remote._token_expiry.isoformat()
            info["token_valid_for_secs"] = round(remaining, 1)
            info["token_valid"] = remaining > 0
    return info


# Local (non-proxied) tool the agent can call to identify the running bridge build + health.
_BRIDGE_INFO_TOOL = mcp_types.Tool(
    name="partyline_bridge_info",
    description=(
        "Diagnostics for the local Partyline MCP bridge process serving THIS session: build "
        "version, module file path, pid, uptime, upstream URL, auth type, connection state, "
        "and token validity. Handled locally by the bridge (not proxied to the app). Use it "
        "to confirm which bridge build is running in this environment and whether it's healthy."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)


_SEND_VERBS = {"post_message", "reply", "ack_checkpoint", "register_agent", "release_resource"}
_SYNC_VERBS = {"get_unread", "get_since", "get_conversation", "search_messages",
               "list_agents", "list_claims", "claim_resource"}
_LOCAL_VERBS = {_BRIDGE_INFO_TOOL.name, "check_send"}


def dispatch_kind(name: str) -> str:
    """Pure classifier: which of the four dispatch tiers a tool name belongs to.

    - "local": handled entirely in-process (bridge_info, check_send) — never proxied.
    - "send":  fire-and-forget — enqueued to the local outbox, returns {queued, ticket}.
    - "sync":  reads/claim — proxied with the fast-fail (sync=True) ceilings.
    - "proxy": everything else — proxied with the original long-ceiling behavior.
    """
    if name in _LOCAL_VERBS:
        return "local"
    if name in _SEND_VERBS:
        return "send"
    if name in _SYNC_VERBS:
        return "sync"
    return "proxy"


_CHECK_SEND_TOOL = mcp_types.Tool(
    name="check_send",
    description=(
        "Check the delivery status of a fire-and-forget send by its ticket (the uuid "
        "returned by post_message/reply/ack_checkpoint/register_agent/release_resource). "
        "Returns pending | sent {message_id} | failed {spooled_to} | unknown. Handled "
        "locally by the bridge — instant, works even when the app is unreachable."
    ),
    inputSchema={
        "type": "object",
        "properties": {"ticket": {"type": "string"}},
        "required": ["ticket"], "additionalProperties": False,
    },
)


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
    try:
        headers = cfg.authenticate()  # dict of headers for the current credential strategy
    except Exception as exc:
        # authenticate() reaches out to refresh U2M tokens / mint M2M tokens; a failure here
        # (expired refresh token, bad client secret, network) is a hard blocker — say so.
        log.error(
            "Databricks auth FAILED (auth_type=%s): %s: %s",
            auth_type or "unknown", type(exc).__name__, exc, exc_info=True,
        )
        raise
    if "Authorization" not in headers:
        raise SystemExit(
            "Could not resolve Databricks OAuth credentials. Set DATABRICKS_HOST and "
            "DATABRICKS_CLIENT_ID/SECRET, or authenticate a U2M OAuth profile."
        )
    return dict(headers)


def _resolve_mcp_url(cfg: Config, url: str | None, app_name: str | None) -> str:
    if url:
        log.info("Using explicit --url endpoint: %s", url)
        return url
    if not app_name:
        raise SystemExit("Provide --url or --app-name.")
    log.info("Resolving app URL for --app-name=%s via the Databricks SDK ...", app_name)
    from databricks.sdk import WorkspaceClient

    try:
        app = WorkspaceClient(config=cfg).apps.get(name=app_name)
    except Exception as exc:
        log.error(
            "Failed to resolve app '%s' via SDK (%s: %s) — check the name, the profile's "
            "workspace, and that you can see the app.",
            app_name, type(exc).__name__, exc, exc_info=True,
        )
        raise
    if not app.url:
        raise SystemExit(f"App '{app_name}' has no URL yet (is it deployed and running?).")
    resolved = app.url.rstrip("/") + "/mcp"
    log.info("Resolved app '%s' -> %s", app_name, resolved)
    return resolved


class _Remote:
    """Lazily-connected remote MCP session that reconnects with fresh credentials.

    The workspace OAuth token expires after ~1h, so a session opened once at bridge
    start silently dies mid-session (observed in the field). Two mechanisms keep the
    token fresh: every proxied call (re)connects on demand and retries once on failure
    (minting fresh auth headers from the SDK ``Config`` on each (re)connect), AND a call
    that finds the current connection older than ``_TOKEN_MAX_AGE_SECS`` reconnects
    *before* using it — so a call never rides a token that has since expired.
    """

    def __init__(self, cfg: Config, mcp_url: str) -> None:
        self._cfg = cfg
        self._url = mcp_url
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connected_at: float | None = None  # monotonic time the token was last minted
        self._token_expiry: dt.datetime | None = None  # real expiry of the baked token, if known
        self._lock = asyncio.Lock()
        self.instructions: str | None = None  # server how-to, captured at initialize()

    def _peek_token_expiry(self, tag: str) -> "dt.datetime | None":
        """Read the REAL expiry of the OAuth token we just baked into the transport, so we
        refresh on actual expiry instead of a fixed age guess. Returns a tz-aware UTC
        datetime, or None when the auth strategy doesn't expose one (then we fall back to
        the age heuristic). Best-effort: never fatal."""
        try:
            tok = self._cfg.oauth_token()
        except Exception as exc:
            log.debug("[%s] token expiry unavailable (%s: %s) — will use age heuristic",
                      tag, type(exc).__name__, exc)
            return None
        expiry = getattr(tok, "expiry", None)
        if expiry is None:
            return None
        if expiry.tzinfo is None:  # normalize to tz-aware UTC for comparison
            expiry = expiry.replace(tzinfo=dt.timezone.utc)
        remaining = (expiry - dt.datetime.now(dt.timezone.utc)).total_seconds()
        log.debug("[%s] token valid for %.0fs (until %s)", tag, remaining, expiry.isoformat())
        return expiry

    def _token_stale(self) -> bool:
        """True if the token baked into the live connection has expired or is about to —
        so a call never rides a dead token (the multi-minute-hang trigger).

        Prefers the token's REAL expiry (verify) and falls back to a mint-age heuristic
        (guess) only when the SDK doesn't expose an expiry for the active auth strategy."""
        if self._token_expiry is not None:
            remaining = (self._token_expiry - dt.datetime.now(dt.timezone.utc)).total_seconds()
            return remaining <= _TOKEN_EXPIRY_SAFETY_SECS
        if self._connected_at is None:
            return False
        return (time.monotonic() - self._connected_at) > _TOKEN_MAX_AGE_SECS

    def _staleness_reason(self) -> str:
        """Human-readable why-we-reconnected, for the log line."""
        if self._token_expiry is not None:
            remaining = (self._token_expiry - dt.datetime.now(dt.timezone.utc)).total_seconds()
            return f"expires in {remaining:.0f}s (<= {_TOKEN_EXPIRY_SAFETY_SECS:.0f}s margin)"
        if self._connected_at is not None:
            return f"minted {time.monotonic() - self._connected_at:.0f}s ago (> {_TOKEN_MAX_AGE_SECS:.0f}s)"
        return "no live token"

    @staticmethod
    def _log_abandoned(task: "asyncio.Task", tag: str, t0: float) -> None:
        """Loud post-mortem for a task abandoned at the hard deadline.

        Previously the callback discarded ``task.exception()`` purely to silence asyncio's
        "exception never retrieved" warning — hiding the single most useful clue: what the
        wedged call was actually stuck on and how it ended. Now we log that outcome.
        """
        elapsed = time.monotonic() - t0
        if task.cancelled():
            log.error("[%s] abandoned task confirmed CANCELLED %.1fs after start", tag, elapsed)
            return
        exc = task.exception()  # retrieved => no "exception never retrieved" warning
        if exc is not None:
            log.error(
                "[%s] abandoned task later FAILED %.1fs after start (%s: %s)",
                tag, elapsed, type(exc).__name__, exc,
            )
        else:
            log.error(
                "[%s] abandoned task later COMPLETED %.1fs after start — result discarded, "
                "the deadline had already fired (consider raising _CALL_TIMEOUT_SECS if this recurs)",
                tag, elapsed,
            )

    async def _connect(self, tag: str = "connect") -> ClientSession:
        log.info("[%s] connecting to %s ...", tag, self._url)
        t0 = time.monotonic()
        await self.close()
        stack = AsyncExitStack()
        headers = _auth_headers(self._cfg)  # fresh token every (re)connect
        log.debug("[%s] auth headers resolved (auth_type=%s)", tag, getattr(self._cfg, "auth_type", "unknown"))
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
                log.warning("[%s] error cleaning up failed connect", tag, exc_info=True)
            if isinstance(exc, asyncio.TimeoutError):
                log.error(
                    "[%s] connect TIMEOUT after %.0fs to %s — app cold-starting/restarting, "
                    "or the network is blocked", tag, _CONNECT_TIMEOUT_SECS, self._url,
                )
            elif isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                pass  # not a connect fault (shutdown/cancellation) — let it propagate quietly
            else:
                log.error(
                    "[%s] connect FAILED to %s (%s: %s)",
                    tag, self._url, type(exc).__name__, exc, exc_info=True,
                )
            raise
        # Capture the upstream server's how-to text so the proxy can advertise it to the
        # agent's MCP client (FR: teach any client how to use the line, no skill needed).
        self.instructions = getattr(init, "instructions", None) or self.instructions
        self._stack, self._session = stack, session
        self._connected_at = time.monotonic()  # start the token-age clock (heuristic fallback)
        self._token_expiry = self._peek_token_expiry(tag)  # real expiry when the SDK exposes it
        log.info("[%s] connected in %.2fs", tag, time.monotonic() - t0)
        return session

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                log.warning("Error closing remote session", exc_info=True)
        self._stack = self._session = None
        self._connected_at = None
        self._token_expiry = None

    async def call(self, op, label: str = "?", sync: bool = False):
        """Run ``op(session)``, reconnecting with fresh credentials on failure.

        Hard-bounded end to end: a call can block at most the lock-acquire ceiling
        (waiting its turn) plus the call ceiling (the op itself) — both well under the
        MCP client's ~1800s idle timeout. A wedged remote therefore surfaces as a fast
        error the client can reconnect from, instead of a silent 30-minute hang.

        ``sync=True`` selects the short fast-fail ceilings (_SYNC_CALL_TIMEOUT_SECS /
        _SYNC_LOCK_ACQUIRE_TIMEOUT_SECS) for the synchronous read/claim path, so those
        calls fail in seconds rather than riding the long ceilings. Default ``sync=False``
        preserves the existing long-ceiling behavior for all other callers.
        """
        tag = f"{label}#{next(_call_seq)}"
        lock_timeout = _SYNC_LOCK_ACQUIRE_TIMEOUT_SECS if sync else _LOCK_ACQUIRE_TIMEOUT_SECS
        call_timeout = _SYNC_CALL_TIMEOUT_SECS if sync else _CALL_TIMEOUT_SECS
        log.debug("[%s] acquiring lock (sync=%s) ...", tag, sync)
        t_wait = time.monotonic()
        try:
            # Bounded acquire (was unbounded): if a prior call wedged the connection, fail
            # fast so the client reconnects rather than queueing behind it until 1800s.
            await asyncio.wait_for(self._lock.acquire(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            log.error(
                "[%s] bridge busy >%.0fs (a prior call is wedged holding the lock) — failing "
                "fast to trigger a client reconnect", tag, lock_timeout,
            )
            raise RuntimeError(
                f"partyline bridge busy for >{lock_timeout:.0f}s; the remote app "
                "may be restarting — please retry."
            )
        lock_wait = time.monotonic() - t_wait
        if lock_wait > _LOCK_WARN_SECS:
            log.warning("[%s] lock acquired after %.2fs (contention)", tag, lock_wait)
        else:
            log.debug("[%s] lock acquired in %.3fs", tag, lock_wait)

        try:
            return await self._call_bounded(op, tag, call_timeout)
        finally:
            self._lock.release()

    async def _call_bounded(self, op, tag: str, call_timeout: float = _CALL_TIMEOUT_SECS):
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
                session = self._session
                if session is not None and self._token_stale():
                    log.info(
                        "[%s] token stale (%s) — reconnecting to refresh it before use",
                        tag, self._staleness_reason(),
                    )
                    session = await self._connect(tag)
                elif session is None:
                    log.debug("[%s] no live session — connecting", tag)
                    session = await self._connect(tag)
                return await op(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # First failure is usually a stale/dropped session — reconnect and retry
                # once. Transient by nature, so log at WARNING (traceback only on DEBUG).
                log.warning(
                    "[%s] op failed after %.2fs (%s: %s) — reconnecting once and retrying",
                    tag, time.monotonic() - t0, type(exc).__name__, exc, exc_info=_trace_tb(),
                )
                session = await self._connect(tag)
                # If the retry also fails it propagates out; the proxy boundary logs the
                # definitive ERROR (with the tool name/args), so we don't double-log here.
                return await op(session)

        task = asyncio.ensure_future(_attempt())
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=call_timeout)
        except asyncio.TimeoutError:
            log.error(
                "[%s] HARD DEADLINE %.0fs exceeded (remote wedged) — abandoning the task and "
                "dropping the session so the next call reconnects", tag, call_timeout,
            )
            # Best-effort cancel and detach: do NOT await it (it may be ignoring the
            # cancel). Drop the session/age so the NEXT call reconnects into a fresh
            # transport; _connect()'s close() reaps the old (leaked) stack then, guarded.
            # The done-callback logs the abandoned task's eventual fate (never silent).
            task.cancel()
            task.add_done_callback(lambda t: self._log_abandoned(t, tag, t0))
            self._session = None
            self._connected_at = None
            raise
        elapsed = time.monotonic() - t0
        if elapsed > _CALL_WARN_SECS:
            log.warning("[%s] slow call: %.2fs", tag, elapsed)
        else:
            log.debug("[%s] completed in %.2fs", tag, elapsed)
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
        log.info("Startup connect OK — upstream reachable, serving proxy")
    except Exception:
        log.warning(
            "Startup connect failed (app may be starting/restarting) — serving anyway; "
            "will connect on the first tool call.",
            exc_info=True,
        )

    async def _proxied(desc: str, op, label: str, sync: bool = False):
        """Run a proxied op and make any failure loud at the boundary — one ERROR record
        naming the operation, with a traceback, before it reaches the MCP framework.

        ``sync=True`` forwards to ``remote.call``'s fast-fail ceilings (reads/claim);
        default ``False`` preserves the original long-ceiling behavior."""
        try:
            return await remote.call(op, label=label, sync=sync)
        except Exception as exc:
            log.error("%s FAILED (%s: %s)", desc, type(exc).__name__, exc, exc_info=True)
            raise

    def _drain_spill_prefix(sender) -> str:
        """One-line warning prepended to the next tool result when sends have spooled."""
        n = sender.spill_flag.get("pending", 0)
        if not n:
            return ""
        path = sender.spill_flag.get("path")
        sender.spill_flag["pending"] = 0
        return f"⚠ {n} partyline message(s) spooled to {path} (app unreachable)"

    # Forward the upstream server's instructions so the agent's client sees the how-to.
    proxy = Server("covasant-partyline-bridge", instructions=remote.instructions)

    @proxy.list_tools()
    async def list_tools():
        # Always advertise the local bridge_info tool — even when the upstream list fails,
        # so an agent can still diagnose the bridge exactly when the line is unreachable.
        try:
            upstream = (await remote.call(lambda s: s.list_tools(), label="list_tools")).tools
        except Exception as exc:
            log.error(
                "list_tools FAILED (%s: %s) — advertising only the local bridge tool",
                type(exc).__name__, exc, exc_info=True,
            )
            upstream = []
        return list(upstream) + [_BRIDGE_INFO_TOOL, _CHECK_SEND_TOOL]

    @proxy.call_tool()
    async def call_tool(name: str, arguments: dict):
        kind = dispatch_kind(name)
        if name == _BRIDGE_INFO_TOOL.name:
            info = _bridge_info(remote, sender=sender_ref["s"])
            return [mcp_types.TextContent(type="text", text=json.dumps(info, indent=2, default=str))], info
        if name == "check_send":
            status = tickets.get(arguments.get("ticket", ""))
            return [mcp_types.TextContent(type="text", text=json.dumps(status, default=str))], status
        if kind == "send":
            import datetime as dt
            event_time = dt.datetime.now(dt.timezone.utc).isoformat()
            ticket = sender_ref["s"].enqueue(name, dict(arguments or {}), event_time)
            out = {"queued": True, "ticket": ticket}
            prefix = _drain_spill_prefix(sender_ref["s"])
            text = (prefix + "\n" if prefix else "") + json.dumps(out)
            log.info("-> call_tool(%s) [queued ticket=%s]", name, ticket)
            return [mcp_types.TextContent(type="text", text=text)], out
        # sync reads/claim: fast-fail; everything else: normal proxy
        log.info("-> call_tool(%s) args=%s", name, list(arguments.keys()) if arguments else [])
        result = await _proxied(
            f"call_tool({name})", lambda s: s.call_tool(name, arguments), f"call_tool/{name}",
            sync=(kind == "sync"),
        )
        log.info("<- call_tool(%s) OK", name)
        # Forward content plus any structured payload the tool returned.
        return result.content, getattr(result, "structuredContent", None)

    @proxy.list_prompts()
    async def list_prompts():
        return (await _proxied("list_prompts", lambda s: s.list_prompts(), "list_prompts")).prompts

    @proxy.get_prompt()
    async def get_prompt(name: str, arguments: dict | None = None):
        return await _proxied(
            f"get_prompt({name})", lambda s: s.get_prompt(name, arguments), f"get_prompt/{name}"
        )

    opts = proxy.create_initialization_options()
    log.info("stdio proxy ready — serving MCP over stdin/stdout")
    try:
        async with stdio_server() as (r, w):
            await proxy.run(r, w, opts)
        log.info("stdio loop ended (client closed the connection) — shutting down")
    except Exception:
        log.error("stdio proxy loop crashed", exc_info=True)
        raise
    finally:
        await remote.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="covasant_partyline.bridge", description=__doc__.splitlines()[0])
    p.add_argument("--url", help="Full https URL of the app's /mcp endpoint")
    p.add_argument("--app-name", help="Databricks App name (resolved to URL via SDK)")
    p.add_argument("--host", help="Databricks workspace host (overrides DATABRICKS_HOST)")
    args = p.parse_args(argv)

    log.info(
        "partyline bridge v%s starting | module=%s | pid=%s | python=%s | host=%s | log_level=%s",
        _bridge_version(), os.path.abspath(__file__), os.getpid(),
        platform.python_version(), platform.node(), _LOG_LEVEL,
    )
    try:
        cfg = Config(host=args.host) if args.host else Config()
        log.info(
            "Databricks config: auth_type=%s host=%s",
            getattr(cfg, "auth_type", "unknown"), getattr(cfg, "host", None),
        )
        _auth_headers(cfg)  # fail fast on PAT/missing credentials before serving
        mcp_url = _resolve_mcp_url(cfg, args.url, args.app_name)
        log.info(
            "timeouts: connect=%.0fs call=%.0fs lock=%.0fs token_max_age=%.0fs",
            _CONNECT_TIMEOUT_SECS, _CALL_TIMEOUT_SECS, _LOCK_ACQUIRE_TIMEOUT_SECS, _TOKEN_MAX_AGE_SECS,
        )
        log.info("Bridge serving -> %s", mcp_url)
        asyncio.run(_serve(cfg, mcp_url))
    except SystemExit as exc:
        # Config errors raise SystemExit with a human message — echo it into the log stream
        # (not just raw interpreter stderr) so the MCP client's captured logs show the reason.
        if exc.code and not isinstance(exc.code, int):
            log.critical("Bridge cannot start: %s", exc.code)
        raise
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    except BaseException as exc:
        log.critical("Bridge FATAL (%s: %s)", type(exc).__name__, exc, exc_info=True)
        raise
    finally:
        log.info("partyline bridge stopped")


if __name__ == "__main__":
    main()
