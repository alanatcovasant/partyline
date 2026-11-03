from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.vq import Claim, Enqueue, Run


def test_bad_refs_targets_and_exit_codes_are_refused():
    ok = dict(issue_ref="o/r#1", target="dev-eastus2", agent_id="a")
    Enqueue(**ok)
    for bad in ({"issue_ref": "1373"}, {"issue_ref": "o/r"}, {"target": "../x"}, {"target": ""}):
        with pytest.raises(ValidationError):
            Enqueue(**{**ok, **bad})
    for code in (-1, 3):
        with pytest.raises(ValidationError):
            Run(**ok, exit_code=code)
    assert Claim(agent_id="a", target=" Dev-EastUS2 ").target == "dev-eastus2"


@pytest.mark.anyio
async def test_lifecycle_evidence_binding_and_claims(client):
    from app import db

    sfx = uuid4().hex[:8]
    a, b, tgt = f"vq_a_{sfx}", f"vq_b_{sfx}", f"zz-vq-{sfx}"
    ref1, ref2 = f"zz/vq#{int(sfx[:6], 16)}", f"zz/vq#{int(sfx[2:], 16)}"
    for agent in (a, b):
        await client.post("/api/agents", json={"agent_id": agent, "owner": "test", "repo": "test"})

    async def post(path, **kw):
        return await client.post(f"/api/vq/{path}", json=kw)

    try:
        # POSITIVE: enqueue is idempotent and starts pending
        r = await post("items", issue_ref=ref1, target=tgt, agent_id=a, script="python v.py", pr="zz/vq#9")
        assert r.status_code == 200 and r.json()["state"] == "pending"
        assert (await post("items", issue_ref=ref1, target=tgt, agent_id=a)).json()["script"] == "python v.py"
        assert (await post("items", issue_ref=ref2, target=tgt, agent_id=a)).status_code == 200
        assert (await post("items", issue_ref=ref2, target=tgt, agent_id="nobody_registered")).status_code == 409

        # POSITIVE: next claims distinct items for distinct validators
        c1 = (await post("claim", agent_id=a, target=tgt)).json()
        c2 = (await post("claim", agent_id=b, target=tgt)).json()
        assert {c1["issue_ref"], c2["issue_ref"]} == {ref1, ref2} and c1["state"] == "claimed"
        # NEGATIVE: nothing left; and a held item cannot be claimed or recorded by another agent
        assert (await post("claim", agent_id=a, target=tgt)).json()["issue_ref"] == c1["issue_ref"]  # own claim re-acquired
        assert (await post("claim", agent_id=b, target=tgt, issue_ref=c1["issue_ref"])).status_code == 423
        assert (await post("runs", issue_ref=c1["issue_ref"], target=tgt, agent_id=b, exit_code=0, executed=True)).status_code == 423

        # NEGATIVE: a hand-recorded PASS is self_reported, never validated
        mine = c1["issue_ref"]
        s = (await post("runs", issue_ref=mine, target=tgt, agent_id=a, exit_code=0, commit="abc")).json()
        assert s["state"] == "self_reported" and s["claimed_by"] is None
        # self_reported stays claimable by `next`; an executed PASS then validates
        again = (await post("claim", agent_id=a, target=tgt)).json()
        assert again["issue_ref"] == mine
        op = str(uuid4())
        v = (await post("runs", issue_ref=mine, target=tgt, agent_id=a, exit_code=0, executed=True, commit="abc", operation_id=op)).json()
        assert v["state"] == "validated" and v["last_executed"] is True
        # idempotent retry, and reuse with different input is refused
        assert (await post("runs", issue_ref=mine, target=tgt, agent_id=a, exit_code=0, executed=True, commit="abc", operation_id=op)).status_code == 200
        assert (await post("runs", issue_ref=mine, target=tgt, agent_id=a, exit_code=1, operation_id=op)).status_code == 409

        # POSITIVE: a later FAIL reopens a validated item; history keeps both runs
        f = (await post("runs", issue_ref=mine, target=tgt, agent_id=a, exit_code=1, executed=True)).json()
        assert f["state"] == "failed"
        shown = (await client.get("/api/vq/show", params={"issue_ref": mine, "target": tgt})).json()
        assert [x["exit_code"] for x in shown["runs"]] == [1, 0, 0]
        # failed items are not handed out by `next`, but can be claimed by name
        other = c2["issue_ref"]
        assert (await post("claim", agent_id=b, target=tgt)).json()["issue_ref"] == other
        assert (await post("claim", agent_id=a, target=tgt, issue_ref=mine)).status_code == 200

        # list filters by state
        assert [i["issue_ref"] for i in (await client.get("/api/vq/items", params={"target": tgt, "state": "failed"})).json()] == [mine]
        assert (await client.get("/api/vq/items", params={"target": tgt, "state": "bogus"})).status_code == 409
        assert (await client.get("/api/vq/show", params={"issue_ref": "zz/vq#1", "target": tgt})).status_code == 404
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM vq_items WHERE target=%s", (tgt,))
            conn.execute("DELETE FROM agents WHERE agent_id = ANY(%s)", ([a, b],))
            conn.commit()


