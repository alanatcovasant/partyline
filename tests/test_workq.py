import asyncio
from uuid import uuid4

import pytest

from app.workq import excluded_by, parse_labels


def test_labels_carry_priority_points_severity_and_cluster():
    f = parse_labels(["bug", "P2", "sp-5", "cluster:Bundle-Environments"])
    assert (f["priority"], f["points"], f["band"], f["cluster"]) == ("P2", 5, 2, "bundle-environments")
    # an explicit P label outranks a band derived from severity; the highest of several P labels wins
    assert parse_labels(["P3", "P1", "severity:S1", "effort:3"])["band"] == 1
    assert parse_labels(["severity:S2", "sp-8"])["band"] == 1
    # NEGATIVE: a non-Fibonacci estimate and an unknown priority are not triage
    f = parse_labels(["sp-4", "P7", "priority-high"])
    assert f["points"] is None and f["band"] is None
    assert parse_labels(["partyline:claimed"])["claim_label"] is True
    assert parse_labels([])["claim_label"] is False


@pytest.fixture()
async def line(client):
    """A throwaway repo `zz/q<sfx>` and two registered agents; everything is purged afterwards."""
    from app import db, workq

    sfx = uuid4().hex[:8]
    repo = f"zz/q{sfx}"
    ag = {k: f"wq_{k}_{sfx}" for k in "ab"}
    for a in ag.values():
        await client.post("/api/agents", json={"agent_id": a, "owner": "test", "repo": f"q{sfx}"})

    async def sync(issues, complete=False, viewer="me", expect=200, **extra):
        r = await client.post("/api/queue/sync", json={"agent_id": ag["a"], "repo": repo, "viewer": viewer,
                                                       "issues": issues, "complete": complete, **extra})
        assert r.status_code == expect, r.text
        return r.json()

    def iss(n, labels=(), assignees=(), state="open", created="2026-11-0%dT00:00:00Z"):
        return {"number": n, "title": f"issue {n}", "labels": list(labels), "assignees": list(assignees),
                "state": state, "created_at": created % (n % 9 + 1)}

    async def states():
        rows = (await client.get("/api/queue", params={"repo": repo})).json()
        return {r["source_ref"].split("#")[1]: r["state"] for r in rows}

    yield type("L", (), dict(client=client, repo=repo, ag=ag, sync=staticmethod(sync), iss=staticmethod(iss),
                             states=staticmethod(states)))
    workq.purge_test_repo(repo)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM claims WHERE agent_id = ANY(%s)", (list(ag.values()),))
        conn.commit()


@pytest.mark.anyio
async def test_triage_gate_order_and_atomic_pull(line):
    c, a, b = line.client, line.ag["a"], line.ag["b"]
    r = await line.sync([
        line.iss(1, ["P2", "sp-3"]), line.iss(2, ["P0", "sp-8"]), line.iss(3, ["P0", "sp-2"]),
        line.iss(4, ["bug"]),                       # untriaged: no band, no points
        line.iss(5, ["P1"]),                        # untriaged: no points
        line.iss(6, ["P1", "sp-1"], assignees=["bob"]),   # assigned to someone else: external
        line.iss(7, ["P1", "sp-1"], assignees=["me"]),    # assigned to the acting account: ordinary
    ])
    assert r["added"] == 7
    assert await line.states() == {"1": "ready", "2": "ready", "3": "ready", "4": "untriaged", "5": "untriaged",
                                   "6": "external", "7": "ready"}
    # a re-sync updates in place
    assert (await line.sync([line.iss(1, ["P2", "sp-3"])]))["updated"] == 1

    # POSITIVE: the band orders first (P0 before P1 before P2), then smaller points first
    got = []
    for who in (a, b, a, b):
        r = await c.post("/api/queue/next", json={"agent_id": who, "queue": line.repo})
        assert r.status_code == 200, r.text
        got.append(r.json())
    # a resumes its own first item instead of taking another; b likewise
    assert [g["source_ref"].split("#")[1] for g in got] == ["3", "2", "3", "2"]
    assert [g["resumed"] for g in got] == [False, False, True, True]
    assert {g["claimed_by"] for g in got} == {a, b}
    # NEGATIVE: untriaged and externally claimed items were never handed out
    st = await line.states()
    assert st["4"] == "untriaged" and st["5"] == "untriaged" and st["6"] == "external"
    assert st["3"] == st["2"] == "claimed"
    # POSITIVE: releasing returns the item to the pool for the other seat
    assert (await c.post("/api/queue/release", json={"agent_id": a, "issue_ref": f"{line.repo}#3"})).json()["released"]
    # NEGATIVE: a non-holder cannot release
    assert not (await c.post("/api/queue/release", json={"agent_id": a, "issue_ref": f"{line.repo}#2"})).json()["released"]
    assert (await line.states())["3"] == "ready"
    # DRAIN: pulling until empty hands out exactly the ready items (3 released, 1 and 7), never the
    # untriaged (4, 5) or the externally claimed (6); 2 is still held by b
    drained = []
    for i in range(6):
        d = f"wq_d{i}_{line.repo[-8:]}"
        await c.post("/api/agents", json={"agent_id": d, "owner": "test", "repo": line.repo.split("/")[1]})
        r = await c.post("/api/queue/next", json={"agent_id": d, "queue": line.repo})
        if r.status_code == 404:
            break
        drained.append(r.json()["source_ref"].split("#")[1])
    assert sorted(drained) == ["1", "3", "7"] and r.status_code == 404


