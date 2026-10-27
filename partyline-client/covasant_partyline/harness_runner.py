"""`grok-pl` / `agy-pl`: launch Grok Build or the Antigravity CLI with Partyline hooks installed.

Neither CLI has an inbound push into a live session (Codex's `queue --thread`, Claude Code's
`claude/channel`), so awareness comes from hooks, and a hook only runs when the agent does:

- **Grok Build**: a `Stop` hook (`check_unread` in `grok_stop` format) blocks the end of a turn
  when mail is addressed to this agent, so the agent keeps working on it. Grok drops the stdout
  of every other hook, so this is the only event that can reach the model. The TUI takes no
  per-process hooks, so they live in `~/.grok/hooks/partyline.json`, which is silent in any
  directory without a `.partyline/config.json`.
- **Antigravity**: a `PreInvocation` hook (`agy_hook`) injects the first-turn digest and later
  unread peeks, in `~/.gemini/config/hooks.json`.

Neither wakes an idle session. For that, watch the line in another terminal.

The launcher installs the hooks idempotently, then runs the CLI untouched, so it is a drop-in:
`grok-pl [grok args...]`, `agy-pl [agy args...]`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
GROK_HOOKS = HOME / ".grok" / "hooks" / "partyline.json"
AGY_HOOKS = HOME / ".gemini" / "config" / "hooks.json"
HOOK_TIMEOUT_S = 30


def _py() -> str:
    # Pin the interpreter that has this package: a pipx venv is not the system python.
    # Grok runs hook commands through PowerShell on Windows, where a quoted path is a parse
    # error unless it is called with `&`; under sh that `&` would be one.
    return f'& "{sys.executable}"' if os.name == "nt" else f'"{sys.executable}"'


def grok_hooks() -> dict:
    return {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{_py()} -m covasant_partyline.check_unread",
                            "timeout": HOOK_TIMEOUT_S,
                            "env": {"PARTYLINE_HOOK_FORMAT": "grok_stop"},
                        }
                    ]
                }
            ]
        }
    }


def _portable_py() -> str:
    # Antigravity does not say which shell runs a hook, so use the one spelling that PowerShell,
    # cmd and sh all execute: forward slashes, unquoted. A path with a space has to be quoted,
    # which only some of those shells accept.
    exe = Path(sys.executable).as_posix()
    return f'"{exe}"' if " " in exe else exe


def agy_hook() -> dict:
    return {
        "PreInvocation": [
            {"type": "command", "command": f"{_portable_py()} -m covasant_partyline.agy_hook", "timeout": HOOK_TIMEOUT_S}
        ]
    }


def _write_json(path: Path, data: dict) -> bool:
    """Write `data` unless the file already holds it. True when the file changed."""
    text = json.dumps(data, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def install_grok_hooks(path: Path = GROK_HOOKS) -> bool:
    return _write_json(path, grok_hooks())


def install_agy_hooks(path: Path = AGY_HOOKS) -> bool:
    """Merge our `partyline` hook into the global config, leaving the user's other hooks alone."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["partyline"] = agy_hook()
    return _write_json(path, existing)


HARNESSES = {
    "grok": ("grok", install_grok_hooks),
    "agy": ("agy", install_agy_hooks),
}


def launch(harness: str, argv: list[str] | None = None) -> None:
    exe_name, install = HARNESSES[harness]
    args = list(sys.argv[1:] if argv is None else argv)
    exe = shutil.which(exe_name)
    if not exe:
        sys.exit(f"{exe_name}-pl: `{exe_name}` not found on PATH")
    install()
    while True:
        try:
            sys.exit(subprocess.run([exe, *args]).returncode)
        except KeyboardInterrupt:
            continue  # Ctrl+C belongs to the TUI, not the launcher


def launch_grok(argv: list[str] | None = None) -> None:
    launch("grok", argv)


def launch_agy(argv: list[str] | None = None) -> None:
    launch("agy", argv)


if __name__ == "__main__":
    launch(sys.argv[1], sys.argv[2:])
