import json
from datetime import datetime, timezone

import pytest

from covasant_partyline import cli, ghclaim

REF = "issue:covasant/aura#4836"
NOW = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)


class FakeGh:
    """An in-memory issue that answers the gh calls ghclaim makes."""

    def __init__(self, state="OPEN", labels=(), assignees=(), comments=None, fail=None):
        self.state, self.labels, self.assignees = state, list(labels), list(assignees)
        self.comments = comments or []  # dicts: id, body, user, assoc
        self.calls, self.fail, self._next = [], fail, 100

    def run(self, args):
        self.calls.append(args)
        if self.fail and self.fail in " ".join(args):
            return 1, "", "HTTP 403: Resource not accessible"
        if args[:2] == ["issue", "view"]:
            return 0, json.dumps({"state": self.state, "labels": [{"name": n} for n in self.labels],
                                  "assignees": [{"login": a} for a in self.assignees]}), ""
        if args[:2] == ["api", "user"]:
            return 0, "alanatcovasant" + chr(10), ""
        if args[0] == "api" and "--paginate" in args:
            page = [{"id": c["id"], "body": c["body"], "user": {"login": c["user"]},
                     "author_association": c["assoc"]} for c in self.comments]
            return 0, json.dumps([page]), ""
        if args[0] == "api" and "-X" in args and args[2] == "DELETE":
            cid = int(args[3].rsplit("/", 1)[1])
            self.comments = [c for c in self.comments if c["id"] != cid]
            return 0, "", ""
        if args[0] == "api" and "-X" in args:  # PATCH a comment
            cid = int(args[3].rsplit("/", 1)[1])
            body = next(a for a in args if a.startswith("body="))[5:]
            next(c for c in self.comments if c["id"] == cid)["body"] = body
            return 0, "{}", ""
        if args[0] == "api":  # POST a comment
            self._next += 1
            body = next(a for a in args if a.startswith("body="))[5:]
            self.comments.append({"id": self._next, "body": body, "user": "alanatcovasant", "assoc": "OWNER"})
            return 0, json.dumps({"id": self._next}), ""
        if args[:2] == ["label", "create"]:
            return 0, "", ""
        if args[:2] == ["issue", "edit"]:
            if "--add-label" in args and ghclaim.LABEL not in self.labels:
                self.labels.append(ghclaim.LABEL)
            if "--remove-label" in args and ghclaim.LABEL in self.labels:
                self.labels.remove(ghclaim.LABEL)
            return 0, "", ""
        return 1, "", f"unhandled {args}"


def test_marker_round_trips_and_ignores_plain_text():
    body = ghclaim.build_comment("alan_aura_2", "aura_2", "fix/4836", "2026-11-03T16:00Z", "re-run validator")
    mk = ghclaim.parse_marker(body)
    assert mk["seat"] == "alan_aura_2" and mk["folder"] == "aura_2" and mk["state"] == "claimed"
    assert "re-run validator" in body and "fix/4836" in body
    assert ghclaim.parse_marker("just a comment <!-- other -->") is None
    assert ghclaim.parse_ref("issue:covasant/aura#4836") == ("covasant", "aura", 4836)
    with pytest.raises(ValueError):
        ghclaim.parse_ref("aura#4836")


