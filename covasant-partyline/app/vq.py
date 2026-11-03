"""Validation queue (prototype): code-complete work awaiting validation on a target.

One item per (issue_ref, target). State is DERIVED from the append-only run log, never
typed in: a hand-recorded PASS is `self_reported`; only an `executed` run (the CLI ran the
item's script and captured the exit code) moves an item to `validated`. `executed` is a
label asserted by the client, not a cryptographic proof; it separates "I ran it" from "I
say it passed". See docs/PROPOSAL_Validation_Queue_2026-11-02.md.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

CLAIM_TTL_SECONDS = 3600
_REF = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$"
_TARGET = r"^[a-z0-9][a-z0-9._-]*$"

# state precedence over the latest run; see _STATE_SQL
STATES = ("pending", "claimed", "validated", "self_reported", "failed", "inconclusive")


class Enqueue(BaseModel):
    issue_ref: str = Field(pattern=_REF, max_length=300)
    target: str = Field(pattern=_TARGET, max_length=128)
    agent_id: str = Field(min_length=1, max_length=200)
    script: str | None = Field(None, max_length=2000)
    proc: str | None = Field(None, max_length=1000)
    pr: str | None = Field(None, max_length=500)

    @field_validator("target", mode="before")
    @classmethod
    def lower_target(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class Run(BaseModel):
    issue_ref: str = Field(pattern=_REF, max_length=300)
    target: str = Field(pattern=_TARGET, max_length=128)
    agent_id: str = Field(min_length=1, max_length=200)
    exit_code: int = Field(ge=0, le=2)  # 0 PASS, 1 FAIL, 2 INCONCLUSIVE
    executed: bool = False  # True only when the CLI ran the item's script itself
    commit: str | None = Field(None, max_length=64)
    output_tail: str | None = Field(None, max_length=4000)
    operation_id: UUID = Field(default_factory=uuid4)

    @field_validator("target", mode="before")
    @classmethod
    def lower_target(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class Claim(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    target: str = Field(pattern=_TARGET, max_length=128)
    issue_ref: str | None = Field(None, pattern=_REF, max_length=300)  # None = take the next one

    @field_validator("target", mode="before")
    @classmethod
    def lower_target(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


# Latest run decides the state; a live claim only matters while nothing has been run since.
_STATE_SQL = """
CASE
  WHEN r.id IS NULL THEN CASE WHEN i.claimed_by IS NOT NULL AND i.claimed_until > now()
                              THEN 'claimed' ELSE 'pending' END
  WHEN r.exit_code = 0 AND r.executed THEN 'validated'
  WHEN r.exit_code = 0 THEN 'self_reported'
  WHEN r.exit_code = 1 THEN 'failed'
  ELSE 'inconclusive'
END
"""
_SELECT = f"""
SELECT i.id, i.issue_ref, i.target, i.script, i.proc, i.pr, i.enqueued_by, i.created_at,
       CASE WHEN i.claimed_until > now() THEN i.claimed_by END AS claimed_by,
       r.exit_code AS last_exit_code, r.executed AS last_executed, r.commit AS last_commit,
       r.created_at AS last_run_at, {_STATE_SQL} AS state
  FROM vq_items i
  LEFT JOIN LATERAL (SELECT * FROM vq_runs WHERE item_id = i.id ORDER BY id DESC LIMIT 1) r ON true
