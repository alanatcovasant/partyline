"""Unit tests for the retention job (covasant_partyline_jobs.main).

Verifies the statement *sequence* — archive INSERT, reply_to detach UPDATE,
DELETE, single commit — without a database, by faking psycopg.connect.
The transactional guarantee (archive and prune together) is exactly the
ordering + single-commit behavior asserted here.
"""

import sys

import pytest

from covasant_partyline_jobs import main as job


class FakeCursor:
    def __init__(self, log):
        self.log = log
        self.rowcount = 3

    def execute(self, sql, params=None):
        self.log.append(" ".join(sql.split()))

    def fetchone(self):
        return (3,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, log):
        self.log = log
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def fake_pg(monkeypatch):
    log = []
    conn = FakeConn(log)
    monkeypatch.setattr(job.psycopg, "connect", lambda conninfo, **kw: conn)
    monkeypatch.setenv("PARTYLINE_PG_CONNINFO", "host=fake")  # bypass SDK entirely
    return log, conn


def test_archive_then_detach_then_delete(fake_pg, monkeypatch):
    log, conn = fake_pg
    monkeypatch.setattr(sys, "argv", ["job", "--retention-days", "30"])
    job.main()

    assert len(log) == 3
    assert log[0].startswith("INSERT INTO partyline.messages_archive")
    assert "ON CONFLICT (id) DO NOTHING" in log[0]
    assert log[1].startswith("UPDATE partyline.messages SET reply_to_id = NULL")
    assert log[2].startswith("DELETE FROM partyline.messages")
    # archive+prune commit together: one commit, after all three statements
    assert conn.commits == 1


def test_dry_run_only_counts(fake_pg, monkeypatch, capsys):
    log, conn = fake_pg
    monkeypatch.setattr(sys, "argv", ["job", "--retention-days", "30", "--dry-run"])
    job.main()

    assert len(log) == 1
    assert log[0].startswith("SELECT count(*) FROM partyline.messages")
    assert conn.commits == 0
    assert "Would delete 3" in capsys.readouterr().out


def test_all_statements_schema_qualified(fake_pg, monkeypatch):
    log, _ = fake_pg
    monkeypatch.setattr(sys, "argv", ["job"])
    job.main()
    for stmt in log:
        assert "partyline.messages" in stmt  # never unqualified (pre-fix regression)
