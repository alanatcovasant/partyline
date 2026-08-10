"""Presence classification — how "present" an agent is, in words rather than a boolean.

Two clocks, deliberately kept apart:

  * ``last_heartbeat`` (agents) — refreshed by ANY call: post, claim, ack. It means "this
    session ran a command recently", i.e. the agent is *alive*.
  * ``checkpoints.updated_at`` — refreshed only by ack_checkpoint. It means "this session
    consumed messages recently", i.e. the agent is *listening*.

Only the second answers the question a sender actually has ("will this be read?"), and
until now nothing surfaced it. An agent can be busily posting for hours while never
reading a word addressed to it — that is precisely the failure this module makes visible.

Pure and dependency-free (thresholds are arguments, not imports) so it unit-tests without
Postgres, like targets.py.
"""

from __future__ import annotations

LIVE = "live"
IDLE = "idle"
STALE = "stale"

# Defaults mirror config.presence_live_seconds / presence_idle_seconds. Restated here so
# the module stays importable and testable on its own.
DEFAULT_LIVE_SECONDS = 300
DEFAULT_IDLE_SECONDS = 3600


def humanize_age(seconds: int | float | None) -> str:
    """Compact age for human-facing warnings: '42s', '9m', '19h', '6d'. None -> 'never'."""
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if seconds < limit:
            return f"{seconds // div}{unit}"
    return f"{seconds // 86400}d"


def classify(
    heartbeat_age_seconds: int | float | None,
    read_age_seconds: int | float | None,
    live_seconds: int = DEFAULT_LIVE_SECONDS,
    idle_seconds: int = DEFAULT_IDLE_SECONDS,
) -> dict:
    """Label one agent's presence from its two clocks.

    ``status`` comes from the heartbeat alone (is the session alive), and ``reading`` from
    the read cursor alone (is it consuming the line). Keeping them separate matters: an
    agent that never acked has ``reading=False`` while still being ``live``, and collapsing
    those into one word would hide the exact case that loses messages.

    ``read_age_seconds`` is None for an agent that has never acked a checkpoint — absence
    of a cursor, not a stale one.
    """
    if heartbeat_age_seconds is None:  # registered rows always have a heartbeat; be safe
        status = STALE
    elif heartbeat_age_seconds <= live_seconds:
        status = LIVE
    elif heartbeat_age_seconds <= idle_seconds:
        status = IDLE
    else:
        status = STALE

    return {
        "status": status,
        "reading": read_age_seconds is not None and read_age_seconds <= idle_seconds,
        "heartbeat_age_seconds": (
            None if heartbeat_age_seconds is None else int(heartbeat_age_seconds)
        ),
        "read_age_seconds": None if read_age_seconds is None else int(read_age_seconds),
    }


def describe(presence: dict) -> str:
    """One-line human summary of a classify() result, for warnings and the CLI/hook."""
    hb = humanize_age(presence.get("heartbeat_age_seconds"))
    read = humanize_age(presence.get("read_age_seconds"))
    status = presence.get("status", STALE)
    if status == LIVE and presence.get("reading"):
        return "live"
    return f"{status}, last active {hb} ago, last read {read} ago"


def delivery_warning(recipient: str, presence: dict) -> str | None:
    """Warn the SENDER when a recipient probably won't read this, else None.

    Returned to the caller of post_message and logged. The message is durable either way —
    this exists so "nobody answered" is distinguishable from "nobody was listening", which
    is the whole failure mode: a successful write into a channel with no reader is
    indistinguishable from a delivered one until much later.
    """
    if presence.get("status") == LIVE and presence.get("reading"):
        return None
    return (
        f"recipient {recipient!r} is {describe(presence)} — the message is stored and will "
        f"be delivered when they next read, but do not treat silence as agreement"
    )
