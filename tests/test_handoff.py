import dataclasses
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.handoff import Handoff


def test_handoff_input_is_validated():
    ok = dict(agent_id="a", action="offer", to="b")
    Handoff(**ok)
    for bad in ({"action": "steal"}, {"folder": "../x"}, {"agent_id": ""}):
        with pytest.raises(ValidationError):
            Handoff(**{**ok, **bad})
    assert Handoff(**ok, folder=" Aura_2 ").folder == "aura_2"


@pytest.fixture()
async def ledger(client):
    """Three registered agents and a helper bundle; cleans every row it created."""
    from app import db

    sfx = uuid4().hex[:8]
    ag = {k: f"ho_{k}_{sfx}" for k in "abc"}
    folders = {k: f"ho-{k}-{sfx}" for k in "abc"}
    for a in ag.values():
        await client.post("/api/agents", json={"agent_id": a, "owner": "test", "repo": "test"})

    async def start(agent, folder, **kw):
        r = await client.post("/api/progress", json={"folder": folder, "agent_id": agent, "status": "started",
                                                      "summary": "work", **kw})
        assert r.status_code == 200, r.text
        return r.json()["task_id"]

    async def ho(task, agent, action, **kw):
        return await client.post(f"/api/progress/{task}/handoff", json={"agent_id": agent, "action": action, **kw})

    async def row(task, folder):
        rows = (await client.get("/api/progress", params={"folder": folder, "include_done": "true"})).json()
        return next(r for r in rows if r["id"] == task)

    def make_stale(agent):
        with db.get_pool().connection() as conn:
            conn.execute("UPDATE agents SET last_heartbeat = now() - interval '3 days' WHERE agent_id=%s", (agent,))
            conn.commit()

    class L:
        pass

    L.client, L.ag, L.folders, L.start, L.ho, L.row, L.make_stale = client, ag, folders, start, ho, row, make_stale
    yield L
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM progress_tasks WHERE agent_id = ANY(%s) OR folder = ANY(%s)",
                     (list(ag.values()), list(folders.values())))
        conn.execute("DELETE FROM agents WHERE agent_id = ANY(%s)", (list(ag.values()),))
        conn.commit()


@pytest.mark.anyio
async def test_offer_accept_moves_the_task_and_keeps_history(ledger):
    L = ledger
    a, b, c = L.ag["a"], L.ag["b"], L.ag["c"]
    t = await L.start(a, L.folders["a"], next_action="run the validator")
    assert (await L.row(t, L.folders["a"]))["next_action"] == "run the validator"

    # NEGATIVE: only the owner can offer; an offer to an unregistered agent is refused; accept needs an offer
    assert (await L.ho(t, b, "offer", to=c)).status_code == 409
    assert (await L.ho(t, a, "offer", to="nobody_registered")).status_code == 409
    assert (await L.ho(t, a, "offer")).status_code == 409
    assert (await L.ho(t, b, "accept", folder=L.folders["b"])).status_code == 409

    # POSITIVE: offer to b with a note and new next action
    r = await L.ho(t, a, "offer", to=b, note="out of tokens", next_action="re-run step 3")
    assert r.status_code == 200 and r.json()["handoff_to"] == b and r.json()["agent_id"] == a
    # NEGATIVE: the wrong agent, the owner, and a missing folder cannot accept
    assert (await L.ho(t, c, "accept", folder=L.folders["c"])).status_code == 409
    assert (await L.ho(t, a, "accept", folder=L.folders["a"])).status_code == 409
    assert (await L.ho(t, b, "accept")).status_code == 409
    # POSITIVE: b accepts into its own folder
    r = await L.ho(t, b, "accept", folder=L.folders["b"])
    assert r.status_code == 200
    got = r.json()
    assert got["agent_id"] == b and got["folder"] == L.folders["b"] and got["handoff_from"] == a
    assert got["handoff_to"] is None and got["next_action"] == "re-run step 3" and got["handoff_note"] is None
    hist = (await L.client.get(f"/api/progress/{t}/history")).json()
    assert [h["status"] for h in hist][:2] == ["handoff_accepted", "handoff_offered"]
    # the first owner's folder no longer has it
    assert (await L.client.get("/api/progress", params={"folder": L.folders["a"]})).json() == []


