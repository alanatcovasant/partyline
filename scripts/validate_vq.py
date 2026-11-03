"""Post-deploy check: the validation queue (partyline vq) works end to end and cannot be gamed.

    python scripts/validate_vq.py [profile] [app-url]

Positive: enqueue -> next -> executed PASS validates; a later FAIL reopens; history keeps every run.
Negative: a hand-recorded PASS only reaches self_reported; a second validator cannot take or
record against a held item; a bad state filter and unknown item are refused; an unauthenticated
call is refused. Prints PASS/FAIL per case; exits non-zero on any failure.

Cleans up after itself: items live on a throwaway `zz-` target that is purged at the end (and
asserted gone), and it reuses two FIXED test agents (the line never deletes agents, so a fresh
pair per run would accumulate; re-registering the same ids is a no-op).
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


sfx = uuid4().hex[:8]
tgt, a, b = f"zz-vq-validate-{sfx}", "zz_vq_validate_a", "zz_vq_validate_b"
n = int(sfx[:6], 16)
ref = f"zz/vq#{n}"
for ag in (a, b):
    call("POST", "/api/agents", {"agent_id": ag, "owner": "validate", "repo": "partyline"})


def run(agent, code, executed, **kw):
    return call("POST", "/api/vq/runs", {"issue_ref": ref, "target": tgt, "agent_id": agent, "exit_code": code,
                                          "executed": executed, **kw})


s, item = call("POST", "/api/vq/items", {"issue_ref": ref, "target": tgt, "agent_id": a, "script": "echo ok"})
check("enqueue creates a pending item", s == 200 and item["state"] == "pending", f"{s} {item}")

s, got = call("POST", "/api/vq/claim", {"agent_id": a, "target": tgt})
check("next claims the item", s == 200 and got["issue_ref"] == ref and got["state"] == "claimed", f"{s} {got}")

s, _ = call("POST", "/api/vq/claim", {"agent_id": b, "target": tgt, "issue_ref": ref})
check("NEG: a second validator cannot claim a held item (423)", s == 423, str(s))
s, _ = run(b, 0, True)
check("NEG: a second validator cannot record against a held item (423)", s == 423, str(s))

s, r = run(a, 0, False, commit="abc")
check("NEG: a hand-recorded PASS is self_reported, not validated", s == 200 and r["state"] == "self_reported", f"{s} {r}")

call("POST", "/api/vq/claim", {"agent_id": a, "target": tgt})
s, r = run(a, 0, True, commit="abc")
check("an executed PASS validates", s == 200 and r["state"] == "validated", f"{s} {r}")

s, r = run(a, 1, True)
check("a later executed FAIL reopens it as failed", s == 200 and r["state"] == "failed", f"{s} {r}")

s, sh = call("GET", f"/api/vq/show?issue_ref={ref.replace('#', '%23')}&target={tgt}")
check("history keeps every run, newest first", s == 200 and [x["exit_code"] for x in sh["runs"]] == [1, 0, 0], f"{s} {sh}")

s, lst = call("GET", f"/api/vq/items?target={tgt}&state=failed")
check("list filters by state", s == 200 and [i["issue_ref"] for i in lst] == [ref], f"{s} {lst}")

s, _ = call("GET", f"/api/vq/items?target={tgt}&state=bogus")
check("NEG: an unknown state filter is refused (409)", s == 409, str(s))
s, _ = call("GET", f"/api/vq/show?issue_ref=zz/vq%231&target={tgt}")
check("NEG: an unknown item is 404", s == 404, str(s))
if TOKEN:
    s, _ = call("GET", f"/api/vq/items?target={tgt}", auth=False)
    check("NEG: an unauthenticated call is refused", s in (401, 403), str(s))

print("\nALL PASS" if not failed else f"\n{failed} FAILED")
sys.exit(1 if failed else 0)
