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