"""


def _registered(conn, agent_id: str) -> None:
    from . import db

    if not conn.execute("SELECT 1 FROM agents WHERE agent_id = %s", (agent_id,)).fetchone():
        raise ValueError("agent is not registered; register before using the validation queue")
    db._heartbeat(conn, agent_id)


def _item(conn, issue_ref: str, target: str) -> dict:
    row = conn.execute(_SELECT + " WHERE i.issue_ref = %s AND i.target = %s", (issue_ref, target)).fetchone()
    if not row:
        raise LookupError("no such queue item")
    return row


def enqueue(body: Enqueue) -> dict:
    from . import db

    with db.get_pool().connection() as conn:
        _registered(conn, body.agent_id)
        conn.execute(
            """INSERT INTO vq_items (id, issue_ref, target, script, proc, pr, enqueued_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (issue_ref, target) DO UPDATE SET
                 script = COALESCE(EXCLUDED.script, vq_items.script),
                 proc = COALESCE(EXCLUDED.proc, vq_items.proc),
                 pr = COALESCE(EXCLUDED.pr, vq_items.pr)""",
            (str(uuid4()), body.issue_ref, body.target, body.script, body.proc, body.pr, body.agent_id),
        )
        row = _item(conn, body.issue_ref, body.target)
        conn.commit()
        return row


def claim(body: Claim) -> dict:
    """Atomically claim one item. With no issue_ref, take the oldest unclaimed open item for the
    target; FOR UPDATE SKIP LOCKED means two concurrent callers never get the same one."""
    from . import db

    with db.get_pool().connection() as conn:
        _registered(conn, body.agent_id)
        if body.issue_ref:
            row = conn.execute(
                "SELECT id, claimed_by, claimed_until FROM vq_items WHERE issue_ref=%s AND target=%s FOR UPDATE",
                (body.issue_ref, body.target)).fetchone()
            if not row:
                raise LookupError("no such queue item")
            live = row["claimed_by"] and row["claimed_until"] and row["claimed_until"] > _now(conn)
            if live and row["claimed_by"] != body.agent_id:
                raise PermissionError(f"claimed by {row['claimed_by']}")
            item_id = row["id"]
        else:
            # Claimable by `next`: never run, inconclusive, or only self-reported. A FAILED item
            # needs a fix first, so it is claimed only by naming it.
            row = conn.execute(
                _SELECT + " WHERE i.target = %s AND (i.claimed_until IS NULL OR i.claimed_until <= now() OR i.claimed_by = %s)"
                " AND (r.id IS NULL OR r.exit_code = 2 OR (r.exit_code = 0 AND NOT r.executed))"
                " ORDER BY i.created_at, i.id LIMIT 1 FOR UPDATE OF i SKIP LOCKED",
                (body.target, body.agent_id)).fetchone()
            if not row:
                raise LookupError("nothing to claim for this target")
            item_id = row["id"]
        conn.execute("UPDATE vq_items SET claimed_by=%s, claimed_until=now() + make_interval(secs => %s) WHERE id=%s",
                     (body.agent_id, CLAIM_TTL_SECONDS, item_id))
        out = conn.execute(_SELECT + " WHERE i.id = %s", (item_id,)).fetchone()
        conn.commit()
        return out


def _now(conn):
    return conn.execute("SELECT now() AS n").fetchone()["n"]


def record_run(body: Run) -> dict:
    from . import db
    from psycopg.types.json import Jsonb

    fields = body.model_dump(mode="json")
    op = fields.pop("operation_id")
    with db.get_pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (op,))
        prior = conn.execute("SELECT * FROM vq_runs WHERE operation_id = %s", (op,)).fetchone()
        if prior:
            if prior["request"] != fields:
                raise ValueError("operation_id was already used with different input")
            return _item(conn, body.issue_ref, body.target)
        _registered(conn, body.agent_id)
        item = conn.execute("SELECT id, claimed_by, claimed_until FROM vq_items WHERE issue_ref=%s AND target=%s FOR UPDATE",
                            (body.issue_ref, body.target)).fetchone()
        if not item:
            raise LookupError("no such queue item")
        if item["claimed_by"] and item["claimed_by"] != body.agent_id and item["claimed_until"] > _now(conn):
            raise PermissionError(f"claimed by {item['claimed_by']}")
        conn.execute(
            """INSERT INTO vq_runs (operation_id, item_id, agent_id, exit_code, executed, commit,
                                    output_tail, request)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (op, item["id"], body.agent_id, body.exit_code, body.executed, body.commit,
             body.output_tail, Jsonb(fields)))
        # a finished run releases the claim
        conn.execute("UPDATE vq_items SET claimed_by=NULL, claimed_until=NULL WHERE id=%s", (item["id"],))
        row = _item(conn, body.issue_ref, body.target)
        conn.commit()
        return row


def list_items(target: str | None = None, state: str | None = None, limit: int = 200) -> list[dict]:
    from . import db

    clauses, args = [], []
    if target:
        clauses.append("i.target = %s")
        args.append(target.strip().lower())
    sql = _SELECT + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql = f"SELECT * FROM ({sql}) q" + (" WHERE state = %s" if state else "") + " ORDER BY created_at, id LIMIT %s"
    if state:
        if state not in STATES:
            raise ValueError("state must be one of " + ", ".join(STATES))
        args.append(state)
    args.append(max(1, min(limit, 1000)))
    with db.get_pool().connection() as conn:
        return conn.execute(sql, args).fetchall()


TEST_PREFIX = "zz-"


def purge_test_target(target: str) -> dict:
    """Delete every item (and its runs, by cascade) on a reserved test target. The queue's history
    is otherwise append-only, so only the `zz-` namespace, used by validation scripts, may be purged."""
    from . import db

    target = target.strip().lower()
    if not target.startswith(TEST_PREFIX):
        raise ValueError(f"only test targets starting with '{TEST_PREFIX}' can be purged")
    with db.get_pool().connection() as conn:
        n = conn.execute("DELETE FROM vq_items WHERE target = %s", (target,)).rowcount
        conn.commit()
        return {"target": target, "deleted_items": n}


def show(issue_ref: str, target: str) -> dict:
    from . import db

    with db.get_pool().connection() as conn:
        item = _item(conn, issue_ref, target.strip().lower())
        runs = conn.execute(
            """SELECT operation_id, agent_id, exit_code, executed, commit, output_tail, created_at
                 FROM vq_runs WHERE item_id = %s ORDER BY id DESC LIMIT 100""", (str(item["id"]),)).fetchall()
        return {**item, "runs": runs}
