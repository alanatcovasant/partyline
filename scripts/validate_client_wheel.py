"""Post-deploy check: the app serves a client wheel that ships claude-pl / codex-pl.

    python scripts/validate_client_wheel.py [profile] [app-url]

Positive: latest wheel is served, exposes the codex-pl console script, and contains the
setup launcher code. Negative: an unauthenticated download and a nonexistent wheel are refused.
Prints PASS/FAIL per case; exits non-zero on any failure.
"""
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "dev-eastus2"
URL = (sys.argv[2] if len(sys.argv) > 2 else
       "https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com").rstrip("/")

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def get(path: str, token: str | None):
    req = urllib.request.Request(f"{URL}/{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


tok = json.loads(subprocess.run(["databricks", "auth", "token", "-p", PROFILE],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                check=True).stdout)["access_token"]

status, body = get("partyline-client-latest.txt", tok)
wheel = body.decode().strip()
check("positive: latest wheel pointer served", status == 200 and wheel.endswith(".whl"), wheel)

status, body = get(wheel, tok)
check("positive: wheel downloads", status == 200 and len(body) > 10_000, f"{len(body)} bytes")

z = zipfile.ZipFile(io.BytesIO(body))
eps = z.read(next(n for n in z.namelist() if n.endswith("entry_points.txt"))).decode()
check("positive: codex-pl console script present", "codex-pl" in eps)
setup_src = z.read("covasant_partyline/setup.py").decode()
check("positive: setup writes claude-pl and codex-pl launchers",
      "claude-pl" in setup_src and "codex-pl" in setup_src)

# Databricks Apps answers an unauthenticated request with a redirect to its login page, which
# urllib follows to a 200 HTML page: the proof of refusal is that no wheel bytes come back.
status, body = get(wheel, None)
check("negative: unauthenticated request does not receive the wheel",
      not body.startswith(b"PK"), f"HTTP {status}, {len(body)} bytes")
status, _ = get("covasant_partyline-0.0.0-py3-none-any.whl", tok)
check("negative: nonexistent wheel is not served", status == 404, f"HTTP {status}")

sys.exit(1 if failed else 0)
