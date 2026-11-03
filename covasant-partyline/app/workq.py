"""Work queue (phase 2, slice 1): a read-through mirror of GitHub issues, ordered, with an atomic pull.

GitHub is the source of every issue and of its triage fields (docs/PROPOSAL_Unified_Work_Queue_2026-11-03.md,
section 0). This module stores only a MIRROR of what a sync reports, plus nothing about claims: a claim is a
live row in the existing `claims` table keyed `issue:owner/repo#N`, so there is exactly one claim authority.
A direct `claim-issue` therefore takes an item out of the queue with no extra step, and `next` takes its
claim through the same all-or-nothing `db.claim_targets`.

Until the Project fields exist, the labels already used in the fleet stand in for them: `P0`..`P3`
(priority band), `sp-N` or `effort:N` (story points, Fibonacci), `severity:S1`..`S4`, `cluster:<name>`.
An explicit P label outranks a band derived from severity.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

from pydantic import BaseModel, Field

WORK_TTL_SECONDS = int(os.getenv("PARTYLINE_WORK_CLAIM_TTL", "14400"))  # 4h, renewed by re-pulling
FIB = (1, 2, 3, 5, 8, 13)
CLAIM_LABEL = "partyline:claimed"
TEST_PREFIX = "zz/"
STATES = ("ready", "untriaged", "claimed", "external", "closed")
_REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
_REF = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$"
_SEV_BAND = {"S1": 0, "S2": 1, "S3": 2, "S4": 3}


def parse_labels(labels: list[str]) -> dict:
    """Triage fields carried by labels. Pure, so the rules are tested without a database."""
    priority = severity = cluster = None
    points: list[int] = []
    for raw in labels:
        name = raw.strip()
        if m := re.fullmatch(r"[Pp]([0-3])", name):
            priority = min(int(m.group(1)), priority if priority is not None else 9)
        elif m := re.fullmatch(r"(?i)severity:\s*s([1-4])", name):
            severity = f"S{m.group(1)}" if severity is None else min(severity, f"S{m.group(1)}")
        elif m := re.fullmatch(r"(?i)(?:sp-|effort:\s*)(\d+)", name):
            if int(m.group(1)) in FIB:
                points.append(int(m.group(1)))
        elif m := re.fullmatch(r"(?i)cluster:\s*(\S.*)", name):
            cluster = cluster or m.group(1).strip().lower()
    band = priority if priority is not None else _SEV_BAND.get(severity)
    return {"priority": f"P{priority}" if priority is not None else None, "severity": severity,
            "points": max(points) if points else None, "cluster": cluster, "band": band,
            "claim_label": CLAIM_LABEL in labels}


class Issue(BaseModel):
    number: int = Field(ge=1)
    title: str = Field(max_length=1000)
    url: str | None = Field(None, max_length=500)
    state: str = Field("open", pattern=r"^(?i:open|closed)$")
    labels: list[str] = Field(default_factory=list, max_length=100)
    assignees: list[str] = Field(default_factory=list, max_length=50)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Sync(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    repo: str = Field(pattern=_REPO, max_length=200)
    viewer: str | None = Field(None, max_length=100)  # the gh login: an assignee equal to it is not a claim
    issues: list[Issue] = Field(max_length=2000)
    complete: bool = False  # True = `issues` is EVERY open issue, so an open entry not listed was closed


class Next(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    queue: str | None = Field(None, max_length=200)  # a repo ("Aura" or "covasant/Aura"); default: the agent's own
    branch: str | None = Field(None, max_length=200)


class Release(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    issue_ref: str = Field(pattern=_REF, max_length=300)


# claimed = a live row in `claims` for this issue; compared case-insensitively because repo casing varies
_SELECT = """
SELECT w.source_ref, w.repo, w.number, w.title, w.url, w.gh_state, w.priority, w.severity, w.points, w.cluster,
       w.band, w.claim_label, w.external_claim, w.gh_created_at, w.gh_updated_at, w.first_seen_at, w.synced_at,
       c.agent_id AS claimed_by, c.expires_at AS claimed_until,
       CASE WHEN w.gh_state <> 'open' THEN 'closed'
            WHEN c.agent_id IS NOT NULL THEN 'claimed'
            WHEN w.external_claim IS NOT NULL THEN 'external'
            WHEN w.band IS NULL OR w.points IS NULL THEN 'untriaged'
            ELSE 'ready' END AS state
  FROM work_items w
  LEFT JOIN claims c ON lower(c.resource_path) = lower('issue:' || w.source_ref) AND c.expires_at > now()
