"""Check omni-pl command construction.

Positive: `omni-pl claude --fork` forks through the sessions API and resumes
the new session, and points that launch at claude-pl. `omni-pl grok` starts
the ACP harness and the session watcher. `omni-pl grok --fork` stays on
`omnigent run --fork`.
Negative: a native fork is not sent as `run --fork`, grok is not pointed at
grok-pl, agy is not pinned with `OMNIGENT_AGY_PATH`, --fork with --resume is
rejected, and an unknown harness is rejected.

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
    "positive: grok starts omnigent run --harness grok and watches the session",
    grok.command == ("/bin/omnigent", "run", "--harness", "grok") and grok.watch is True,
)
check(
    "negative: grok is not redirected to grok-pl",
    "OMNIGENT_GROK_PATH" not in dict(grok.env_updates) and "/bin/grok-pl" not in grok.command,
)
grok_fork = prepare(["grok", "--fork", "conv_g"], which)
check(
    "positive: grok fork stays on omnigent run --fork",
    grok_fork.api_fork is False
    and grok_fork.command == ("/bin/omnigent", "run", "--fork", "conv_g", "--harness", "grok"),
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
