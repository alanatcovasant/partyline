"""Check that omni-pl passes harness names to Omnigent instead of gating them.

Offline (default) checks command construction with a stubbed Omnigent.
`--live` also asks the omnigent on PATH, read-only (`omnigent <name> --help`
only, nothing is launched, registered or posted).

Positive: cmdc, cline and grok (and their -native spellings) build an omnigent
command without a probe; a harness Partyline has no launcher for (goose) is
confirmed by Omnigent and launched with the session watcher; a native fork goes
through the sessions API and resumes; the alias layer still pins *-pl binaries.
Negative: a typo exits with Omnigent's own "No such command" text (no local
"Known:" list), a native fork is never sent as `run --fork`, --fork with
--resume is rejected, and a stale server's fork error reaches the user verbatim.

    python scripts/validate_omni_pl.py           # offline
    python scripts/validate_omni_pl.py --live    # plus the installed omnigent
"""
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partyline-client"))

from covasant_partyline import omni_runner as omni

failed = 0
cache_dir = Path(tempfile.mkdtemp(prefix="omni-pl-validate-"))
omni._cache_path = lambda: cache_dir / "omni-harnesses.json"


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


def no_probe(o: str, name: str) -> tuple[int, str]:
    raise AssertionError(f"probed {name}")


def exit_of(fn) -> tuple[object, str]:
    """Run fn; return (SystemExit code or None, captured stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            fn()
        except SystemExit as exc:
            return exc.code, err.getvalue()
    return None, err.getvalue()


# --- passthrough ----------------------------------------------------------
for name in ("cmdc", "cline", "grok"):
    plan = omni.prepare([name, "--yolo"], which, no_probe)
    check(f"positive: omni-pl {name} --yolo reaches omnigent", plan.command == ("/bin/omnigent", name, "--yolo"))
for name in ("cline-native", "commandcode-native", "grok-native", "native-grok"):
    plan = omni.prepare([name, "--yolo"], which, no_probe)
    check(
        f"positive: omni-pl {name} --yolo reaches omnigent run --harness",
        plan.command == ("/bin/omnigent", "run", "--harness", name, "--yolo"),
    )
goose = omni.prepare(["goose"], which, lambda o, n: (0, ""))
check(
    "positive: a harness Partyline has no launcher for is confirmed by Omnigent and watched",
    goose.command == ("/bin/omnigent", "goose") and goose.watch and goose.env_updates == (),
)
code, err = exit_of(lambda: omni.prepare(["goose"], which, no_probe))
check("positive: the Omnigent answer is cached (no second probe)", code is None, err.strip())

typo_msg = "Error: No such command 'clien'. (Did you mean one of: 'cline', 'login'?)\n"
code, err = exit_of(lambda: omni.prepare(["clien", "--yolo"], which, lambda o, n: (2, typo_msg)))
check("negative: a typo exits non-zero with Omnigent's message verbatim", code == 2 and err == typo_msg, repr(err))
check("negative: no partyline-local harness list in the error", "Known:" not in err and "unknown harness" not in err)

# --- alias layer still attaches Partyline ---------------------------------
grok = omni.prepare(["grok"], which, no_probe)
check(
    "positive: grok pins OMNIGENT_GROK_PATH to grok-pl and installs hooks",
    dict(grok.env_updates).get("OMNIGENT_GROK_PATH") == "/bin/grok-pl" and grok.hooks == "grok",
)
cmdc = omni.prepare(["commandcode-native"], which, no_probe)
check(
    "positive: commandcode-native keeps the cmdc-pl attachment",
    dict(cmdc.env_updates).get("OMNIGENT_COMMANDCODE_PATH") == "/bin/cmdc-pl" and cmdc.hooks == "cmdc",
)
agy = omni.prepare(["agy"], which, no_probe)
check(
    "negative: agy is pinned with OMNIGENT_ANTIGRAVITY_PATH, not OMNIGENT_AGY_PATH",
    dict(agy.env_updates).get("OMNIGENT_ANTIGRAVITY_PATH") == "/bin/agy-pl"
    and "OMNIGENT_AGY_PATH" not in dict(agy.env_updates),
)

# --- forks ----------------------------------------------------------------
fork = omni.prepare(["claude", "--fork", "conv_abc"], which, no_probe)
check(
    "positive: claude fork resumes a session created by the fork API",
    fork.api_fork and fork.fork_agent == "claude-native-ui" and fork.command == ("/bin/omnigent", "claude", "--resume"),
)
nfork = omni.prepare(["cline-native", "--fork", "conv_x"], which, no_probe)
check(
    "positive: a -native fork resumes through run --harness",
    nfork.fork_agent == "cline-native-ui"
    and nfork.command == ("/bin/omnigent", "run", "--harness", "cline-native", "--resume"),
)
check("negative: a native fork is never sent as run --fork", "--fork" not in fork.command + nfork.command)
code, _ = exit_of(lambda: omni.prepare(["claude", "--fork", "a", "--resume", "b"], which, no_probe))
check("negative: --fork and --resume together are rejected", code is not None and "only one" in str(code))

stale = "invalid agent spec synthesized from omnigent YAML: must be one of [...], got 'cline-native'"
real_request = omni._request_json
omni._request_json = lambda method, url, body=None: (
    (200, {"data": [{"name": "cline-native-ui", "id": "ag"}]}) if method == "GET" else (400, {"detail": stale})
)
code, _ = exit_of(lambda: omni.fork_session("http://127.0.0.1:6767", "conv_src", "cline-native-ui"))
omni._request_json = real_request
check("negative: a stale server's fork error reaches the user verbatim", stale in str(code), str(code))

# --- live: ask the installed omnigent (read-only) -------------------------
if "--live" in sys.argv:
    real = shutil.which("omnigent") or shutil.which("omni")
    if not real:
        check("live: omnigent on PATH", False, "not found")
    else:
        for name in ("cmdc", "cline", "grok", "goose"):
            code, out = omni._run_probe(real, name)
            check(f"live positive: installed omnigent accepts `{name}`", code == 0, out.strip()[:120])
        code, err = exit_of(lambda: omni.confirm_with_omnigent(real, "clien"))
        check(
            "live negative: installed omnigent rejects `clien` with its own message",
            code not in (None, 0) and "No such command" in err,
            err.strip()[:160],
        )

shutil.rmtree(cache_dir, ignore_errors=True)
sys.exit(1 if failed else 0)