@pytest.mark.anyio
async def test_decline_withdraw_wildcard_and_folder_clash(ledger):
    L = ledger
    a, b, c = L.ag["a"], L.ag["b"], L.ag["c"]
    t = await L.start(a, L.folders["a"])
    assert (await L.ho(t, a, "decline")).status_code == 409  # nothing to decline
    await L.ho(t, a, "offer", to=b)
    # the offeree declines; the offer is gone and the owner is unchanged
    r = await L.ho(t, b, "decline")
    assert r.status_code == 200 and r.json()["handoff_to"] is None and r.json()["agent_id"] == a
    # the owner can withdraw an offer; a third party cannot decline someone else's targeted offer
    await L.ho(t, a, "offer", to=b)
    assert (await L.ho(t, c, "decline")).status_code == 409
    assert (await L.ho(t, a, "decline")).status_code == 200
    # an offer to anyone can be accepted by any other agent, but not into a folder that is busy
    await L.ho(t, a, "offer", to="*")
    await L.start(c, L.folders["c"])
    clash = await L.ho(t, c, "accept", folder=L.folders["c"])
    assert clash.status_code == 409 and "already has a current task" in clash.text
    ok = await L.ho(t, b, "accept", folder=L.folders["b"])
    assert ok.status_code == 200 and ok.json()["agent_id"] == b


@pytest.mark.anyio
async def test_takeover_only_from_a_stale_owner_and_with_a_reason(ledger):
    L = ledger
    a, b = L.ag["a"], L.ag["b"]
    t = await L.start(a, L.folders["a"])
    # NEGATIVE: a live owner cannot be taken over
    r = await L.ho(t, b, "takeover", folder=L.folders["b"], note="idle")
    assert r.status_code == 409 and a in r.text and "stale" in r.text
    L.make_stale(a)
    # the stale owner's task is flagged orphaned, never reassigned by itself
    flagged = await L.row(t, L.folders["a"])
    assert flagged["owner_status"] == "stale" and flagged["orphaned"] is True and flagged["agent_id"] == a
    # NEGATIVE: a takeover without a reason is refused
    assert (await L.ho(t, b, "takeover", folder=L.folders["b"])).status_code == 409
    # POSITIVE
    r = await L.ho(t, b, "takeover", folder=L.folders["b"], note="a is gone for the day")
    assert r.status_code == 200 and r.json()["agent_id"] == b and r.json()["handoff_from"] == a
    hist = (await L.client.get(f"/api/progress/{t}/history")).json()
    assert hist[0]["status"] == "handoff_takeover" and "a is gone" in hist[0]["details"]
    assert (await L.row(t, L.folders["b"]))["orphaned"] is False


@pytest.mark.anyio
async def test_retry_is_idempotent_and_reuse_with_other_input_is_refused(ledger):
    L = ledger
    a, b = L.ag["a"], L.ag["b"]
    t = await L.start(a, L.folders["a"])
    op = str(uuid4())
    assert (await L.ho(t, a, "offer", to=b, operation_id=op)).status_code == 200
    assert (await L.ho(t, a, "offer", to=b, operation_id=op)).status_code == 200  # retry, no second event
    hist = (await L.client.get(f"/api/progress/{t}/history")).json()
    assert [h["status"] for h in hist].count("handoff_offered") == 1
    assert (await L.ho(t, a, "offer", to=L.ag["c"], operation_id=op)).status_code == 409
    # a finished task has nothing to hand off
    await L.client.post("/api/progress", json={"folder": L.folders["a"], "agent_id": a, "status": "done", "summary": "ok"})
    assert (await L.ho(t, a, "offer", to=b)).status_code == 409
    assert (await L.ho(str(uuid4()), a, "offer", to=b)).status_code == 404


@pytest.mark.anyio
async def test_fencing_is_off_by_default_and_refuses_non_owners_when_on(ledger, monkeypatch):
    import app.config

    L = ledger
    a, b = L.ag["a"], L.ag["b"]
    t = await L.start(a, L.folders["a"])
    update = {"folder": L.folders["a"], "agent_id": b, "status": "progress", "summary": "b writes", "task_id": t}
    # default: shared folders keep working (documented, so a regression is visible)
    assert (await L.client.post("/api/progress", json=update)).status_code == 200
    monkeypatch.setattr(app.config, "settings", dataclasses.replace(app.config.settings, progress_fencing=True))
    t2 = await L.start(a, L.folders["c"])
    refused = await L.client.post("/api/progress", json={**update, "folder": L.folders["c"], "task_id": t2})
    assert refused.status_code == 409 and a in refused.text
    owner_ok = await L.client.post("/api/progress", json={**update, "folder": L.folders["c"], "agent_id": a, "task_id": t2})
    assert owner_ok.status_code == 200


@pytest.mark.anyio
async def test_purge_only_removes_zz_test_folders(ledger):
    L = ledger
    a = L.ag["a"]
    zz, real = f"zz-{L.folders['a']}", L.folders["b"]
    await L.start(a, zz)
    await L.start(L.ag["b"], real)
    # NEGATIVE: a real folder is never purged
    assert (await L.client.delete("/api/progress", params={"folder": real})).status_code == 409
    assert len((await L.client.get("/api/progress", params={"folder": real})).json()) == 1
    # POSITIVE
    r = await L.client.delete("/api/progress", params={"folder": zz})
    assert r.status_code == 200 and r.json()["deleted_tasks"] == 1
    assert (await L.client.get("/api/progress", params={"folder": zz, "include_done": "true"})).json() == []
