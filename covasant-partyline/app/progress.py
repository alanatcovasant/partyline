"""Small folder-based task tracker. Coordination claims remain independent."""
from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class ProgressUpdate(BaseModel):
    folder: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    agent_id: str = Field(min_length=1, max_length=200)
    status: str
    summary: str = Field(min_length=1, max_length=500)
    details: str | None = Field(None, max_length=20000)
    task_id: UUID | None = None
    operation_id: UUID = Field(default_factory=uuid4)
    workstream: str | None = Field(None, max_length=200)
    workdir: str | None = Field(None, max_length=2000)
    branch: str | None = Field(None, max_length=500)
    issue_ref: str | None = Field(None, max_length=2000)

    @field_validator("folder", "summary", "agent_id", mode="before")
    @classmethod
    def strip(cls, value, info):
        if isinstance(value, str):
            value = value.strip()
            return value.lower() if info.field_name == "folder" else value
        return value

    @field_validator("status", mode="before")
    @classmethod
    def status_value(cls, value):
        status = str(value).strip().lower()
        status = {"ended": "done"}.get(status, status)
        if status not in {"queued", "started", "progress", "blocked", "done", "cancelled"}:
            raise ValueError("status must be queued, started, progress, blocked, done or cancelled")
        return status


def record(body: ProgressUpdate, author: str | None = None) -> dict:
    from . import db

    fields = body.model_dump(mode="json")
    task_id = fields.pop("task_id")
    operation_id = fields.pop("operation_id")
    with db.get_pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (operation_id,))
        # Serialize a folder's changes, including creation and retry checks. Unlike a
        # Python hash, PostgreSQL's text hash is consistent across app worker processes.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (body.folder,))
        prior = conn.execute("SELECT * FROM progress_updates WHERE operation_id = %s", (operation_id,)).fetchone()
        if prior:
            expected = {**fields, "task_id": task_id}
            if prior["request"] != expected:
                raise ValueError("operation_id was already used with different input")
            return {k: v for k, v in prior.items() if k != "request"}
        if not conn.execute("SELECT agent_id FROM agents WHERE agent_id = %s", (body.agent_id,)).fetchone():
            raise ValueError("agent is not registered; register before recording progress")
        db._heartbeat(conn, body.agent_id)
        task = None
        if task_id:
            task = conn.execute("SELECT * FROM progress_tasks WHERE id = %s AND folder = %s", (task_id, body.folder)).fetchone()
            if not task:
                raise ValueError("task does not exist in this folder")
        elif body.status not in {"queued", "started"}:
            task = conn.execute("SELECT * FROM progress_tasks WHERE folder = %s AND status IN ('active','blocked')", (body.folder,)).fetchone()
            if not task:
                raise ValueError("no current task for this folder; start one or specify task_id")
        if task and task["status"] in {"done", "cancelled"}:
            raise ValueError("task is completed; start a new task")
        state = {"started": "active", "progress": "active"}.get(body.status, body.status)
        task_id_value = str(task["id"]) if task else str(uuid4())
        if state in {"active", "blocked"}:
            current = conn.execute("SELECT id FROM progress_tasks WHERE folder = %s AND status IN ('active','blocked') AND id <> %s", (body.folder, task_id_value)).fetchone()
            if current:
                raise ValueError("folder already has a current task; update or finish it first")
        if not task:
            conn.execute("""INSERT INTO progress_tasks (id, folder, title, status, agent_id, summary,
                details, workstream, workdir, branch, issue_ref)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (task_id_value, body.folder, body.summary, state, body.agent_id, body.summary,
                 body.details, body.workstream, body.workdir, body.branch, body.issue_ref))
        else:
            conn.execute("""UPDATE progress_tasks SET status=%s, agent_id=%s, summary=%s,
                details=%s, workstream=COALESCE(%s,workstream), workdir=COALESCE(%s,workdir),
                branch=COALESCE(%s,branch), issue_ref=COALESCE(%s,issue_ref), updated_at=now()
                WHERE id=%s""", (state, body.agent_id, body.summary, body.details, body.workstream,
                body.workdir, body.branch, body.issue_ref, task_id_value))
        from psycopg.types.json import Jsonb

        row = conn.execute("""INSERT INTO progress_updates (operation_id, task_id, folder,
            agent_id, status, summary, details, author, request)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (operation_id, task_id_value, body.folder, body.agent_id, body.status, body.summary,
             body.details, author, Jsonb({**fields, "task_id": task_id}))).fetchone()
        conn.commit()
        return {k: v for k, v in row.items() if k != "request"}


def list_tasks(folder: str | None = None, include_done: bool = False, workstream: str | None = None, limit: int = 200) -> list[dict]:
    from . import db

    clauses, args = [], []
    if folder:
        clauses.append("folder = %s")
        args.append(folder.strip().lower())
    if not include_done:
        clauses.append("status NOT IN ('done','cancelled')")
    if workstream:
        clauses.append("workstream = %s")
        args.append(workstream)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    args.append(max(1, min(limit, 1000)))
    with db.get_pool().connection() as conn:
        return conn.execute("SELECT id, folder, title, status, agent_id, summary, workstream, workdir, branch, issue_ref, created_at, updated_at FROM progress_tasks" + where + " ORDER BY (status IN ('done','cancelled')), folder, created_at DESC, id LIMIT %s", args).fetchall()


def history(task_id: UUID, limit: int = 100) -> list[dict]:
    from . import db

    with db.get_pool().connection() as conn:
        return conn.execute("""SELECT operation_id, task_id, folder, agent_id, status, summary,
            details, author, created_at FROM progress_updates WHERE task_id = %s
            ORDER BY id DESC LIMIT %s""", (str(task_id), max(1, min(limit, 500)))).fetchall()
