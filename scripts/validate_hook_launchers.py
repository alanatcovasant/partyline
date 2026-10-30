"""Post-deploy check: `kimi-pl` / `devin-pl` surface Partyline mail to the model.

    python scripts/validate_hook_launchers.py [kimi devin]

For each harness (default: kimi) it runs the launcher's own code path - register, install hooks -
then drives the real CLI headless against a throwaway project whose Partyline config points at a
local stub line (nothing touches the live line).

Positive: with mail addressed to the agent the model is handed it and states the codeword (in its
output or in a message it posts back to the stub), and
the launcher registered the agent (a POST /api/agents reached the line).
Negative: on a quiet line the codeword never appears.

Prints PASS/FAIL per case; exits non-zero on any failure or when a CLI is missing. Needs each CLI
on PATH and logged in.
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
from covasant_partyline.autoregister import register_self

CODEWORD = "CODEWORD-PINEAPPLE"
PROMPT = ("Reply with the single word: ready. If you later receive a Partyline message containing "
          "a CODEWORD, state the codeword verbatim.")
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def serve(mail: list):
    seen: list[tuple[str, str, str]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            body_in = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode(errors="replace")
            seen.append((self.command, self.path, body_in))
            body = json.dumps(mail if self.command == "GET" else {"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def command(harness: str) -> list[str]:
    if harness == "devin":
        # --print cannot show the workspace-trust prompt in the throwaway project, so skip it.
        return [shutil.which("devin"), "--print", "--respect-workspace-trust", "false", PROMPT]
    # kimi --print takes the prompt on stdin
    return [shutil.which("kimi"), "--print"]


def run(harness: str, mail: list) -> tuple[str, list]:
    srv, seen = serve(mail)
    agent = f"validate_{harness.replace('-', '')}_1"
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        (proj / ".partyline").mkdir()
        (proj / ".partyline" / "config.json").write_text(
            json.dumps({"url": f"http://127.0.0.1:{srv.server_port}", "agentId": agent}))
        # An ambient PARTYLINE_* would override the stub config and point the hooks at the live line.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PARTYLINE_")}
        cwd = os.getcwd()
        old = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith("PARTYLINE_")}
        os.chdir(proj)
        try:
            register_self(harness)  # what the launcher does before it starts the CLI
        finally:
            os.chdir(cwd)
            os.environ.update(old)
        for p in (Path.home() / ".partyline").glob(f"*-seen-{agent}.json"):
            p.unlink()
        r = subprocess.run(command(harness), cwd=proj, env=env, input=PROMPT if harness == "kimi" else None,
                           capture_output=True, text=True, timeout=300,
                           stdin=None if harness == "kimi" else subprocess.DEVNULL)
        for p in (Path.home() / ".partyline").glob(f"*-seen-{agent}.json"):
            p.unlink()
    srv.shutdown()
    return r.stdout, seen


def validate(harness: str) -> None:
    names = harness_runner.HARNESSES[harness][0]
    if not any(shutil.which(n) for n in names):
        check(f"{harness}: CLI on PATH", False, f"`{names[0]}` not found")
        return
    harness_runner.HARNESSES[harness][1]()  # install hooks, as launch() does
    mail = [{"id": 9001, "agent_id": "stub_sender", "message_type": "question",
             "summary": f"{CODEWORD}: reply with this codeword", "payload": f"{CODEWORD}: reply with this codeword"}]
    out, seen = run(harness, mail)
    # An agent that was handed the mail may answer on the line instead of in its own output.
    said = CODEWORD in out or any(m == "POST" and p == "/api/messages" and CODEWORD in b for m, p, b in seen)
    check(f"{harness} positive: model is handed the mail and states the codeword", said)
    check(f"{harness} positive: launcher registered the agent",
          any((m, p) == ("POST", "/api/agents") for m, p, _ in seen), f"{len(seen)} requests")
    out, seen = run(harness, [])
    check(f"{harness} negative: quiet line, codeword never appears",
          CODEWORD not in out and not any(CODEWORD in b for _, _, b in seen) and "ready" in out.lower())


for h in sys.argv[1:] or ["kimi"]:
    validate(h)
sys.exit(1 if failed else 0)
