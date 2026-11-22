"""Post-deploy validation for gh-9 (reading presence on the fleet tab).

Positive: the served JS bundle renders the `chip-reading` badge, and every
agent row in /api/agents carries the `reading` + `read_age_seconds` fields it
consumes. Negative: an agent payload missing the fields is caught by the same
shape check (validated offline against a stub row).

    python scripts/validate_reading_presence.py [--profile dev-eastus2]
"""
import json
import subprocess
import sys
import urllib.request

APP = "https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com"
failed = 0


def check(name, ok, detail=""):
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def token(profile):
    r = subprocess.run(["databricks", "auth", "token", "-p", profile],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["access_token"]


def get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    return urllib.request.urlopen(req, timeout=30)


def row_has_fields(row):
    return "reading" in row and "read_age_seconds" in row


# Negative (offline): a row without the new fields is rejected.
check("offline negative: field check rejects a pre-gh-9 row",
      not row_has_fields({"agent_id": "x", "last_heartbeat": "t"}))
check("offline positive: field check accepts a gh-9 row",
      row_has_fields({"agent_id": "x", "reading": False, "read_age_seconds": 10}))

profile = "dev-eastus2"
if "--profile" in sys.argv:
    profile = sys.argv[sys.argv.index("--profile") + 1]
tok = token(profile)

# Positive (live): bundle renders the badge.
index = get(f"{APP}/", tok).read().decode()
bundle = index.split('src="', 1)[1].split('"', 1)[0] if 'src="' in index else ""
check("index.html references a JS bundle", bool(bundle), bundle)
js = get(f"{APP}{bundle}", tok).read().decode() if bundle else ""
check("deployed bundle contains chip-reading", "chip-reading" in js)

# Positive (live): the API the badge binds to returns the fields for all rows.
agents = json.loads(get(f"{APP}/api/agents", tok).read())
check("/api/agents returns rows", len(agents) > 0, f"{len(agents)} agents")
check("every agent row carries reading + read_age_seconds",
      all(row_has_fields(a) for a in agents),
      f"{sum(row_has_fields(a) for a in agents)}/{len(agents)}")
check("at least one agent is currently reading",
      any(a.get("reading") for a in agents),
      f"{sum(1 for a in agents if a.get('reading'))} reading")

sys.exit(1 if failed else 0)
