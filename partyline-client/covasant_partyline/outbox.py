"""In-process outbox: spool-path/agent-id resolution, ticket table, and YAML spool writer.

Pure, testable units with no MCP-server or asyncio coupling. The bridge owns an Outbox
instance; the background sender (sender.py) drains it.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict

from . import cli

_TICKET_BOUND = 200  # keep the last N ticket statuses; evict oldest


def resolve_agent_id(cli_arg: str | None) -> str | None:
    """flag -> PARTYLINE_AGENT_ID -> .partyline/config.json (agentId/agent_id).

    Kept as a named seam for the bridge; the chain itself lives in cli.resolve_agent_id,
    the single home every partyline surface delegates to.
    """
    return cli.resolve_agent_id(cli_arg)


def resolve_spool_path(agent_id: str, env_value: str | None, cwd: str) -> str:
    """Resolve where undeliverable messages spill. See module/spec docs for the rules."""
    if not env_value:
        parent = os.path.dirname(os.path.abspath(cwd))
        return os.path.join(parent, f"partyline_{agent_id}.yaml")
    if "{agent_id}" in env_value:
        return env_value.replace("{agent_id}", agent_id)
    if os.path.isdir(env_value) or env_value.endswith((os.sep, "/")):
        return os.path.join(env_value.rstrip("/" + os.sep), f"partyline_{agent_id}.yaml")
    return env_value


class TicketTable:
    """Bounded, thread-safe map of ticket -> delivery status (in-memory; not persisted)."""

    def __init__(self) -> None:
        self._d: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()

    def new(self, verb: str) -> str:
        import uuid
        ticket = str(uuid.uuid4())
        with self._lock:
            self._d[ticket] = {"status": "pending", "verb": verb}
            while len(self._d) > _TICKET_BOUND:
                self._d.popitem(last=False)  # evict oldest
        return ticket

    def mark_sent(self, ticket: str, message_id: int | None) -> None:
        with self._lock:
            if ticket in self._d:
                self._d[ticket].update(status="sent", message_id=message_id)

    def mark_failed(self, ticket: str, error: str, spooled_to: str) -> None:
        with self._lock:
            if ticket in self._d:
                self._d[ticket].update(status="failed", error=error, spooled_to=spooled_to)

    def get(self, ticket: str) -> dict:
        with self._lock:
            entry = self._d.get(ticket)
            return dict(entry) if entry else {"status": "unknown"}


def _yaml_scalar(v) -> str:
    """Emit a YAML-safe scalar for the limited types spool records contain."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Always double-quote strings and escape backslashes/quotes/newlines -> a single safe line.
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{s}"'


def _yaml_doc(record: dict) -> str:
    """Render one record as a top-level list item ('- key: val' with nested 'message')."""
    lines = []
    first = True
    for k, v in record.items():
        prefix = "- " if first else "  "
        first = False
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            for sk, sv in v.items():
                lines.append(f"    {sk}: {_yaml_scalar(sv)}")
        else:
            lines.append(f"{prefix}{k}: {_yaml_scalar(v)}")
    return "\n".join(lines) + "\n"


def spool_message(path: str, record: dict) -> bool:
    """Append one YAML document to `path`. Best-effort lock; never raises."""
    import time
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        lock = path + ".lock"
        # crude cross-platform advisory lock: create-exclusive with brief spin
        acquired = False
        for _ in range(50):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.02)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(_yaml_doc(record))
            return True
        finally:
            if acquired:
                try:
                    os.remove(lock)
                except OSError:
                    pass
    except Exception:
        return False
