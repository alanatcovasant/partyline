"""Subtree-overlap conflict tests (needs local Postgres: docker compose up -d)."""

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean_test_claims(started_app):
    """Drop this module's claims before each test so the suite is re-runnable.

    Claims are TTL'd, not transactional, so a test that asserts mid-way leaves its leases
    behind and poisons the next run — and a leftover parent claim now (correctly) blocks
    the child claim the next test tries to take.
    """
    from app import db

    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM claims WHERE resource_path LIKE '/t/%%' "
            "OR resource_path LIKE 'issue:covasant/aura#%%'"
        )
        conn.commit()
    yield


async def _register(client, agent_id):
    r = await client.post("/api/agents", json={
        "agent_id": agent_id, "owner": "test", "repo": "aura", "branch": "main",
    })
    assert r.status_code == 200


async def _claim(client, agent_id, path):
    return await client.post("/api/claims", json={
        "agent_id": agent_id, "repo": "aura", "resource_path": path,
        "claim_scope": "exclusive", "ttl_seconds": 300,
    })


async def _release(client, agent_id, path):
    await client.post("/api/claims/release", json={
        "agent_id": agent_id, "repo": "aura", "resource_path": path,
    })


async def test_parent_claim_blocks_child(client):
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl1/pkg")).json()["acquired"] is True
    refused = (await _claim(client, "ovl_b", "/t/ovl1/pkg/sub/mod")).json()
    assert refused["acquired"] is False
    assert refused["claim"]["agent_id"] == "ovl_a"
    await _release(client, "ovl_a", "/t/ovl1/pkg")


async def test_child_claim_blocks_parent(client):
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl2/pkg/sub")).json()["acquired"] is True
    refused = (await _claim(client, "ovl_b", "/t/ovl2/pkg")).json()
    assert refused["acquired"] is False
    assert refused["claim"]["agent_id"] == "ovl_a"
    await _release(client, "ovl_a", "/t/ovl2/pkg/sub")


async def test_siblings_do_not_conflict(client):
    """/a/foo and /a/foobar are unrelated — this is why the prefix test appends '/'."""
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl3/foo")).json()["acquired"] is True
    assert (await _claim(client, "ovl_b", "/t/ovl3/foobar")).json()["acquired"] is True
    await _release(client, "ovl_a", "/t/ovl3/foo")
    await _release(client, "ovl_b", "/t/ovl3/foobar")


async def test_underscore_is_not_a_wildcard(client):
    """LIKE would match aura_core against auraXcore. starts_with must not."""
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "/t/ovl4/aura_core")).json()["acquired"] is True
    assert (await _claim(client, "ovl_b", "/t/ovl4/auraxcore/sub")).json()["acquired"] is True
    await _release(client, "ovl_a", "/t/ovl4/aura_core")
    await _release(client, "ovl_b", "/t/ovl4/auraxcore/sub")


async def test_agent_may_refine_its_own_territory(client):
    await _register(client, "ovl_a")
    assert (await _claim(client, "ovl_a", "/t/ovl5/pkg")).json()["acquired"] is True
    assert (await _claim(client, "ovl_a", "/t/ovl5/pkg/sub")).json()["acquired"] is True
    await _release(client, "ovl_a", "/t/ovl5/pkg")
    await _release(client, "ovl_a", "/t/ovl5/pkg/sub")


async def test_expired_parent_does_not_block(client):
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    r = await client.post("/api/claims", json={
        "agent_id": "ovl_a", "repo": "aura", "resource_path": "/t/ovl6/pkg",
        "claim_scope": "exclusive", "ttl_seconds": 1,
    })
    assert r.json()["acquired"] is True
    import anyio
    await anyio.sleep(1.5)
    assert (await _claim(client, "ovl_b", "/t/ovl6/pkg/sub")).json()["acquired"] is True
    await _release(client, "ovl_b", "/t/ovl6/pkg/sub")


async def test_distinct_work_items_do_not_conflict(client):
    """issue #1 must not prefix-match issue #12 — the appended '/' guards this too."""
    await _register(client, "ovl_a")
    await _register(client, "ovl_b")
    assert (await _claim(client, "ovl_a", "issue:covasant/aura#1")).json()["acquired"] is True
    assert (await _claim(client, "ovl_b", "issue:covasant/aura#12")).json()["acquired"] is True
    refused = (await _claim(client, "ovl_b", "issue:covasant/aura#1")).json()
    assert refused["acquired"] is False
    await _release(client, "ovl_a", "issue:covasant/aura#1")
    await _release(client, "ovl_b", "issue:covasant/aura#12")