@pytest.mark.anyio
async def test_next_skips_a_row_locked_by_another_transaction(client):
    """Deterministic guard for FOR UPDATE SKIP LOCKED: while one transaction holds the oldest
    item's row lock, `next` must hand out the NEXT item instead of blocking on, or reusing, it."""
    import asyncio

    from app import db

    sfx = uuid4().hex[:8]
    tgt, a, b = f"zz-vqc-{sfx}", f"vqc_a_{sfx}", f"vqc_b_{sfx}"
    for ag in (a, b):
        await client.post("/api/agents", json={"agent_id": ag, "owner": "test", "repo": "test"})
    try:
        for n in (1, 2):
            await client.post("/api/vq/items", json={"issue_ref": f"zz/vqc#{n}", "target": tgt, "agent_id": a})
        with db.get_pool().connection() as conn:
            conn.execute("SELECT 1 FROM vq_items WHERE issue_ref='zz/vqc#1' AND target=%s FOR UPDATE", (tgt,))
            r = await asyncio.wait_for(client.post("/api/vq/claim", json={"agent_id": b, "target": tgt}), 5)
            assert r.status_code == 200 and r.json()["issue_ref"] == "zz/vqc#2", r.text
            conn.rollback()
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM vq_items WHERE target=%s", (tgt,))
            conn.execute("DELETE FROM agents WHERE agent_id = ANY(%s)", ([a, b],))
            conn.commit()


@pytest.mark.anyio
async def test_purge_only_removes_zz_test_targets(client):
    from app import db

    sfx = uuid4().hex[:8]
    agent, test_tgt, real_tgt = f"vqp_{sfx}", f"zz-vqp-{sfx}", f"vqp-real-{sfx}"
    await client.post("/api/agents", json={"agent_id": agent, "owner": "test", "repo": "test"})
    try:
        for t in (test_tgt, real_tgt):
            await client.post("/api/vq/items", json={"issue_ref": "zz/vqp#1", "target": t, "agent_id": agent})
        await client.post("/api/vq/runs", json={"issue_ref": "zz/vqp#1", "target": test_tgt, "agent_id": agent, "exit_code": 0})
        # NEGATIVE: a real target's history is never purged
        r = await client.delete("/api/vq/items", params={"target": real_tgt})
        assert r.status_code == 409
        assert len((await client.get("/api/vq/items", params={"target": real_tgt})).json()) == 1
        # POSITIVE: the zz- target goes, runs with it
        r = await client.delete("/api/vq/items", params={"target": test_tgt})
        assert r.status_code == 200 and r.json()["deleted_items"] == 1
        assert (await client.get("/api/vq/items", params={"target": test_tgt})).json() == []
        with db.get_pool().connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM vq_runs r JOIN vq_items i ON i.id=r.item_id WHERE i.target=%s", (test_tgt,)).fetchone()["n"] == 0
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM vq_items WHERE target = ANY(%s)", ([test_tgt, real_tgt],))
            conn.execute("DELETE FROM agents WHERE agent_id=%s", (agent,))
            conn.commit()
