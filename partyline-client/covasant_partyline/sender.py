"""REST sender + background drain worker for the bridge's fire-and-forget outbox.

Sends over plain urllib POST (like channel.py.post_reply), NOT the proxied MCP session —
so fire-and-forget is decoupled from the flaky proxy and mints a fresh token per attempt
(token expiry becomes a non-event). Failed messages spill to the spool file after a bounded
give-up window; nothing is ever silently dropped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request

from . import outbox

log = logging.getLogger("partyline.sender")

_BACKOFF_START = 1.0
_BACKOFF_CAP = 15.0
_GIVEUP_SECS = 120.0
_POST_TIMEOUT = 30.0


def _msg_body(args: dict, event_time) -> dict:
    body = {
        "agent_id": args.get("agent_id"),
        "message_type": args.get("message_type", "status"),
        "recipient": args.get("recipient"),
        "repo": args.get("repo"),
        "branch": args.get("branch"),
        "resource_path": args.get("resource_path"),
        "issue_ref": args.get("issue_ref"),
        "summary": args.get("summary"),
        "payload": args.get("payload"),
        "metadata": args.get("metadata") or {},
        "mentions": args.get("mentions") or [],
        "reply_to_id": args.get("reply_to_id"),
        "event_time": event_time,
    }
    return body


# verb -> (path, body builder(args, event_time))
VERB_ENDPOINTS = {
    "post_message": ("/api/messages", _msg_body),
    "reply": ("/api/messages", _msg_body),
    "ack_checkpoint": ("/api/checkpoints", lambda a, et: {
        "agent_id": a.get("agent_id"), "last_read_message_id": a.get("last_read_message_id"),
    }),
    "register_agent": ("/api/agents", lambda a, et: {
        "agent_id": a.get("agent_id"), "owner": a.get("owner"), "repo": a.get("repo"),
        "branch": a.get("branch"), "display_name": a.get("display_name"),
    }),
    "release_resource": ("/api/claims/release", lambda a, et: {
        "agent_id": a.get("agent_id"), "repo": a.get("repo"),
        "resource_path": a.get("resource_path"), "branch": a.get("branch"),
    }),
}


def send_once(base_url: str, token: str | None, verb: str, args: dict, event_time) -> dict:
    """One blocking POST for `verb`. Raises on HTTP/network error."""
    path, build = VERB_ENDPOINTS[verb]
    body = build(args, event_time)
    url = base_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_POST_TIMEOUT) as resp:
        return json.loads(resp.read() or "null")


class BackgroundSender:
    """Drains an outbox queue, delivering via send_once with backoff -> spool on give-up."""

    def __init__(self, base_url, token_fn, tickets: "outbox.TicketTable", spool_path: str,
                 agent_id: str = "?") -> None:
        self._base = base_url
        self._token_fn = token_fn          # callable -> fresh bearer per attempt
        self._tickets = tickets
        self.spool_path = spool_path
        self._agent_id = agent_id
        self._q: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self.spill_flag = {"pending": 0, "path": None}

    def enqueue(self, verb: str, args: dict, event_time) -> str:
        ticket = self._tickets.new(verb)
        self._q.put_nowait({
            "ticket": ticket, "verb": verb, "args": args,
            "event_time": event_time, "enqueued_at": time.time(),
        })
        return ticket

    async def _deliver(self, item: dict) -> None:
        t0 = time.monotonic()
        backoff = _BACKOFF_START
        attempts = 0
        last_error = ""
        while True:
            attempts += 1
            try:
                # Token acquisition shares the fate of the send itself: self._token_fn()
                # is the bridge's _token(), which ultimately calls _auth_headers(cfg) —
                # that raises SystemExit (a BaseException, NOT caught by `except Exception`)
                # on PAT/unresolvable creds. Left uncaught, that would escape _deliver,
                # escape run(), and silently kill the drain loop. Catch it here so a token
                # failure is just another delivery failure: retry -> eventual spill on
                # give-up, same as a network/HTTP error. CancelledError must still
                # propagate (shutdown/cancellation), so it is excluded and re-raised.
                try:
                    token = self._token_fn()
                except asyncio.CancelledError:
                    raise
                except (Exception, SystemExit) as exc:
                    raise RuntimeError(f"token acquisition failed: {type(exc).__name__}: {exc}") from exc
                result = await asyncio.to_thread(
                    send_once, self._base, token, item["verb"], item["args"], item["event_time"]
                )
                mid = result.get("id") if isinstance(result, dict) else None
                self._tickets.mark_sent(item["ticket"], mid)
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if self._stop.is_set() or time.monotonic() - t0 >= _GIVEUP_SECS:
                    self._spill(item, attempts, last_error or "bridge shutting down")
                    return
                # Interruptible backoff: wake at once if shutdown is signaled mid-wait, and spill.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    self._spill(item, attempts, last_error or "bridge shutting down")
                    return
                except asyncio.TimeoutError:
                    pass  # normal backoff elapsed -> retry
                backoff = min(backoff * 2, _BACKOFF_CAP)

    def _spill(self, item: dict, attempts: int, error: str) -> None:
        import datetime as dt
        record = {
            "ticket": item["ticket"], "agent_id": self._agent_id, "verb": item["verb"],
            "event_time": str(item["event_time"]),
            "spooled_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "attempts": attempts, "last_error": error,
            "message": item["args"],
        }
        ok = outbox.spool_message(self.spool_path, record)
        self._tickets.mark_failed(item["ticket"], error, self.spool_path if ok else "(spill failed)")
        if ok:
            self.spill_flag["pending"] += 1
            self.spill_flag["path"] = self.spool_path
            log.error("spooled undeliverable %s to %s after %s attempts (%s)",
                      item["verb"], self.spool_path, attempts, error)
        else:
            log.error("SPILL FAILED for %s (%s) — message lost", item["verb"], error)

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                # Cancelled while a delivery was in flight — do not lose the message.
                self._spill(item, 0, "sender cancelled in flight")
                raise
            except Exception as exc:
                # _deliver contains its own errors; this is belt-and-suspenders so an
                # unexpected bookkeeping failure can never kill the drain loop.
                log.error("sender: unexpected error delivering %s (%s) — spilling",
                          item.get("verb"), exc)
                self._spill(item, 0, f"unexpected: {type(exc).__name__}: {exc}")

    async def drain_and_stop(self, grace: float = 5.0) -> None:
        """Flush remaining queued items within `grace`, then spill whatever is left."""
        self._stop.set()
        deadline = time.monotonic() + grace
        while not self._q.empty() and time.monotonic() < deadline:
            item = self._q.get_nowait()
            try:
                token = self._token_fn()
                result = await asyncio.to_thread(
                    send_once, self._base, token, item["verb"], item["args"], item["event_time"]
                )
                self._tickets.mark_sent(item["ticket"], result.get("id") if isinstance(result, dict) else None)
            except Exception as exc:
                self._spill(item, 1, f"{type(exc).__name__}: {exc}")
        while not self._q.empty():  # grace expired: spill the rest
            item = self._q.get_nowait()
            self._spill(item, 0, "bridge shutdown grace expired")
