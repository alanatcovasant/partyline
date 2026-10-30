from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.progress import ProgressUpdate


def test_progress_normalizes_folder_and_legacy_end_status():
    body = ProgressUpdate(folder=" Aura_1 ", agent_id="alan_aura_1", status="ENDED", summary=" complete ")
    assert body.folder == "aura_1" and body.status == "done" and body.summary == "complete"


@pytest.mark.parametrize("fields", [{"folder": "../aura_1"}, {"folder": ""}, {"summary": "  "}, {"status": "finished-maybe"}])
def test_invalid_progress_is_refused(fields):
    with pytest.raises(ValidationError):
        ProgressUpdate(**dict(dict(folder="aura_1", agent_id="a", status="started", summary="work"), **fields))


@pytest.mark.anyio
async def test_folder_backlog_lifecycle_history_and_retries(client):
    from app import db

    suffix = uuid4().hex[:10]
    folder, agent = f"progress-{suffix}", f"progress_agent_{suffix}"
    await client.post("/api/agents", json={"agent_id": agent, "owner": "test", "repo": "test"})
    base = {"folder": folder, "agent_id": agent, "workstream": "release"}
    try:
        queued = await client.post("/api/progress", json={**base, "status": "queued", "summary": "First task"})
        assert queued.status_code == 200, queued.text
        task_id = queued.json()["task_id"]
        other = await client.post("/api/progress", json={**base, "status": "queued", "summary": "Next task"})
        assert other.status_code == 200
        operation_id = str(uuid4())
        start = {**base, "task_id": task_id, "status": "started", "summary": "Working", "operation_id": operation_id}
        first = await client.post("/api/progress", json=start)
        assert first.status_code == 200, first.text
        assert (await client.post("/api/progress", json=start)).json()["operation_id"] == operation_id
        assert (await client.post("/api/progress", json={**start, "summary": "different"})).status_code == 409
        assert (await client.post("/api/progress", json={**base, "status": "started", "summary": "Conflicting work"})).status_code == 409
        note = await client.post("/api/progress", json={**base, "status": "progress", "summary": "Tests passed", "details": "Run: 10 assertions"})
        assert note.status_code == 200 and note.json()["task_id"] == task_id
        history = (await client.get(f"/api/progress/{task_id}/history")).json()
        assert len(history) == 3 and history[0]["details"] == "Run: 10 assertions"
        assert all("request" not in row for row in history)
        assert (await client.post("/api/progress", json={**base, "status": "done", "summary": "Accepted"})).status_code == 200
        active = (await client.get("/api/progress", params={"folder": folder})).json()
        assert len(active) == 1 and active[0]["status"] == "queued"
        assert len((await client.get("/api/progress", params={"folder": folder, "include_done": "true"})).json()) == 2
        assert (await client.post("/api/progress", json={**base, "task_id": task_id, "status": "progress", "summary": "stale update"})).status_code == 409
    finally:
        with db.get_pool().connection() as conn:
            conn.execute("DELETE FROM progress_tasks WHERE folder=%s", (folder,))
            conn.execute("DELETE FROM agents WHERE agent_id=%s", (agent,))
            conn.commit()