"""
_ORDER = " ORDER BY w.band NULLS LAST, w.points NULLS LAST, w.gh_created_at NULLS LAST, w.source_ref"


def _registered(conn, agent_id: str) -> dict:
    from . import db

    row = conn.execute("SELECT agent_id, repo FROM agents WHERE agent_id = %s", (agent_id,)).fetchone()
    if not row:
        raise ValueError("agent is not registered; register before using the work queue")
    db._heartbeat(conn, agent_id)
    return row


def sync(body: Sync) -> dict:
    from . import db
    from psycopg.types.json import Jsonb

    added = updated = closed = 0
    seen = []
    with db.get_pool().connection() as conn:
        _registered(conn, body.agent_id)
        for it in body.issues:
            ref = f"{body.repo}#{it.number}"
            seen.append(ref)
            f = parse_labels(it.labels)
            others = [a for a in it.assignees if a and a != body.viewer]
            row = conn.execute(
                """INSERT INTO work_items (source_ref, repo, number, title, url, gh_state, labels, assignees,
                          priority, severity, points, cluster, band, claim_label, external_claim,
                          gh_created_at, gh_updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (source_ref) DO UPDATE SET
                     title=EXCLUDED.title, url=EXCLUDED.url, gh_state=EXCLUDED.gh_state, labels=EXCLUDED.labels,
                     assignees=EXCLUDED.assignees, priority=EXCLUDED.priority, severity=EXCLUDED.severity,
                     points=EXCLUDED.points, cluster=EXCLUDED.cluster, band=EXCLUDED.band,
                     claim_label=EXCLUDED.claim_label, external_claim=EXCLUDED.external_claim,
                     gh_created_at=EXCLUDED.gh_created_at, gh_updated_at=EXCLUDED.gh_updated_at, synced_at=now()
                   RETURNING (xmax = 0) AS inserted""",
                (ref, body.repo, it.number, it.title, it.url, it.state.lower(), Jsonb(it.labels), Jsonb(it.assignees),
                 f["priority"], f["severity"], f["points"], f["cluster"], f["band"], f["claim_label"],
                 others[0] if others else None, it.created_at, it.updated_at)).fetchone()
            added += bool(row["inserted"])
            updated += not row["inserted"]
        if body.complete:
            closed = conn.execute(
                "UPDATE work_items SET gh_state='closed', synced_at=now() "
                "WHERE repo=%s AND gh_state='open' AND NOT (source_ref = ANY(%s))", (body.repo, seen)).rowcount
        conn.commit()
    return {"repo": body.repo, "received": len(body.issues), "added": added, "updated": updated,
            "closed_upstream": closed, "complete": body.complete}


def _scope_sql(queue: str | None, agent_repo: str) -> tuple[str, list]:
    scope = (queue or agent_repo).strip().lower()
    if "/" in scope:
        return " AND lower(w.repo) = %s", [scope]
    return " AND lower(split_part(w.repo, '/', 2)) = %s", [scope]


def next_item(body: Next) -> dict:
    """Resume this seat's own claimed issue, else claim the best ready one in scope."""
    from . import db

    with db.get_pool().connection() as conn:
        agent = _registered(conn, body.agent_id)
        own = conn.execute(
            _SELECT + " WHERE w.gh_state='open' AND c.agent_id = %s" + _ORDER + " LIMIT 1", (body.agent_id,)).fetchone()
        scope_sql, scope_args = _scope_sql(body.queue, agent["repo"])
        cands = [] if own else conn.execute(
            _SELECT + " WHERE w.gh_state='open' AND c.agent_id IS NULL AND w.external_claim IS NULL"
            " AND w.band IS NOT NULL AND w.points IS NOT NULL" + scope_sql + _ORDER + " LIMIT 25",
            scope_args).fetchall()
        conn.commit()
    pick = [own] if own else cands
    for item in pick:
        ok, _rows, _blocker = db.claim_targets(
            body.agent_id, item["repo"], body.branch, [("issue:" + item["source_ref"], "issue")],
            "exclusive", "work next", WORK_TTL_SECONDS)
        if ok:
            with db.get_pool().connection() as conn:
                out = conn.execute(_SELECT + " WHERE w.source_ref = %s", (item["source_ref"],)).fetchone()
            return {**out, "resumed": bool(own)}
    raise LookupError("nothing ready in this queue")


def release(body: Release) -> dict:
    from . import db

    with db.get_pool().connection() as conn:
        _registered(conn, body.agent_id)
        n = conn.execute("DELETE FROM claims WHERE lower(resource_path) = lower(%s) AND agent_id = %s",
                         ("issue:" + body.issue_ref, body.agent_id)).rowcount
        conn.commit()
    return {"issue_ref": body.issue_ref, "released": bool(n)}


def list_items(repo: str | None = None, state: str | None = None, limit: int = 200) -> list[dict]:
    from . import db

    if state and state not in STATES:
        raise ValueError("state must be one of " + ", ".join(STATES))
    args: list = []
    where = ""
    if repo:
        where = " WHERE " + ("lower(w.repo) = %s" if "/" in repo else "lower(split_part(w.repo, '/', 2)) = %s")
        args.append(repo.strip().lower())
    sql = f"SELECT * FROM ({_SELECT}{where}) q" + (" WHERE state = %s" if state else "")
    if state:
        args.append(state)
    args.append(max(1, min(limit, 1000)))
    order = " ORDER BY band NULLS LAST, points NULLS LAST, gh_created_at NULLS LAST, source_ref LIMIT %s"
    with db.get_pool().connection() as conn:
        return conn.execute(sql + order, args).fetchall()


def audit(repo: str | None = None) -> dict:
    """What the queue cannot vouch for: untriaged intake, external claims, labels with no live claim, and
    work closed upstream while a seat still holds it. `last_sync` says how fresh the mirror is."""
    rows = list_items(repo, None, 1000)
    open_rows = [r for r in rows if r["state"] != "closed"]

    def brief(r):
        return {k: r[k] for k in ("source_ref", "title", "state", "claimed_by", "external_claim", "gh_created_at")}

    from . import db

    with db.get_pool().connection() as conn:
        syncs = conn.execute("SELECT repo, max(synced_at) AS last_sync, count(*) FILTER (WHERE gh_state='open') AS open "
                             "FROM work_items GROUP BY repo ORDER BY repo").fetchall()
        closed_held = conn.execute(
            _SELECT + " WHERE w.gh_state='closed' AND c.agent_id IS NOT NULL").fetchall()
    return {
        "counts": {s: sum(1 for r in rows if r["state"] == s) for s in STATES},
        "untriaged": [brief(r) for r in sorted(open_rows, key=lambda r: r["gh_created_at"] or datetime.max.astimezone())
                      if r["state"] == "untriaged"],
        "external_claims": [brief(r) for r in open_rows if r["state"] == "external"],
        "label_without_live_claim": [brief(r) for r in open_rows if r["claim_label"] and not r["claimed_by"]],
        "closed_upstream_while_claimed": [brief(r) for r in closed_held],
        "last_sync": syncs,
    }


def purge_test_repo(repo: str) -> dict:
    """Delete mirror rows of a reserved test repo (`zz/...`); real repos' rows are never purged here."""
    from . import db

    if not repo.strip().lower().startswith(TEST_PREFIX):
        raise ValueError(f"only test repos starting with '{TEST_PREFIX}' can be purged")
    with db.get_pool().connection() as conn:
        n = conn.execute("DELETE FROM work_items WHERE lower(repo) = %s", (repo.strip().lower(),)).rowcount
        conn.execute("DELETE FROM claims WHERE lower(resource_path) LIKE %s", (f"issue:{repo.strip().lower()}#%",))
        conn.commit()
        return {"repo": repo, "deleted_items": n}