@pytest.mark.anyio
async def test_concurrent_pulls_never_hand_out_the_same_item(line):
    await line.sync([line.iss(n, ["P1", "sp-2"]) for n in range(1, 5)])
    agents = [f"wq_c{i}_{uuid4().hex[:6]}" for i in range(4)]
    for g in agents:
        await line.client.post("/api/agents", json={"agent_id": g, "owner": "test", "repo": line.repo.split("/")[1]})
    try:
        rs = await asyncio.gather(*[line.client.post("/api/queue/next", json={"agent_id": g, "queue": line.repo})
                                    for g in agents])
        refs = [r.json()["source_ref"] for r in rs if r.status_code == 200]
        assert len(refs) == 4 and len(set(refs)) == 4
        # NEGATIVE: the queue is now empty for a fifth seat
        r = await line.client.post("/api/queue/next", json={"agent_id": line.ag["a"], "queue": line.repo})
        assert r.status_code == 404
    finally:
        from app import db

        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM claims WHERE agent_id = ANY(%s)", (agents,))
            conn.commit()


@pytest.mark.anyio
async def test_scope_direct_claims_and_closed_upstream(line):
    c, a, b = line.client, line.ag["a"], line.ag["b"]
    await line.sync([line.iss(1, ["P1", "sp-1"]), line.iss(2, ["P1", "sp-1"])])
    # NEGATIVE: an agent whose own repo is a different one is never handed this repo's work by default
    stranger = f"wq_s_{uuid4().hex[:6]}"
    await c.post("/api/agents", json={"agent_id": stranger, "owner": "test", "repo": "elsewhere"})
    assert (await c.post("/api/queue/next", json={"agent_id": stranger})).status_code == 404
    # POSITIVE: a direct claim by name takes the item out of the queue, with no queue-side step
    r = await c.post("/api/claims/work-item", json={"agent_id": b, "repo": line.repo, "work_item": f"issue:{line.repo}#1",
                                                    "paths": []})
    assert r.json()["acquired"]
    assert (await line.states())["1"] == "claimed"
    got = await c.post("/api/queue/next", json={"agent_id": a})   # default scope = a's own repo (q<sfx>)
    assert got.json()["source_ref"].endswith("#2")
    # a full sync that omits #2 closes it upstream; the audit reports it as still held
    await line.sync([line.iss(1, ["P1", "sp-1"])], complete=True)
    assert (await line.states())["2"] == "closed"
    audit = (await c.get("/api/queue/audit", params={"repo": line.repo})).json()
    assert [x["source_ref"] for x in audit["closed_upstream_while_claimed"]] == [f"{line.repo}#2"]
    # NEGATIVE: a partial sync (complete=false) never closes anything
    await line.sync([line.iss(1, ["P1", "sp-1"])], complete=False)
    assert (await line.states())["1"] == "claimed"
    # NEGATIVE: only zz/ repos can be purged
    assert (await c.delete("/api/queue", params={"repo": "covasant/Aura"})).status_code == 409
    # untriaged + label-without-claim are audited
    await line.sync([line.iss(9, ["bug"]), line.iss(10, ["P1", "sp-1", "partyline:claimed"])])
    audit = (await c.get("/api/queue/audit", params={"repo": line.repo})).json()
    assert f"{line.repo}#9" in [x["source_ref"] for x in audit["untriaged"]]
    assert f"{line.repo}#10" in [x["source_ref"] for x in audit["label_without_live_claim"]]


def test_exclusion_labels_are_matched_case_insensitively_and_ordinary_labels_are_not():
    assert excluded_by(["bug", "Epic"]) == "Epic"
    assert excluded_by(["pending-deploy"]) == "pending-deploy"
    assert excluded_by(["bug", "enhancement", "P1", "sp-3", "validation-failed"]) is None


@pytest.mark.anyio
async def test_excluded_work_is_never_handed_out_but_is_audited(line):
    c = line.client
    await line.sync([line.iss(1, ["P0", "sp-1", "epic"]), line.iss(2, ["P0", "sp-1", "question"]),
                     line.iss(3, ["P2", "sp-5"])])
    assert await line.states() == {"1": "excluded", "2": "excluded", "3": "ready"}
    got = await c.post("/api/queue/next", json={"agent_id": line.ag["a"], "queue": line.repo})
    assert got.json()["source_ref"].endswith("#3")      # the P0 items are skipped, not the P2 one chosen by accident
    assert (await c.post("/api/queue/next", json={"agent_id": line.ag["b"], "queue": line.repo})).status_code == 404
    audit = (await c.get("/api/queue/audit", params={"repo": line.repo})).json()
    assert {x["source_ref"].split("#")[1]: x["excluded_by"] for x in audit["excluded"]} == {"1": "epic", "2": "question"}
    # removing the label in GitHub puts it back in the queue at its band at the next sync
    await line.sync([line.iss(1, ["P0", "sp-1"]), line.iss(2, ["P0", "sp-1", "question"]), line.iss(3, ["P2", "sp-5"])])
    assert (await line.states())["1"] == "ready"


