"""Check omni-pl command construction.

Positive: `omni-pl claude --fork` forks through the sessions API and resumes
the new session, and points that launch at claude-pl. `omni-pl grok` starts
the native grok harness on grok-pl and forks through the API. `omni-pl cmdc`
points Command Code at cmdc-pl. `omni-pl cline` starts native Cline with the
session watcher.
Negative: a native fork is not sent as `run --fork`, grok no longer uses the
ACP `run --harness grok` path, cline pins no binary, agy is not pinned with
`OMNIGENT_AGY_PATH`, --fork with --resume is rejected, and an unknown harness
is rejected.

    python scripts/validate_omni_pl.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partyline-client"))

from covasant_partyline.omni_runner import prepare  # noqa: E402

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def which(name: str) -> str | None:
    return {
        "omnigent": "/bin/omnigent",
        "claude-pl": "/bin/claude-pl",
        "agy-pl": "/bin/agy-pl",
        "grok-pl": "/bin/grok-pl",
        "cmdc-pl": "/bin/cmdc-pl",
    }.get(name)


fork = prepare(["claude", "--fork", "conv_abc"], which)
check(
    "positive: claude fork resumes a session created by the fork API",
    fork.api_fork
    and fork.fork_agent == "claude-native-ui"
    and fork.command == ("/bin/omnigent", "claude", "--resume"),
)
check(
    "negative: native fork is not sent as omnigent run --fork",
    "--fork" not in fork.command and "claude-native" not in fork.command,
)
check(
    "positive: claude launch pins OMNIGENT_CLAUDE_PATH to claude-pl",
    dict(fork.env_updates).get("OMNIGENT_CLAUDE_PATH") == "/bin/claude-pl",
)
grok = prepare(["grok"], which)
check(
    "positive: grok starts omnigent grok on grok-pl",
    grok.command == ("/bin/omnigent", "grok")
    and dict(grok.env_updates).get("OMNIGENT_GROK_PATH") == "/bin/grok-pl"
    and grok.hooks == "grok",
)
check(
    "negative: grok no longer uses the ACP run --harness grok path",
    "run" not in grok.command and "--harness" not in grok.command,
)
grok_fork = prepare(["grok", "--fork", "conv_g"], which)
check(
    "positive: grok fork goes through the API onto grok-native-ui",
    grok_fork.api_fork and grok_fork.fork_agent == "grok-native-ui"
    and grok_fork.command == ("/bin/omnigent", "grok", "--resume"),
)
cmdc = prepare(["cmdc"], which)
check(
    "positive: cmdc starts omnigent cmdc on cmdc-pl",
    cmdc.command == ("/bin/omnigent", "cmdc")
    and dict(cmdc.env_updates).get("OMNIGENT_COMMANDCODE_PATH") == "/bin/cmdc-pl"
    and cmdc.hooks == "cmdc",
)
cline = prepare(["cline"], which)
check(
    "positive: cline starts omnigent cline and watches the session",
    cline.command == ("/bin/omnigent", "cline") and cline.watch is True,
)
check(
    "negative: cline pins no binary and warns about nothing",
    cline.env_updates == () and cline.warnings == (),
)
agy = prepare(["agy"], which)
check(
    "positive: agy pins OMNIGENT_ANTIGRAVITY_PATH to agy-pl",
    dict(agy.env_updates).get("OMNIGENT_ANTIGRAVITY_PATH") == "/bin/agy-pl",
)
check(
    "negative: agy is not pinned with OMNIGENT_AGY_PATH",
    "OMNIGENT_AGY_PATH" not in dict(agy.env_updates),
)
try:
    prepare(["claude", "--fork", "a", "--resume", "b"], which)
except SystemExit as exc:
    check("negative: --fork and --resume together are rejected", "only one" in str(exc))
else:
    check("negative: --fork and --resume together are rejected", False, "no SystemExit")
try:
    prepare(["not-a-harness"], which)
except SystemExit as exc:
    check("negative: unknown harness is rejected", "unknown harness" in str(exc))
else:
    check("negative: unknown harness is rejected", False, "no SystemExit")

sys.exit(1 if failed else 0)
