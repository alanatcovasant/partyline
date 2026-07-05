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

import psycopg

from .config import settings
from .db import NOTIFY_CHANNEL, _mint_token

log = logging.getLogger("partyline.notify")


class NotifyHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # --- subscriber management (SSE endpoints) ---
    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, payload: str) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer: drop it rather than block the listener.
                self._subscribers.discard(q)

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
            f"host={settings.pg_host} port={settings.pg_port} sslmode={settings.pg_sslmode}"
        )
        while not self._stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(
                    conninfo, password=_mint_token(), autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    log.info("LISTEN %s established", NOTIFY_CHANNEL)
                    backoff = 1.0
                    async for notify in conn.notifies():
                        self._broadcast(notify.payload)
                        if self._stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Listener connection lost (%s); reconnecting in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


hub = NotifyHub()