@pytest.mark.anyio
async def test_a_second_operator_cannot_flip_the_external_claims(line):
    issues = [line.iss(1, ["P1", "sp-1"], assignees=["alan"]), line.iss(2, ["P1", "sp-1"])]
    await line.sync(issues, viewer="alan")
    assert (await line.states())["1"] == "ready"        # assigned to the operator: ordinary work
    # NEGATIVE: another login syncing the same repo would make #1 look claimed by "alan", so it is refused
    r = await line.sync(issues, viewer="someone-else", expect=409)
    assert "operator alan" in r["detail"] and "--change-operator" in r["detail"]
    assert (await line.states())["1"] == "ready"        # nothing was written by the refused sync
    # NEGATIVE: an unknown operator is refused rather than treating every assignee as a claim
    r = await line.client.post("/api/queue/sync", json={"agent_id": line.ag["a"], "repo": line.repo, "issues": issues})
    assert r.status_code == 409 and "operator login is unknown" in r.json()["detail"]
    # POSITIVE: switching operator is possible, but only on request
    await line.sync(issues, viewer="someone-else", change_operator=True)
    assert (await line.states())["1"] == "external"


@pytest.mark.anyio
async def test_an_implausibly_large_closure_is_held_back_unless_forced(line):
    full = [line.iss(n, ["P1", "sp-1"]) for n in range(1, 13)]
    await line.sync(full)
    # NEGATIVE: a login that sees 3 of 12 would "close" 9; that is a partial view, so nothing closes
    r = await line.sync(full[:3], complete=True)
    assert r["closed_upstream"] == 0 and "looks like a partial view" in r["closure_skipped"]
    assert sum(v == "closed" for v in (await line.states()).values()) == 0
    # POSITIVE: a small closure (1 of 12) is applied normally
    r = await line.sync(full[:11], complete=True)
    assert r["closed_upstream"] == 1 and r["closure_skipped"] is None
    # POSITIVE: the same large closure goes through when the caller confirms it
    r = await line.sync(full[:3], complete=True, force_close=True)
    assert r["closed_upstream"] == 8


def _r(n, band, pts, cluster=None, repo="zz/x"):
    return {"source_ref": f"{repo}#{n}", "repo": repo, "band": band, "points": pts, "cluster": cluster, "gh_created_at": None}


def test_clusters_take_the_best_member_priority_and_split_at_the_point_cap():
    from app.workq import form_clusters

    units = form_clusters([_r(1, 3, 2, "ingest"), _r(2, 1, 5, "ingest"), _r(3, 2, 1),          # P3+P1 cluster, a P2 single
                           _r(4, 3, 8, "big"), _r(5, 3, 8, "big"),                              # 16 points: split at 13
                           _r(6, 2, 2, "ingest", repo="zz/other")])                              # same name, other repo
    refs = [[m["source_ref"].split("#")[1] for m in u] for u in units]
    assert refs[0] == ["2", "1"]                       # P1 cluster first, led by its highest member; the P3 rides along
    assert ["3"] in refs and ["6"] in refs             # a single stays single; a cluster never spans repositories
    assert sorted(m for u in refs if u[0] in "45" for m in u) == ["4", "5"] and ["4", "5"] not in refs  # over the cap: split


@pytest.mark.anyio
async def test_work_next_claims_a_whole_cluster_or_nothing(line):
    c, a, b = line.client, line.ag["a"], line.ag["b"]
    await line.sync([line.iss(1, ["P3", "sp-2", "cluster:retry"]), line.iss(2, ["P1", "sp-3", "cluster:retry"]),
                     line.iss(3, ["P2", "sp-1"])])
    r = (await c.post("/api/queue/next", json={"agent_id": a, "queue": line.repo})).json()
    assert [m["source_ref"].split("#")[1] for m in r["members"]] == ["2", "1"]      # POSITIVE: both members, P1 leads
    assert r["cluster_priority"] == 1 and r["cluster_points"] == 5
    st = await line.states()
    assert st["1"] == st["2"] == "claimed" and st["3"] == "ready"
    # the holder resumes the same unit; NEGATIVE: the other seat gets the single, never a half cluster
    assert (await c.post("/api/queue/next", json={"agent_id": a, "queue": line.repo})).json()["resumed"] is True
    other = (await c.post("/api/queue/next", json={"agent_id": b, "queue": line.repo})).json()
    assert [m["source_ref"].split("#")[1] for m in other["members"]] == ["3"]
