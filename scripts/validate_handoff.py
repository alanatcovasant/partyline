"""Post-deploy check: progress-task ownership handoff (offer/accept/decline/takeover) works and is guarded.

    python scripts/validate_handoff.py [profile] [app-url]

Positive: an owner offers a task, the named agent accepts it into its own folder and becomes the owner
(with history, next_action kept and the previous owner recorded); an offer can be withdrawn; listings
carry next_action and owner_status.
Negative: a non-owner cannot offer; an offer to an unregistered agent is refused; the wrong agent,
the owner and a missing folder cannot accept; a live owner cannot be taken over; a real folder cannot be
purged; an unauthenticated call is refused.
Not covered live: a takeover of a STALE owner (it needs a heartbeat older than the idle threshold, which a
validator cannot fake against the deployed database); tests/test_handoff.py covers it.

Cleans up after itself: tasks live in `zz-` folders that are purged at the end (and asserted gone), and the
three test agents are fixed ids (the line never deletes agents, so a fresh set per run would accumulate).
Prints PASS/FAIL per case; exits non-zero on any failure.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import uuid4

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
    if URL.startswith(("http://localhost", "http://127.")):
        return None
    out = subprocess.run(["databricks", "auth", "token", "-p", PROFILE], capture_output=True, text=True,
                         check=True, stdin=subprocess.DEVNULL).stdout
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


sfx = uuid4().hex[:8]
A, B, C = "zz_ho_validate_a", "zz_ho_validate_b", "zz_ho_validate_c"
FA, FB, FC = (f"zz-ho-validate-{sfx}-{x}" for x in "abc")
for ag in (A, B, C):
    call("POST", "/api/agents", {"agent_id": ag, "owner": "validate", "repo": "partyline"})


def ho(task, agent, action, **kw):
    return call("POST", f"/api/progress/{task}/handoff", {"agent_id": agent, "action": action, **kw})


s, r = call("POST", "/api/progress", {"folder": FA, "agent_id": A, "status": "started", "summary": "validate handoff",
                                      "next_action": "run the check"})
task = r["task_id"] if s == 200 else None
check("start a task with a next_action", s == 200 and task, f"{s} {r}")

s, rows = call("GET", f"/api/progress?folder={FA}")
mine = rows[0] if s == 200 and rows else {}
check("listing carries next_action and owner_status",
      mine.get("next_action") == "run the check" and mine.get("owner_status") in ("live", "idle", "stale")
      and mine.get("orphaned") is False, str(mine))

check("NEG: a non-owner cannot offer", ho(task, B, "offer", to=C)[0] == 409)
check("NEG: an offer to an unregistered agent is refused", ho(task, A, "offer", to="zz_nobody_registered")[0] == 409)
check("NEG: accept with no pending offer is refused", ho(task, B, "accept", folder=FB)[0] == 409)

s, r = ho(task, A, "offer", to=B, note="validation", next_action="continue the check")
check("POS: the owner offers to B", s == 200 and r["handoff_to"] == B and r["agent_id"] == A, f"{s} {r}")
check("NEG: the wrong agent cannot accept a targeted offer", ho(task, C, "accept", folder=FC)[0] == 409)
check("NEG: the owner cannot accept its own offer", ho(task, A, "accept", folder=FA)[0] == 409)
check("NEG: accept without a folder is refused", ho(task, B, "accept")[0] == 409)

s, r = ho(task, B, "accept", folder=FB)
check("POS: B accepts into its own folder and becomes the owner",
      s == 200 and r["agent_id"] == B and r["folder"] == FB and r["handoff_from"] == A and r["handoff_to"] is None
      and r["next_action"] == "continue the check", f"{s} {r}")

s, hist = call("GET", f"/api/progress/{task}/history")
check("POS: history records the offer and the acceptance",
      s == 200 and [h["status"] for h in hist][:2] == ["handoff_accepted", "handoff_offered"], f"{s} {hist}")

s, r = ho(task, A, "takeover", folder=FA, note="validator probe")
check("NEG: a live owner cannot be taken over (409, names the owner)", s == 409 and B in str(r), f"{s} {r}")

check("POS: an offer to anyone can be made and withdrawn",
      ho(task, B, "offer", to="*")[0] == 200 and ho(task, B, "decline")[0] == 200)
check("NEG: nothing left to decline after the withdrawal", ho(task, B, "decline")[0] == 409)

s, _ = call("DELETE", "/api/progress?folder=" + FB.replace("zz-", "real-", 1))
check("NEG: purge refuses a non-test folder (409)", s == 409, str(s))
if TOKEN:
    s, _ = call("GET", f"/api/progress?folder={FB}", auth=False)
    check("NEG: an unauthenticated call is refused", s in (401, 403), str(s))

for f in (FA, FB, FC):
    call("DELETE", f"/api/progress?folder={f}")
left = [call("GET", f"/api/progress?folder={f}&include_done=true")[1] for f in (FA, FB, FC)]
check("cleanup: no tasks left in the test folders", all(x == [] for x in left), str(left))

print("\nALL PASS" if not failed else f"\n{failed} FAILED")
sys.exit(1 if failed else 0)
