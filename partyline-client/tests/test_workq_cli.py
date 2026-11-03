import json

from covasant_partyline import cli, ghclaim, workq


class FakeGh:
    def __init__(self, issues, viewer="alanatcovasant", fail=False):
        self.issues, self.viewer, self.fail, self.calls = issues, viewer, fail, []

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            if self.fail:
                return 1, "", "HTTP 404"
            return 0, json.dumps(self.issues[: int(args[args.index("--limit") + 1])]), ""
        if args[:2] == ["api", "user"]:
            return 0, self.viewer + chr(10), ""
        return 1, "", "unhandled"


def issue(n, labels=(), assignees=()):
    return {"number": n, "title": f"t{n}", "url": f"https://x/{n}", "state": "OPEN",
            "labels": [{"name": x} for x in labels], "assignees": [{"login": x} for x in assignees],
            "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-02T00:00:00Z"}


def test_fetch_flattens_gh_json_and_reports_the_viewer():
    got = workq.fetch_issues(FakeGh([issue(1, ["P1", "sp-3"], ["bob"])]), "o/r")
    assert got["viewer"] == "alanatcovasant" and got["complete"] is True
    assert got["issues"][0] == {"number": 1, "title": "t1", "url": "https://x/1", "state": "open",
                                "labels": ["P1", "sp-3"], "assignees": ["bob"],
                                "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z"}


def test_a_list_that_hit_the_limit_is_not_complete_so_closure_is_never_inferred():
    gh = FakeGh([issue(n) for n in range(1, 6)])
    assert workq.fetch_issues(gh, "o/r", limit=5)["complete"] is False   # exactly the limit: may be truncated
    assert workq.fetch_issues(gh, "o/r", limit=6)["complete"] is True


def test_gh_failure_is_an_error_not_an_empty_queue():
    import pytest

    with pytest.raises(RuntimeError, match="gh issue list failed"):
        workq.fetch_issues(FakeGh([], fail=True), "o/r")


def test_sync_posts_what_gh_returned_and_partial_never_claims_completeness(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(cli, "_request", lambda m, u, path, body=None: sent.append((m, path, body)) or {"ok": 1})
    monkeypatch.setattr(ghclaim, "Gh", lambda: FakeGh([issue(1, ["P1", "sp-2"]), issue(2)]))
    p = cli.build_parser()
    p.parse_args(["queue", "sync", "--repo", "o/r", "--agent-id", "a"]).func(
        p.parse_args(["queue", "sync", "--repo", "o/r", "--agent-id", "a"]))
    assert sent[-1][1] == "/api/queue/sync" and sent[-1][2]["complete"] is True and len(sent[-1][2]["issues"]) == 2
    a = p.parse_args(["queue", "sync", "--repo", "o/r", "--agent-id", "a", "--partial"])
    a.func(a)
    assert sent[-1][2]["complete"] is False      # NEGATIVE: --partial overrides completeness


def test_work_next_projects_to_github_only_when_asked(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(cli, "_request", lambda m, u, path, body=None: {"source_ref": "o/r#7", "state": "claimed"})
    monkeypatch.setattr(ghclaim, "project_claim",
                        lambda gh, ref, seat, folder, branch, **kw: seen.append((ref, seat)) or
                        {"ref": ref, "written": ["comment created"], "warnings": []})
    monkeypatch.delenv("PARTYLINE_GH_CLAIM", raising=False)
    p = cli.build_parser()
    a = p.parse_args(["work", "next", "--agent-id", "alan_x_1"])
    a.func(a)
    assert seen == []
    a = p.parse_args(["work", "next", "--agent-id", "alan_x_1", "--github"])
    a.func(a)
    assert seen == [("issue:o/r#7", "alan_x_1")]