def test_claim_writes_comment_and_label_once():
    gh = FakeGh()
    r = ghclaim.project_claim(gh, REF, "alan_aura_2", "aura_2", "fix/4836", now=NOW)
    assert r["warnings"] == [] and r["written"] == ["comment created", "label added"]
    assert gh.labels == [ghclaim.LABEL] and len(gh.comments) == 1
    # a repeat claim by the same seat edits in place: no second comment, original time kept
    ghclaim.project_claim(gh, REF, "alan_aura_2", "aura_2", "fix/4836", next_action="open the PR",
                          now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    assert len(gh.comments) == 1 and "open the PR" in gh.comments[0]["body"]
    assert 'since="2026-11-03T16:00Z"' in gh.comments[0]["body"]


def test_release_marks_comment_and_removes_label_only_for_the_holder():
    gh = FakeGh()
    ghclaim.project_claim(gh, REF, "alan_aura_2", "aura_2", now=NOW)
    # NEGATIVE: another seat cannot release it in GitHub
    r = ghclaim.release_claim(gh, REF, "alan_aura_3", "not mine")
    assert r["written"] == [] and gh.labels == [ghclaim.LABEL]
    # POSITIVE
    r = ghclaim.release_claim(gh, REF, "alan_aura_2", "done")
    assert r["written"] == ["comment edited", "label removed"] and gh.labels == []
    assert ghclaim.parse_marker(gh.comments[0]["body"])["state"] == "released"
    assert "Released by" in gh.comments[0]["body"]


@pytest.mark.parametrize("fake,via", [
    (dict(labels=[ghclaim.LABEL]), "label"),
    (dict(comments=[{"id": 1, "body": "/claim seat=alan_aura_9", "user": "bob", "assoc": "MEMBER"}]), "command"),
])
def test_foreign_claims_are_never_overwritten(fake, via):
    gh = FakeGh(**fake)
    r = ghclaim.project_claim(gh, REF, "alan_aura_2", "aura_2", now=NOW)
    assert r["written"] == [] and "already claimed in GitHub" in r["warnings"][0] and via in r["warnings"][0]
    assert not any(c[:2] == ["issue", "edit"] for c in gh.calls)
    state = ghclaim.read_state(gh, REF)
    assert ghclaim.foreign_claim(state, "alan_aura_2")["via"] == via


def test_an_assignee_is_foreign_only_when_it_is_not_the_account_we_act_as():
    mine = FakeGh(assignees=["alanatcovasant"])  # the operator account: ordinary, never a claim
    assert ghclaim.foreign_claim(ghclaim.read_state(mine, REF), "alan_aura_2") is None
    assert ghclaim.project_claim(mine, REF, "alan_aura_2", now=NOW)["written"] == ["comment created", "label added"]
    other = FakeGh(assignees=["bob-dev"])
    st = ghclaim.read_state(other, REF)
    assert ghclaim.foreign_claim(st, "alan_aura_2") == {"claimant": "bob-dev", "via": "assignee", "partyline": False}
    r = ghclaim.project_claim(other, REF, "alan_aura_2", now=NOW)
    assert r["written"] == [] and "bob-dev" in r["warnings"][0]


def test_a_claim_command_from_a_non_collaborator_is_ignored():
    gh = FakeGh(comments=[{"id": 1, "body": "/claim", "user": "stranger", "assoc": "NONE"}])
    assert ghclaim.foreign_claim(ghclaim.read_state(gh, REF), "alan_aura_2") is None
    assert ghclaim.project_claim(gh, REF, "alan_aura_2", now=NOW)["written"] == ["comment created", "label added"]


def test_stale_marker_from_another_partyline_seat_is_replaced():
    old = ghclaim.build_comment("alan_aura_9", "aura_9", None, "2026-11-01T00:00Z")
    gh = FakeGh(labels=[ghclaim.LABEL], comments=[{"id": 7, "body": old, "user": "alanatcovasant", "assoc": "OWNER"}])
    r = ghclaim.project_claim(gh, REF, "alan_aura_2", "aura_2", now=NOW)
    assert "replaced a stale claim marker for alan_aura_9" in r["warnings"][0]
    assert ghclaim.parse_marker(gh.comments[0]["body"])["seat"] == "alan_aura_2" and len(gh.comments) == 1


def test_label_removed_by_a_human_reads_as_a_release_request():
    gh = FakeGh()
    ghclaim.project_claim(gh, REF, "alan_aura_2", "aura_2", now=NOW)
    assert ghclaim.release_requested(ghclaim.read_state(gh, REF)) is False
    gh.labels.clear()  # a person removes the label
    assert ghclaim.release_requested(ghclaim.read_state(gh, REF)) is True


def test_github_failure_never_raises_and_is_reported_as_unprojected():
    gh = FakeGh(fail="issues/101")
    r = ghclaim.project_claim(FakeGh(fail="--paginate"), REF, "a", now=NOW)
    assert r["written"] == [] and r["warnings"][0].startswith("unprojected:")
    assert ghclaim.project_claim(FakeGh(state="CLOSED"), REF, "a", now=NOW)["warnings"] == ["issue is closed; no marks written"]
    assert ghclaim.release_claim(FakeGh(fail="--paginate"), REF, "a")["warnings"][0].startswith("unreleased in GitHub")
    assert ghclaim.project_claim(gh, "not-a-ref", "a")["warnings"][0].startswith("unprojected:")


def test_claim_issue_cli_projects_only_when_asked_and_never_blocks(monkeypatch, tmp_path, capsys):
    seen = []
    monkeypatch.setattr(cli, "_request", lambda *a: {"claimed": True})
    monkeypatch.setattr(ghclaim, "project_claim",
                        lambda gh, ref, seat, folder, branch, **kw: seen.append((ref, seat, folder)) or
                        {"ref": ref, "written": ["comment created"], "warnings": ["unprojected: boom"]})
    monkeypatch.delenv("PARTYLINE_GH_CLAIM", raising=False)
    folder = tmp_path / "Aura_2"
    folder.mkdir()
    monkeypatch.chdir(folder)
    base = ["claim-issue", "alan_aura_2", "aura", "issue:covasant/aura#4836"]
    args = cli.build_parser().parse_args(base)
    args.func(args)
    assert seen == []  # off by default
    args = cli.build_parser().parse_args(base + ["--github"])
    args.func(args)  # a warning is printed, the command still succeeds
    assert seen == [("issue:covasant/aura#4836", "alan_aura_2", "aura_2")]
    assert "unprojected: boom" in capsys.readouterr().err
    monkeypatch.setenv("PARTYLINE_GH_CLAIM", "1")
    args = cli.build_parser().parse_args(base)
    args.func(args)
    assert len(seen) == 2
    # a PR claim is not projected
    args = cli.build_parser().parse_args(["claim-issue", "a", "aura", "pr:covasant/aura#1", "--github"])
    args.func(args)
    assert len(seen) == 2
