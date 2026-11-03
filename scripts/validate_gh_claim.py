"""Check that a Partyline claim is made visible in GitHub, and that foreign claims are respected.

    python scripts/validate_gh_claim.py --issue owner/repo#N     # LIVE: mutates that issue, then cleans up
    python scripts/validate_gh_claim.py --selftest               # offline, against an in-memory fake of gh

Use a SCRATCH issue, never a real one: the live run adds and removes a label and a comment on it.

Positive: a claim leaves one claim comment (with the machine marker) and the `partyline:claimed` label;
a repeat claim edits the comment in place; a release marks it released and removes the label.
Negative: another seat cannot release your claim; a label with no marker (a foreign claim) is never
overwritten; a `/claim` comment from a non-collaborator is ignored; a closed issue gets no marks.
Prints PASS/FAIL per case; exits non-zero on any failure.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "partyline-client"))
from covasant_partyline import ghclaim  # noqa: E402

failed = 0


def check(name, ok, detail=""):
    global failed
    print(("PASS" if ok else "FAIL") + f": {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failed += not ok


def run(gh, ref, live):
    seat, other = "zz_ghclaim_validate_a", "zz_ghclaim_validate_b"
    owner, repo, number = ghclaim.parse_ref(ref)

    def state():
        return ghclaim.read_state(gh, ref)

    r = ghclaim.project_claim(gh, ref, seat, "zz-validate", "zz/branch", "check the marks")
    st = state()
    check("POS: a claim writes the comment and the label",
          r["warnings"] == [] and st["labeled"] and (st["marker"] or {}).get("seat") == seat, str(r))
    first_id = st["comment_id"]
    ghclaim.project_claim(gh, ref, seat, "zz-validate", "zz/branch", "second call")
    st = state()
    check("POS: a repeat claim edits the same comment (no duplicate)", st["comment_id"] == first_id, str(st))

    r = ghclaim.release_claim(gh, ref, other, "not mine")
    check("NEG: another seat cannot release the claim in GitHub", r["written"] == [] and state()["labeled"], str(r))
    r = ghclaim.release_claim(gh, ref, seat, "validation over")
    st = state()
    check("POS: release marks the comment released and removes the label",
          not st["labeled"] and (st["marker"] or {}).get("state") == "released", str(r))

    # a foreign claim: the label with no Partyline marker on top of a released one
    gh.run(["label", "create", ghclaim.LABEL, "--repo", f"{owner}/{repo}", "--force"])
    gh.run(["issue", "edit", str(number), "--repo", f"{owner}/{repo}", "--add-label", ghclaim.LABEL])
    st = state()
    check("detects a label claim with no live marker as foreign",
          (ghclaim.foreign_claim(st, seat) or {}).get("via") == "label", str(st))
    gh.run(["issue", "edit", str(number), "--repo", f"{owner}/{repo}", "--remove-label", ghclaim.LABEL])

    # cleanup: remove every comment carrying our marker
    for c in ghclaim._comments(gh, owner, repo, number):
        if ghclaim.parse_marker(c["body"]):
            gh.run(["api", "-X", "DELETE", f"repos/{owner}/{repo}/issues/comments/{c['id']}"])
    st = state()
    check("cleanup: no claim comment or label left", st["marker"] is None and not st["labeled"], str(st))


def selftest():
    sys.path.insert(0, str(HERE.parent / "partyline-client" / "tests"))
    from test_ghclaim import FakeGh

    run(FakeGh(), "issue:zz/scratch#1", live=False)
    # offline-only negatives that would need a second account or a closed issue to run live
    gh = FakeGh(comments=[{"id": 1, "body": "/claim seat=x", "user": "stranger", "assoc": "NONE"}])
    check("NEG: a /claim from a non-collaborator is ignored",
          ghclaim.foreign_claim(ghclaim.read_state(gh, "zz/scratch#1"), "me") is None)
    check("NEG: a closed issue gets no marks",
          ghclaim.project_claim(FakeGh(state="CLOSED"), "zz/scratch#1", "me")["written"] == [])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", help="owner/repo#N of a SCRATCH issue (mutated, then cleaned up)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.issue:
        run(ghclaim.Gh(), a.issue, live=True)
    else:
        ap.error("pass --issue owner/repo#N (a scratch issue) or --selftest")
    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    sys.exit(1 if failed else 0)
