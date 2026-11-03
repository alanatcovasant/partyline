"""Post-deploy check: the work queue (queue sync / work next) orders, gates and hands out work safely.

    python scripts/validate_workq.py [profile] [app-url]

Positive: a sync mirrors issues; `next` hands out ready work in band-then-points order, a seat resumes its own
item, a direct claim takes an item out of the queue, release returns it, a full sync closes what vanished.
Negative: untriaged and externally claimed issues are never handed out; the queue empties instead of
leaking them; a partial sync closes nothing; an out-of-scope agent gets nothing; only `zz/` repos purge;
an unauthenticated call is refused. Prints PASS/FAIL per case; exits non-zero on any failure.

Runs on a throwaway `zz/workq-validate` repo, purged first and last (and asserted gone), with two FIXED
test agents (the line never deletes agents, so a fresh pair per run would accumulate).
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "dev-eastus2"
URL = (sys.argv[2] if len(sys.argv) > 2 else
       "https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com").rstrip("/")
failed = 0


def check(name, ok, detail=""):
    global failed
    print(("PASS" if ok else "FAIL") + f": {name}" + (f"  [{detail}]" if detail and not ok else ""))
    failed += not ok


def token():
    if os.getenv("PARTYLINE_TOKEN"):
        return os.environ["PARTYLINE_TOKEN"]
    if URL.startswith("http://localhost") or URL.startswith("http://127."):
        return None
    out = subprocess.run(["databricks", "auth", "token", "-p", PROFILE], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["access_token"]


TOKEN = token()


def call(method, path, body=None, auth=True):
    req = urllib.request.Request(URL + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if auth and TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


REPO = "zz/workq-validate"
a, b = "zz_workq_validate_a", "zz_workq_validate_b"
drain = ["zz_workq_validate_d1", "zz_workq_validate_d2", "zz_workq_validate_d3"]
for ag in (a, b, *drain):
    call("POST", "/api/agents", {"agent_id": ag, "owner": "validate", "repo": "workq-validate"})
call("DELETE", f"/api/queue?repo={REPO}")  # clear any leftovers from an aborted run


def sync(issues, complete=False, **extra):
    return call("POST", "/api/queue/sync", {"agent_id": a, "repo": REPO, "viewer": "me", "issues": issues,
                                              "complete": complete, **extra})


def iss(n, labels=(), assignees=()):
    return {"number": n, "title": f"validate {n}", "labels": list(labels), "assignees": list(assignees),
            "state": "open", "created_at": "2026-11-01T00:00:00Z"}


def states():
    s, rows = call("GET", f"/api/queue?repo={REPO}")
    return {r["source_ref"].split("#")[1]: r["state"] for r in rows} if s == 200 else {}


def nxt(agent, **kw):
    return call("POST", "/api/queue/next", {"agent_id": agent, **kw})


s, r = sync([iss(1, ["P2", "sp-3"]), iss(2, ["P0", "sp-8"]), iss(3, ["P0", "sp-2"]), iss(4, ["bug"]),
             iss(5, ["P1"]), iss(6, ["P1", "sp-1"], ["bob"]), iss(7, ["P1", "sp-1"], ["me"])])
check("sync mirrors the issues", s == 200 and r["added"] == 7, f"{s} {r}")
check("triage gate and external claim are reflected in state",
      states() == {"1": "ready", "2": "ready", "3": "ready", "4": "untriaged", "5": "untriaged",
                   "6": "external", "7": "ready"}, str(states()))

s, g1 = nxt(a, queue=REPO)
check("next hands out the best item: highest band, then fewest points (#3)",
      s == 200 and g1["source_ref"].endswith("#3") and g1["claimed_by"] == a, f"{s} {g1}")
s, g2 = nxt(b, queue=REPO)
check("a second seat gets a different item (#2)", s == 200 and g2["source_ref"].endswith("#2"), f"{s} {g2}")
s, g3 = nxt(a, queue=REPO)
check("a seat resumes its own item instead of taking another", s == 200 and g3["source_ref"].endswith("#3")
      and g3["resumed"], f"{s} {g3}")

s, rel = call("POST", "/api/queue/release", {"agent_id": b, "issue_ref": f"{REPO}#3"})
check("NEG: a non-holder cannot release another seat's item", s == 200 and rel["released"] is False, f"{s} {rel}")
s, rel = call("POST", "/api/queue/release", {"agent_id": a, "issue_ref": f"{REPO}#3"})
check("release returns the item to the queue", s == 200 and rel["released"] and states()["3"] == "ready", f"{s} {rel}")

s, c = call("POST", "/api/claims/work-item", {"agent_id": a, "repo": REPO, "work_item": f"issue:{REPO}#1", "paths": []})
check("a direct claim-issue takes the item out of the queue", s == 200 and c["acquired"] and states()["1"] == "claimed",
      f"{s} {c} {states()}")

# a and b hold #1 and #2, so three fresh seats drain what is left: only 3 and 7 are ready
handed = []
for ag in drain:
    s, got = nxt(ag, queue=REPO)
    if s != 200:
        break
    handed.append(got["source_ref"].split("#")[1])
check("NEG: draining hands out only ready work (3, 7), never untriaged (4, 5) or external (6), then 404",
      sorted(handed) == ["3", "7"] and s == 404, f"{handed} last={s}")

s, au = call("GET", f"/api/queue/audit?repo={REPO}")
check("audit lists the untriaged intake and the external claim",
      s == 200 and {f"{REPO}#4", f"{REPO}#5"} <= {x["source_ref"] for x in au["untriaged"]}
      and [x["source_ref"] for x in au["external_claims"]] == [f"{REPO}#6"], f"{s} {au}")

s, _ = sync([iss(1, ["P2", "sp-3"])], complete=False)
check("NEG: a partial sync closes nothing", s == 200 and states()["2"] == "claimed", str(states()))
s, r = sync([iss(1, ["P2", "sp-3"])], complete=True, force_close=True)  # 6 of 7 vanish: only closes when confirmed
check("a confirmed complete sync closes what vanished upstream", s == 200 and states()["2"] == "closed", f"{s} {r} {states()}")
s, au = call("GET", f"/api/queue/audit?repo={REPO}")
check("audit lists work closed upstream while still held",
      s == 200 and f"{REPO}#2" in [x["source_ref"] for x in au["closed_upstream_while_claimed"]], f"{s} {au}")

stranger = "zz_workq_validate_c"
call("POST", "/api/agents", {"agent_id": stranger, "owner": "validate", "repo": "elsewhere"})
s, _ = nxt(stranger)
check("NEG: an agent whose own repo differs gets nothing by default (404)", s == 404, str(s))
s, _ = call("DELETE", "/api/queue?repo=covasant/Aura")
check("NEG: purge refuses a real repo (409)", s == 409, str(s))
s, _ = call("GET", "/api/queue?state=bogus")
check("NEG: an unknown state filter is refused (409)", s == 409, str(s))
if TOKEN:
    s, _ = call("GET", f"/api/queue?repo={REPO}", auth=False)
    check("NEG: an unauthenticated call is refused", s in (401, 403), str(s))

# --- guards added after review: exclusions, one operator per repo, implausibly large closures ---
call("DELETE", f"/api/queue?repo={REPO}")
s, r = sync([iss(1, ["P0", "sp-1", "epic"]), iss(2, ["P1", "sp-2"], ["me"])])
check("an epic-labelled issue is excluded, an operator-assigned issue is ordinary work",
      s == 200 and states() == {"1": "excluded", "2": "ready"}, f"{s} {r} {states()}")
s, got = nxt(drain[0], queue=REPO)
check("NEG: work next skips the excluded P0 and hands out the ready P1", s == 200 and got["source_ref"].endswith("#2"),
      f"{s} {got}")
s, r = call("POST", "/api/queue/sync", {"agent_id": a, "repo": REPO, "viewer": "someone-else", "issues": [iss(2, ["P1", "sp-2"], ["me"])]})
check("NEG: a sync under a different operator login is refused (409) and changes nothing",
      s == 409 and "operator me" in str(r) and states()["2"] == "claimed", f"{s} {r} {states()}")
s, r = call("POST", "/api/queue/sync", {"agent_id": a, "repo": REPO, "issues": [iss(2, ["P1", "sp-2"])]})
check("NEG: a sync with no operator login is refused (409)", s == 409, f"{s} {r}")
call("DELETE", f"/api/queue?repo={REPO}")
full = [iss(n, ["P1", "sp-1"]) for n in range(1, 13)]
sync(full)
s, r = sync(full[:3], complete=True)
check("NEG: a sync that sees 3 of 12 does not close the other 9", s == 200 and r["closed_upstream"] == 0
      and r["closure_skipped"] and "closed" not in states().values(), f"{s} {r}")
s, r = sync(full[:11], complete=True)
check("a small closure (1 of 12) is applied normally", s == 200 and r["closed_upstream"] == 1, f"{s} {r}")
s, r = call("POST", "/api/queue/sync", {"agent_id": a, "repo": REPO, "viewer": "me", "issues": full[:3],
                                        "complete": True, "force_close": True})
check("a large closure is applied when confirmed", s == 200 and r["closed_upstream"] == 8, f"{s} {r}")

s, pr = call("DELETE", f"/api/queue?repo={REPO}")
check("cleanup: purge removes the test repo's entries", s == 200 and pr["deleted_items"] >= 1, f"{s} {pr}")
check("cleanup: nothing left", call("GET", f"/api/queue?repo={REPO}")[1] == [], "")
s, cl = call("GET", "/api/claims")
check("cleanup: no claim left on the test repo",
      s == 200 and not [x for x in cl if x["resource_path"].startswith(f"issue:{REPO}#")], "")

print("\nALL PASS" if not failed else f"\n{failed} FAILED")
sys.exit(1 if failed else 0)
