"""Push delivery via Postgres LISTEN/NOTIFY, fanned out to SSE subscribers.

A single dedicated async connection (app service-principal identity) holds ``LISTEN
partyline`` for the life of the process and rebroadcasts every notification to all
connected SSE clients. This connection lives *outside* the request pool because it is
long-lived; it reconnects with backoff to survive token expiry, idle timeouts, and
Lakebase scale-to-zero wake-ups.
"""

from __future__ import annotations

import asyncio
import logging
import time

import psycopg

from .config import settings
from .db import NOTIFY_CHANNEL, _mint_token

log = logging.getLogger("partyline.notify")

# How long to wait for a notification before returning to the loop to run a liveness
# check. Bounds how long a silently-dead socket can go undetected.
_LISTEN_HEARTBEAT_SECS = 30.0
# Proactively recycle the LISTEN connection well within the Lakebase token/idle window,
# so it never lingers stale (the minted OAuth credential is only used at login, but the
# connection can still be reaped server-side after long idle).
_LISTEN_MAX_LIFETIME_SECS = 1800.0


class NotifyHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --- subscriber management (SSE endpoints) ---
    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        log.info("SSE subscriber added count=%s", len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)
        log.info("SSE subscriber removed count=%s", len(self._subscribers))

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _broadcast(self, payload: str) -> None:
        dropped = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer: drop it rather than block the listener.
                self._subscribers.discard(q)
                dropped += 1
        if dropped:
            log.warning("Dropped %s slow SSE subscribers; remaining=%s", dropped, len(self._subscribers))

    # --- lifecycle ---
    def start(self) -> None:
        if settings.db_configured:
            self._task = asyncio.create_task(self._run(), name="partyline-listener")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = 1.0
        conninfo = (
            f"dbname={settings.pg_database} user={settings.pg_user} "
            f"host={settings.pg_host} port={settings.pg_port} sslmode={settings.pg_sslmode} "
            # connect_timeout bounds the handshake; TCP keepalives make the kernel surface
            # a silently-dropped socket as an error so we reconnect instead of hanging in
            # notifies() forever (which would stop all push delivery with no signal).
            "connect_timeout=10 "
            "keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=3"
        )
        while not self._stop.is_set():
            try:
                # Local dev uses PGPASSWORD (like the pool in db.py); deployed mints a
                # fresh Lakebase OAuth credential per (re)connect.
                if settings.local:
                    import os

                    password = os.environ.get("PGPASSWORD", "")
                else:
                    password = _mint_token()
                async with await psycopg.AsyncConnection.connect(conninfo, password=password, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    log.info("LISTEN %s established", NOTIFY_CHANNEL)
                    backoff = 1.0
                    started = time.monotonic()
                    # Bounded-wait loop: notifies(timeout=...) returns after the heartbeat
                    # interval even when idle, giving us a chance to actively probe the
                    # socket (SELECT 1) and to recycle before the connection goes stale.
                    while not self._stop.is_set():
                        async for notify in conn.notifies(timeout=_LISTEN_HEARTBEAT_SECS):
                            self._broadcast(notify.payload)
                            if self._stop.is_set():
                                break
                        if self._stop.is_set():
                            break
                        # Liveness probe: raises on a dead socket (→ reconnect below)
                        # rather than silently delivering nothing.
                        await conn.execute("SELECT 1")
                        if time.monotonic() - started > _LISTEN_MAX_LIFETIME_SECS:
                            log.info("Recycling LISTEN connection (max lifetime reached)")
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Listener connection lost (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


hub = NotifyHub()
