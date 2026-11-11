"""Check that *-pl shell launchers are Linux-safe.

Positive: a Windows interpreter path is written as LF, with a /mnt branch for WSL
and the original path for Git Bash. A POSIX interpreter is pinned with no WSL branch.
Negative: the writer does not emit a CR in the shebang (that is the
"cannot execute: required file not found" failure), and a POSIX path is not rewritten
to /mnt.

    python scripts/validate_wsl_launchers.py

Prints PASS/FAIL per case and exits non-zero on any failure. Does not install into WSL;
`partyline install-wsl-launchers` does that.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partyline-client"))

from covasant_partyline.setup import _shell_launcher, _windows_path_to_wsl  # noqa: E402

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


win = _shell_launcher(r"C:\Python314\python.exe", "-m covasant_partyline.harness_runner agy")
posix = _shell_launcher("/usr/bin/python3", "-m covasant_partyline.harness_runner grok")
claude = _shell_launcher(None, "claude --dangerously-load-development-channels server:partyline-channel")

check("positive: windows launcher keeps LF shebang", "\r" not in win and win.startswith("#!/bin/sh\n"))
check(
    "positive: windows launcher runs python.exe via /mnt inside WSL",
    "/mnt/c/Python314/python.exe" in win and "WSL_DISTRO_NAME" in win,
)
check(
    "positive: windows launcher still execs the Git Bash path",
    'exec "C:\\Python314\\python.exe" -m covasant_partyline.harness_runner agy "$@"' in win,
)
check(
    "positive: posix launcher pins that interpreter",
    'exec "/usr/bin/python3" -m covasant_partyline.harness_runner grok "$@"' in posix,
)
check("positive: claude-pl stays a shell exec with LF", claude.startswith("#!/bin/sh\n") and "\r" not in claude)
check(
    "negative: posix launcher is not rewritten to a /mnt path",
    "WSL_DISTRO_NAME" not in posix and "/mnt/" not in posix,
)
check(
    "negative: a CR shebang is not produced",
    b"\r" not in win.encode() and b"\r" not in posix.encode() and b"\r" not in claude.encode(),
)
check(
    "negative: a non-windows path does not map to /mnt",
    _windows_path_to_wsl("/usr/bin/python3") is None and _windows_path_to_wsl("python3") is None,
)

sys.exit(1 if failed else 0)