async def test_concurrent_parent_and_child_have_exactly_one_winner(started_app):
    """The whole reason for the advisory lock: check-then-insert must be atomic.

    Without it both threads read an empty conflict set, both insert non-conflicting keys
    (the unique index is on resource_path, and parent != child), and both believe they
    hold exclusive territory.
    """
    import threading

    from app import db

    db.register_agent("race_a", "test", "aura", "main", None)
    db.register_agent("race_b", "test", "aura", "main", None)
    results: dict[str, bool] = {}
    start = threading.Barrier(2)

    def claim(agent: str, path: str) -> None:
        start.wait()
        results[agent] = db.claim_resource(
            agent, "aura", None, path, "directory", "exclusive", None, 300
        )[0]

    threads = [
        threading.Thread(target=claim, args=("race_a", "/t/race/pkg")),
        threading.Thread(target=claim, args=("race_b", "/t/race/pkg/sub")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results.values()) == 1, f"expected exactly one winner, got {results}"
    db.release_resource("race_a", "aura", None, "/t/race/pkg")
    db.release_resource("race_b", "aura", None, "/t/race/pkg/sub")


async def test_heartbeat_extends_workdir_claims(client):
    """A live session holds its territory; a dead one loses it one TTL later."""
    await _register(client, "ovl_hb")
    r = await client.post("/api/claims", json={
        "agent_id": "ovl_hb", "repo": "aura", "resource_path": "/t/ovl7/wd",
        "resource_type": "workdir", "claim_scope": "exclusive", "ttl_seconds": 60,
    })
    assert r.json()["acquired"] is True
    first_expiry = r.json()["claim"]["expires_at"]

    # Any later call from this agent heartbeats, which re-extends workdir claims.
    await client.post("/api/agents", json={
        "agent_id": "ovl_hb", "owner": "test", "repo": "aura", "branch": "main",
    })
    claims = (await client.get("/api/claims")).json()
    held = next(c for c in claims if c["resource_path"] == "/t/ovl7/wd")
    assert held["expires_at"] > first_expiry  # extended to the workdir TTL, not 60s
    await _release(client, "ovl_hb", "/t/ovl7/wd")


async def test_heartbeat_does_not_extend_ordinary_claims(client):
    await _register(client, "ovl_hb2")
    r = await client.post("/api/claims", json={
        "agent_id": "ovl_hb2", "repo": "aura", "resource_path": "/t/ovl8/pkg",
        "resource_type": "directory", "claim_scope": "exclusive", "ttl_seconds": 300,
    })
    first_expiry = r.json()["claim"]["expires_at"]
    await client.post("/api/agents", json={
        "agent_id": "ovl_hb2", "owner": "test", "repo": "aura", "branch": "main",
    })
    claims = (await client.get("/api/claims")).json()
    held = next(c for c in claims if c["resource_path"] == "/t/ovl8/pkg")
    assert held["expires_at"] == first_expiry  # task leases expire on their own schedule
    await _release(client, "ovl_hb2", "/t/ovl8/pkg")


async def test_migration_purges_duplicates_so_the_unique_index_can_exist(started_app):
    """Regression: the live table held duplicate resource_paths under the old composite key.

    Creating ux_claims_path without purging fails with UniqueViolation — and because
    migrate_schema SKIPS failures rather than aborting, the app would then run
    ON CONFLICT (resource_path) against an index that does not exist. Every claim would
    error at runtime with the deploy reporting success.
    """
    from app import db

    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM claims WHERE resource_path = '/t/dup/path'")
        conn.execute("DELETE FROM agents WHERE agent_id = 'dup_a'")
        conn.execute(
            "INSERT INTO agents (agent_id, owner, repo) VALUES ('dup_a', 't', 'aura') "
            "ON CONFLICT (agent_id) DO NOTHING"
        )
        # Two rows, same path, different repo — impossible to insert once the index exists,
        # so drop it first to recreate the pre-migration state.
        conn.execute("DROP INDEX IF EXISTS ux_claims_path")
        for repo in ("aura", "aura-web"):
            conn.execute(
                "INSERT INTO claims (agent_id, repo, resource_path, expires_at) "
                "VALUES ('dup_a', %s, '/t/dup/path', now() - interval '1 hour')",
                (repo,),
            )
        conn.commit()

        # Applying the migration list must clear the duplicates and leave the index in place.
        for stmt in db._MIGRATIONS:
            if "claims" in stmt:
                conn.execute(stmt)
        conn.commit()

        remaining = conn.execute(
            "SELECT count(*) AS n FROM claims WHERE resource_path = '/t/dup/path'"
        ).fetchone()["n"]
        assert remaining == 0, "expired duplicates should have been purged"
        idx = conn.execute(
            "SELECT count(*) AS n FROM pg_indexes WHERE indexname = 'ux_claims_path'"
        ).fetchone()["n"]
        assert idx == 1, "ux_claims_path must exist after migration"
