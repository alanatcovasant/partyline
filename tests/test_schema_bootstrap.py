"""Unit tests for init_schema's statement-granular, privilege-tolerant bootstrap.

After an app rename/replace, tables may be owned by a prior identity: ownership-
requiring statements (ALTER ... REPLICA IDENTITY) must be skipped with a warning,
never crash the app (the failure mode that took the renamed app down on boot).
"""

import psycopg
import pytest

from app import db


class FakeConn:
    def __init__(self, fail_on=()):
        self.fail_on = fail_on
        self.executed = []
        self.rollbacks = 0

    def execute(self, stmt, params=None):
        if any(marker in stmt for marker in self.fail_on):
            raise psycopg.errors.InsufficientPrivilege("must be owner of table x")
        self.executed.append(" ".join(stmt.split()))

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    class _Ctx:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, *a):
            return False

    def connection(self):
        return self._Ctx(self._conn)


def _run(monkeypatch, conn):
    monkeypatch.setattr(db, "get_pool", lambda: FakePool(conn))
    db.init_schema()


def test_replica_identity_failures_are_nonfatal(monkeypatch):
    conn = FakeConn(fail_on=("REPLICA IDENTITY",))
    _run(monkeypatch, conn)  # must not raise
    assert conn.rollbacks == len(db._REPLICA_IDENTITY)
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS messages") for s in conn.executed)


def test_any_privilege_failure_skips_only_that_statement(monkeypatch):
    conn = FakeConn(fail_on=("CREATE INDEX",))
    _run(monkeypatch, conn)
    executed = " | ".join(conn.executed)
    assert "CREATE INDEX" not in executed          # the failing one skipped
    assert "CREATE TABLE IF NOT EXISTS claims" in executed  # later ones still ran
    assert conn.rollbacks >= 1


def test_clean_run_executes_everything(monkeypatch):
    conn = FakeConn()
    _run(monkeypatch, conn)
    assert conn.rollbacks == 0
    executed = " | ".join(conn.executed)
    for table in ("agents", "messages", "messages_archive", "checkpoints", "claims"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in executed
    assert executed.count("REPLICA IDENTITY") == len(db._REPLICA_IDENTITY)


def test_non_privilege_errors_still_raise(monkeypatch):
    class Boom(FakeConn):
        def execute(self, stmt, params=None):
            raise psycopg.errors.SyntaxError("broken DDL")

    with pytest.raises(psycopg.errors.SyntaxError):
        _run(monkeypatch, Boom())  # only InsufficientPrivilege is tolerated


def test_schema_create_table_declares_three_clocks():
    # The base CREATE TABLE is the source of truth: fresh DBs get the columns here, and
    # messages_archive (LIKE messages INCLUDING ALL) only inherits them if declared here.
    from app.db import _SCHEMA
    norm = " ".join(_SCHEMA.split())
    assert "event_time TIMESTAMPTZ" in norm
    assert "received_at TIMESTAMPTZ" in norm


def test_migrations_add_three_clock_columns():
    from app.db import _MIGRATIONS
    joined = " ".join(" ".join(m.split()) for m in _MIGRATIONS)
    # event_time + received_at added to both live and archive tables (existing DBs)
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ" in joined
    assert "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ" in joined
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ" in joined
    assert "ALTER TABLE messages_archive ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ" in joined
    # existing rows backfilled so ordering stays sane
    assert "SET event_time = created_at" in joined
    assert "SET received_at = created_at" in joined


def test_schema_split_produces_only_valid_statements():
    # A semicolon inside an _SCHEMA comment would corrupt _SCHEMA.split(";"),
    # truncating a CREATE TABLE mid-comment and yielding garbage fragments that
    # crash init_schema() on a fresh DB. Guard against that whole class of bug.
    from app.db import _SCHEMA

    # "--" is included because a full-line SQL comment legitimately precedes a
    # statement in a few places (e.g. before idx_messages_agent_status) — Postgres
    # ignores such comments, so that's valid SQL, not a garbage fragment. A semicolon
    # truncating mid-comment instead lands on bare prose (e.g. "the"), which is not in
    # this list and still fails, so this doesn't mask the bug under test.
    _KEYWORDS = ("CREATE", "ALTER", "DROP", "COMMENT", "GRANT", "INSERT", "UPDATE", "DO", "SET", "--")
    statements = [s.strip() for s in _SCHEMA.split(";") if s.strip()]
    for stmt in statements:
        first = stmt.split(None, 1)[0].upper()
        assert first in _KEYWORDS, f"fragment does not start with a DDL keyword: {stmt[:60]!r}"
