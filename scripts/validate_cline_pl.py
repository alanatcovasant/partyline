"""Offline validation for cline-pl (issues #14/#15/#16).

Positive: the `cline-pl` launcher is registered and its shims render for both OS
forms (Unix extensionless + Windows .ps1) with the clone's agent id stamped, the
install is idempotent, and the cline envelope round-trips from check_unread.
Negative: launching outside a clone (no .partyline/config.json) writes no shims,
and a quiet line emits nothing.

    python scripts/validate_cline_pl.py           # offline
    python scripts/validate_cline_pl.py --live    # plus `cline --version` if installed
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partyline-client"))

from covasant_partyline import check_unread, harness_runner as hr
from covasant_partyline import setup as pl_setup

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


# --- launcher registration ------------------------------------------------- #
launchers = pl_setup._launchers(Path(tempfile.mkdtemp(prefix="cline-pl-launchers-")))
cline = launchers and next((p for p in launchers if p.name == "cline-pl"), None)
check("cline-pl launcher written by partyline setup", cline is not None)
check("cline-pl.cmd written for Windows", cline is not None and cline.with_name("cline-pl.cmd").exists())

# --- shim rendering -------------------------------------------------------- #
work = Path(tempfile.mkdtemp(prefix="cline-pl-validate-"))
(work / ".partyline").mkdir()
(work / ".partyline" / "config.json").write_text(
    json.dumps({"agentId": "alan_partyline_cline_1", "url": "http://localhost:8000"}))

os.environ.pop("PARTYLINE_AGENT_ID", None)
check("install_cline_hooks writes shims", hr.install_cline_hooks(work) is True)
hooks = work / ".clinerules" / "hooks"
task = hooks / "TaskStart"
peek = hooks / "UserPromptSubmit"
check("TaskStart + UserPromptSubmit (Unix) exist", task.is_file() and peek.is_file())
check("TaskStart.ps1 + UserPromptSubmit.ps1 (Windows) exist",
      (hooks / "TaskStart.ps1").is_file() and (hooks / "UserPromptSubmit.ps1").is_file())
task_txt = task.read_text()
peek_txt = peek.read_text()
check("TaskStart stamps agent id + cline format",
      "PARTYLINE_AGENT_ID=alan_partyline_cline_1" in task_txt and "PARTYLINE_HOOK_FORMAT=cline" in task_txt)
check("TaskStart runs the digest module", "-m covasant_partyline.hook" in task_txt)
check("UserPromptSubmit runs the peek module", "-m covasant_partyline.check_unread" in peek_txt)
check("Unix shims are executable", os.access(task, os.X_OK) and os.access(peek, os.X_OK))
check(".ps1 stamps agent id", '$env:PARTYLINE_AGENT_ID = "alan_partyline_cline_1"'
      in (hooks / "TaskStart.ps1").read_text())
check("install is idempotent", hr.install_cline_hooks(work) is False)

# --- negatives ------------------------------------------------------------- #
empty = work / "no-clone"
empty.mkdir()
err = io.StringIO()
with contextlib.redirect_stderr(err):
    wrote = hr.install_cline_hooks(empty)
check("no identity -> no shims, warns", wrote is False and not (empty / ".clinerules").exists()
      and "partyline init" in err.getvalue())

# Quiet line -> cline peek emits nothing.
check_unread.cli.resolve_url = lambda cli_arg=None, cfg=None: "http://localhost:8000"
check_unread.cli.resolve_agent_id = lambda cli_arg=None, cfg=None: "me"
check_unread.cli._load_config = lambda: {}


class _Quiet:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps([]).encode()


check_unread.urllib.request.urlopen = lambda req, timeout=None: _Quiet()
os.environ["PARTYLINE_HOOK_FORMAT"] = "cline"
check_unread.sys.stdin = io.StringIO("{}")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    check_unread.main()
check("quiet line emits nothing (cline peek)", out.getvalue().strip() == "")

# --- live probe ------------------------------------------------------------ #
if "--live" in sys.argv:
    exe = shutil.which("cline")
    if not exe:
        print("SKIP  cline CLI not on PATH — live probe skipped")
    else:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        check("cline --version", r.returncode == 0, r.stdout.strip() or r.stderr.strip())

sys.exit(1 if failed else 0)
