"""Ownership handoff for progress tasks (work-queue phase 1).

A task's owner is the agent in its `agent_id`. A task moves to another seat only by an explicit
event, never by being overwritten: the owner OFFERS it (to one agent or anyone), the offeree
ACCEPTS from its own folder (or either side DECLINES/withdraws), or an agent TAKES OVER a task
whose owner has gone stale. The default for interrupted work is to stay with its seat (see
docs/PROPOSAL_Unified_Work_Queue_2026-11-03.md, section 2.1); this module is the exception path.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

_FOLDER = r"^[a-z0-9][a-z0-9._-]*$"


class Handoff(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)  # the actor
    action: Literal["offer", "accept", "decline", "takeover"]
    to: str | None = Field(None, max_length=200)  # offer: an agent id, or "*" for anyone
    note: str | None = Field(None, max_length=1000)  # why; required for takeover
    next_action: str | None = Field(None, max_length=500)
    folder: str | None = Field(None, pattern=_FOLDER, max_length=128)  # the new owner's folder
    operation_id: UUID = Field(default_factory=uuid4)

    @field_validator("folder", mode="before")
    @classmethod
    def lower_folder(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


_COLS = ("id, folder, title, status, agent_id, summary, workstream, workdir, branch, issue_ref, next_action,"
         " handoff_to, handoff_from, handoff_note, handoff_offered_at, created_at, updated_at")


def _owner_presence(conn, agent_id: str) -> tuple[str, str]:
    """(status, human description) for an agent from its heartbeat, via the shared classifier."""
    from . import presence
    from .config import settings

    row = conn.execute("SELECT EXTRACT(EPOCH FROM (now() - last_heartbeat)) AS age FROM agents WHERE agent_id=%s",
                       (agent_id,)).fetchone()
    p = presence.classify(None if not row else float(row["age"]), None,
                          settings.presence_live_seconds, settings.presence_idle_seconds)
    return p["status"], presence.humanize_age(p["heartbeat_age_seconds"])


def handoff(task_id: UUID, body: Handoff) -> dict:
    from psycopg.types.json import Jsonb

    from . import db, presence

    fields = body.model_dump(mode="json")
    op = fields.pop("operation_id")
    with db.get_pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (op,))
        prior = conn.execute("SELECT request FROM progress_updates WHERE operation_id = %s", (op,)).fetchone()
        if prior:
            if prior["request"] != fields:
                raise ValueError("operation_id was already used with different input")
            return conn.execute(f"SELECT {_COLS} FROM progress_tasks WHERE id = %s", (str(task_id),)).fetchone()
        if not conn.execute("SELECT 1 FROM agents WHERE agent_id = %s", (body.agent_id,)).fetchone():
            raise ValueError("agent is not registered; register before handing work off")
        db._heartbeat(conn, body.agent_id)
        task = conn.execute("SELECT * FROM progress_tasks WHERE id = %s FOR UPDATE", (str(task_id),)).fetchone()
        if not task:
            raise LookupError("no such task")
        if task["status"] in {"done", "cancelled"}:
            raise ValueError("task is completed; there is nothing to hand off")
        owner, offered_to = task["agent_id"], task["handoff_to"]
        new_folder, new_owner, clear_offer, kind, summary = task["folder"], owner, False, None, None
        set_offer = None

        if body.action == "offer":
            if body.agent_id != owner:
                raise ValueError(f"only the owner ({owner}) can offer this task")
            if not body.to:
                raise ValueError("an offer needs `to`: an agent id, or * for anyone")
            if body.to != "*":
                if body.to == owner:
                    raise ValueError("cannot offer a task to its owner")
                if not conn.execute("SELECT 1 FROM agents WHERE agent_id = %s", (body.to,)).fetchone():
                    raise ValueError(f"agent {body.to} is not registered; an offer to it could never be seen")
            set_offer, kind = body.to, "handoff_offered"
            summary = f"offered to {'anyone' if body.to == '*' else body.to}"
        elif body.action == "decline":
            if not offered_to:
                raise ValueError("there is no pending offer on this task")
            if body.agent_id not in (owner, offered_to) and offered_to != "*":
                raise ValueError(f"only the owner ({owner}) or the offeree ({offered_to}) can decline")
            clear_offer = True
            kind = "handoff_withdrawn" if body.agent_id == owner else "handoff_declined"
            summary = f"offer {'withdrawn' if body.agent_id == owner else 'declined'} by {body.agent_id}"
        else:  # accept | takeover: the task moves to the actor's folder
            if body.agent_id == owner:
                raise ValueError("the owner already has this task")
            if not body.folder:
                raise ValueError(f"{body.action} needs `folder`: the folder you will work this task in")
            if body.action == "accept":
                if not offered_to:
                    raise ValueError("there is no pending offer on this task")
                if offered_to not in ("*", body.agent_id):
                    raise ValueError(f"the offer is to {offered_to}, not {body.agent_id}")
                kind, summary = "handoff_accepted", f"accepted from {owner} into {body.folder}"
            else:
                if not (body.note or "").strip():
                    raise ValueError("a takeover needs `note`: why it is being taken over")
                status, age = _owner_presence(conn, owner)
                if status != presence.STALE:
                    raise ValueError(f"owner {owner} is {status} (last active {age} ago); take over only a stale owner, "
                                     "or ask them to offer it")
                kind, summary = "handoff_takeover", f"taken over from stale {owner} (last active {age} ago) into {body.folder}"
            if task["status"] in {"active", "blocked"} and body.folder != task["folder"]:
                clash = conn.execute("SELECT 1 FROM progress_tasks WHERE folder=%s AND status IN ('active','blocked')",
                                     (body.folder,)).fetchone()
                if clash:
                    raise ValueError(f"folder {body.folder} already has a current task; finish it or use another folder")
            new_folder, new_owner, clear_offer = body.folder, body.agent_id, True

        # Resolve every new value here so the UPDATE has no untyped parameters.
        new_to = None if clear_offer else (set_offer if set_offer is not None else task["handoff_to"])
        new_note = None if clear_offer else (body.note if set_offer is not None else task["handoff_note"])
        offered_at = "NULL" if clear_offer else ("now()" if set_offer is not None else "handoff_offered_at")
        new_from = owner if new_owner != owner else task["handoff_from"]
        conn.execute(
            f"""UPDATE progress_tasks SET agent_id=%s, folder=%s, handoff_from=%s, handoff_to=%s,
                 handoff_note=%s, handoff_offered_at={offered_at},
                 next_action=COALESCE(%s, next_action), updated_at=now() WHERE id=%s""",
            (new_owner, new_folder, new_from, new_to, new_note, body.next_action, str(task_id)))
        conn.execute(
            """INSERT INTO progress_updates (operation_id, task_id, folder, agent_id, status, summary, details, author, request)
               VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s)""",
            (op, str(task_id), new_folder, body.agent_id, kind, summary, body.note, Jsonb(fields)))
        row = conn.execute(f"SELECT {_COLS} FROM progress_tasks WHERE id = %s", (str(task_id),)).fetchone()
        conn.commit()
        return row
