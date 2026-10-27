"""Post-deploy check: Grok Build's Stop hook surfaces Partyline mail to the model.

    python scripts/validate_grok_pl.py

Runs the real `grok` headless against a throwaway project whose Partyline config points at a
local stub line (nothing touches the live line), with `grok-pl`'s hooks installed.

Positive: with mail addressed to the agent, the Stop gate fires, the model is handed the
message and states its codeword, and the line was peeked exactly once (no re-block loop).
Negative: on a quiet line the gate stays silent, so the turn ends untouched and the codeword
never appears.

Prints PASS/FAIL per case; exits non-zero on any failure. Needs `grok` on PATH and a login.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from covasant_partyline import harness_runner

CODEWORD = "CODEWORD-PINEAPPLE"
PROMPT = ("Reply with the single word: ready. If you later receive a Partyline message containing "
          "a CODEWORD, state the codeword verbatim.")
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def serve(mail: list):
    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps(mail).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def run_grok(mail: list) -> tuple[str, int]:
    srv, seen = serve(mail)
    agent = "validate_grok_1"
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / ".partyline").mkdir()
        (proj / ".partyline" / "config.json").write_text(
            json.dumps({"url": f"http://127.0.0.1:{srv.server_port}", "agentId": agent}))
        # An ambient PARTYLINE_* would override the stub config and point the hook at the live line.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PARTYLINE_")}
        seen_file = Path.home() / ".partyline" / f"grok-seen-{agent}.json"
        seen_file.unlink(missing_ok=True)
        r = subprocess.run([shutil.which("grok"), "--always-approve", "-p", PROMPT], cwd=proj, env=env,
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        seen_file.unlink(missing_ok=True)
    srv.shutdown()
    return r.stdout, sum("/unread" in p for p in seen)


if not shutil.which("grok"):
    sys.exit("FAIL  `grok` not found on PATH")
harness_runner.install_grok_hooks()

mail = [{"id": 9001, "agent_id": "stub_sender", "message_type": "question",
         "summary": f"{CODEWORD}: reply with this codeword"}]
out, peeks = run_grok(mail)
check("positive: model is handed the mail and states the codeword", CODEWORD in out)
check("positive: line peeked exactly once (no re-block loop)", peeks == 1, f"{peeks} unread requests")

out, peeks = run_grok([])
check("negative: quiet line does not block the stop", CODEWORD not in out and "ready" in out.lower())
check("negative: quiet line was still peeked", peeks == 1, f"{peeks} unread requests")

sys.exit(1 if failed else 0)
