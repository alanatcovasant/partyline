"""`grok-pl`, `agy-pl`, `kimi-pl`, `cmdc-pl`, `cursor-agent-pl`, `devin-pl`, `cline-pl`: Partyline launchers.

None of these CLIs has an inbound push into a live session (Codex's `queue --thread`, Claude Code's
`claude/channel`), so awareness comes from hooks, and a hook only runs when the agent does:

- **Grok Build**: a `Stop` hook (`check_unread` in `grok_stop` format) blocks the end of a turn
  when mail is addressed to this agent, so the agent keeps working on it. Grok drops the stdout
  of every other hook, so this is the only event that can reach the model. The TUI takes no
  per-process hooks, so they live in `~/.grok/hooks/partyline.json`, which is silent in any
  directory without a `.partyline/config.json`.
- **Antigravity**: a `PreInvocation` hook (`agy_hook`) injects the first-turn digest and later
  unread peeks, in `~/.gemini/config/hooks.json`.
- **Kimi**: a `Stop` hook (`kimi_hook`) in a managed block of `~/.kimi/config.toml`. Kimi feeds no
  hook stdout to the model; a `Stop` that denies re-runs the turn with its reason as the user
  message. (`kimi --config` replaces the whole config, so it cannot carry hooks per process.)
- **Cursor Agent**: preserving global `sessionStart` and `stop` hooks. The adapter resolves
  clone identity from the workspace payload because global hooks run from `~/.cursor`.
- **Devin**: `SessionStart` and `Stop` hooks merged into the user-level config. The adapter
  resolves clone identity from `DEVIN_PROJECT_DIR` because the hooks live in a global config.
- **Genie Code CLI**: `SessionStart`, `UserPromptSubmit` and `Stop` hooks merged into
  `$GENIE_HOME/hooks.json` (`~/.genie` by default). The contract is Claude-compatible
  except that context injection requires `hookEventName` echoed back (the Qwen quirk),
  and `Stop` blocks on `{"decision": "block"}` — all verified on genie 0.4.1. Genie
  persists hook trust by content hash, so the first session after install asks once
  (`/hooks` manages it; `--dangerously-bypass-hook-trust` for `genie exec` automation).
- **Cline**: file-based hooks — no JSON config, discovered by filename under the
  clone's `.clinerules/hooks/`. `TaskStart` (the SessionStart analog) injects the
  digest and `UserPromptSubmit` peeks unread; both stamp the clone's agent id and
  emit the `{"cancel": false, "contextModification": ...}` envelope. Written per
  launch (gitignored) because they hardcode the agent id.

None of them wakes an idle session. For that, watch the line in another terminal.

The launcher installs the hooks idempotently, then runs the CLI untouched, so it is a drop-in:
`grok-pl [grok args...]`, `agy-pl [agy args...]`, and so on.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .autoregister import register_self

HOME = Path.home()
GROK_HOOKS = HOME / ".grok" / "hooks" / "partyline.json"
AGY_HOOKS = HOME / ".gemini" / "config" / "hooks.json"
KIMI_CONFIG = HOME / ".kimi" / "config.toml"
CMDC_SETTINGS = HOME / ".commandcode" / "settings.json"
CURSOR_HOOKS = HOME / ".cursor" / "hooks.json"
DEVIN_CONFIG = (Path(os.environ["APPDATA"]) / "devin" / "config.json") if os.environ.get("APPDATA") \
    else (HOME / ".config" / "devin" / "config.json")
GENIE_HOOKS = Path(os.environ["GENIE_HOME"]) / "hooks.json" if os.environ.get("GENIE_HOME") \
    else HOME / ".genie" / "hooks.json"
KIMI_BEGIN, KIMI_END = "# >>> partyline hooks (managed by kimi-pl)", "# <<< partyline hooks"
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


def kimi_block() -> str:
    py = _portable_py()
    command = json.dumps(f"{py} -m covasant_partyline.kimi_hook")
    return f'{KIMI_BEGIN}\n[[hooks]]\nevent = "Stop"\ncommand = {command}\ntimeout = {HOOK_TIMEOUT_S}\n{KIMI_END}\n'


def install_kimi_hooks(path: Path = KIMI_CONFIG) -> bool:
    """Keep one managed `[[hooks]]` block in Kimi's config, leaving the user's own hooks alone.

    A top-level `hooks = []` (Kimi's default) cannot coexist with `[[hooks]]` tables, so that one
    line is dropped; a non-empty inline `hooks = [...]` is the user's, so we do not touch the file.
    """
    try:
        old = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        old = ""
    text = re.sub(rf"\n*{re.escape(KIMI_BEGIN)}.*?{re.escape(KIMI_END)}\n", "\n", old, flags=re.S)
    text = re.sub(r"(?m)^hooks\s*=\s*\[\s*\]\s*\n", "", text)
    if re.search(r"(?m)^hooks\s*=", text):
        return False
    new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + kimi_block()
    if new == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def install_cmdc_hooks(path: Path = CMDC_SETTINGS) -> bool:
    """Merge our session/stop hooks without replacing user settings or other hooks."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"cmdc-pl: invalid settings in {path}")
    hooks = data.setdefault("hooks", {})
    command = f"{_portable_py()} -m covasant_partyline.cmdc_hook"
    for event in ("SessionStart", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"cmdc-pl: invalid {event} hooks in {path}")
        # Replace only our exact module invocation; retain the user's other commands.
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise ValueError(f"cmdc-pl: invalid {event} hook entry in {path}")
            entry["hooks"] = [h for h in entry["hooks"]
                              if not (isinstance(h, dict) and str(h.get("command", "")).endswith(
                                  " -m covasant_partyline.cmdc_hook"))]
        entries[:] = [entry for entry in entries if entry["hooks"]]
        entries.append({"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}]})
    return _write_json(path, data)


def install_cursor_hooks(path: Path = CURSOR_HOOKS) -> bool:
    """Merge global Cursor hooks, keeping user hooks and rejecting malformed config."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"version": 1}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"cursor-agent-pl: invalid hooks in {path}")
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    for event in ("sessionStart", "stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError(f"cursor-agent-pl: invalid {event} hooks in {path}")
        suffix = f" -m covasant_partyline.cursor_hook {event}"
        entries[:] = [e for e in entries if not str(e.get("command", "")).endswith(suffix)]
        entry = {"command": f"{_portable_py()}{suffix}", "timeout": HOOK_TIMEOUT_S}
        if event == "stop":
            entry["loop_limit"] = 3
        entries.append(entry)
    return _write_json(path, data)


def which_cli(names: tuple[str, ...]) -> str | None:
    """Find a harness CLI. In WSL, `agy` is `agy.exe` on the imported Windows PATH.

    Linux `shutil.which` does not append PATHEXT, so the bare name misses.
    """
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        if os.name != "nt" and not name.endswith(".exe"):
            found = shutil.which(name + ".exe")
            if found:
                return found
    return None


def cursor_executable() -> str | None:
    """The generic `agent` name can belong to Grok; verify it before using it."""
    exe = which_cli(("cursor-agent",))
    if exe:
        return exe
    exe = which_cli(("agent",))
    if exe:
        try:
            result = subprocess.run([exe, "--help"], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=15)
            if result.returncode == 0 and "Cursor Agent" in result.stdout:
                return exe
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def install_devin_hooks(path: Path = DEVIN_CONFIG) -> bool:
    """Merge SessionStart/Stop hooks into Devin's user config, keeping other settings."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"devin-pl: invalid config in {path}")
    hooks = data.setdefault("hooks", {})
    command = f"{_portable_py()} -m covasant_partyline.devin_hook"
    for event in ("SessionStart", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"devin-pl: invalid {event} hooks in {path}")
        # Replace only our exact module invocation; retain the user's other hooks.
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise ValueError(f"devin-pl: invalid {event} hook entry in {path}")
            entry["hooks"] = [h for h in entry["hooks"]
                              if not (isinstance(h, dict) and str(h.get("command", "")).endswith(
                                  " -m covasant_partyline.devin_hook"))]
        entries[:] = [entry for entry in entries if entry["hooks"]]
        entries.append({"matcher": "", "hooks": [{"type": "command", "command": command,
                                                  "timeout": HOOK_TIMEOUT_S}]})
    return _write_json(path, data)


def install_genie_hooks(path: Path = GENIE_HOOKS) -> bool:
    """Merge the three partyline hooks into Genie's hooks.json, keeping user hooks.

    Genie's file is `{"description": ..., "hooks": {<Event>: [{matcher, hooks: [...]}]}}`
    — the Claude inner shape under a `hooks` key. We replace only our own
    `genie_hook` invocations in each managed event and leave every other entry alone.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError(f"genie-pl: invalid hooks.json in {path}")
    data.setdefault("description", "Partyline session digest, unread peek, and stop gate (managed by genie-pl)")
    hooks = data.setdefault("hooks", {})
    command = f"{_portable_py()} -m covasant_partyline.genie_hook"
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError(f"genie-pl: invalid {event} hooks in {path}")
        for entry in entries:
            if not isinstance(entry.get("hooks"), list):
                raise ValueError(f"genie-pl: invalid {event} hook entry in {path}")
            entry["hooks"] = [h for h in entry["hooks"]
                              if not (isinstance(h, dict) and str(h.get("command", "")).endswith(
                                  " -m covasant_partyline.genie_hook"))]
        entries[:] = [entry for entry in entries if entry["hooks"]]
        entries.append({"matcher": "", "hooks": [{"type": "command", "command": command,
                                                  "timeout": HOOK_TIMEOUT_S}]})
    return _write_json(path, data)


def _cline_shim(module: str, agent_id: str, *, ps1: bool) -> str:
    py = sys.executable
    head = (f"# Partyline {module.rsplit('.', 1)[-1]} for Cline — managed by cline-pl; "
            "per-clone, gitignored. Regenerate by launching via cline-pl.\n")
    if ps1:
        return (head + '$env:PARTYLINE_HOOK_FORMAT = "cline"\n'
                f'$env:PARTYLINE_AGENT_ID = "{agent_id}"\n'
                f'& "{py}" -m {module} @args\n')
    return ("#!/bin/sh\n" + head + "export PARTYLINE_HOOK_FORMAT=cline\n"
            f"export PARTYLINE_AGENT_ID={agent_id}\n"
            f'exec "{py}" -m {module} "$@"\n')


def install_cline_hooks(cwd: Path | None = None) -> bool:
    """Write Cline's file-based hooks into ``<cwd>/.clinerules/hooks/`` (per-clone).

    Cline has no SessionStart event and discovers hooks **by filename**, not a JSON
    config, so the shims live in the clone being launched from: ``TaskStart`` is the
    session-digest analog and ``UserPromptSubmit`` the per-turn unread peek. Each shim
    stamps the clone's resolved agent id and ``PARTYLINE_HOOK_FORMAT=cline`` (the
    ``{"cancel": false, "contextModification": ...}`` envelope the shared modules
    already emit). Like every hook here they fire around a turn, not while idle — the
    VS Code extension's SSE toast is the idle signal. Returns False without writing
    when no identity resolves.
    """
    from . import cli

    root = Path(cwd) if cwd else Path.cwd()
    cfg: dict = {}
    cfg_path = root / ".partyline" / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    agent_id = cli.resolve_agent_id(cfg=cfg)
    if not agent_id:
        print("cline-pl: no partyline identity resolves — run `partyline init` in the clone "
              "(skipping .clinerules/hooks)", file=sys.stderr)
        return False
    hooks_dir = root / ".clinerules" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, module in (("TaskStart", "covasant_partyline.hook"),
                         ("UserPromptSubmit", "covasant_partyline.check_unread")):
        for suffix, ps1 in (("", False), (".ps1", True)):
            p = hooks_dir / (name + suffix)
            content = _cline_shim(module, agent_id, ps1=ps1)
            if not p.is_file() or p.read_text(encoding="utf-8") != content:
                p.write_text(content, encoding="utf-8")
                changed = True
            if not ps1:
                p.chmod(p.stat().st_mode | 0o111)
    return changed


# harness -> (executable names to look for, hook installer)
HARNESSES = {
    "grok": (("grok",), install_grok_hooks),
    "agy": (("agy",), install_agy_hooks),
    "kimi": (("kimi",), install_kimi_hooks),
    "cmdc": (("cmdc", "command-code"), install_cmdc_hooks),
    "cursor": (("cursor-agent", "agent"), install_cursor_hooks),
    "devin": (("devin",), install_devin_hooks),
    "genie": (("genie",), install_genie_hooks),
    "cline": (("cline",), install_cline_hooks),
}


def launch(harness: str, argv: list[str] | None = None) -> None:
    exe_names, install = HARNESSES[harness]
    args = list(sys.argv[1:] if argv is None else argv)
    exe = cursor_executable() if harness == "cursor" else which_cli(exe_names)
    if not exe:
        name = "cursor-agent-pl" if harness == "cursor" else f"{harness}-pl"
        sys.exit(f"{name}: `{exe_names[0]}` not found on PATH (or fallback is not this CLI)")
    install()
    register_self(harness)
    while True:
        try:
            sys.exit(subprocess.run([exe, *args]).returncode)
        except KeyboardInterrupt:
            continue  # Ctrl+C belongs to the TUI, not the launcher


def launch_grok(argv: list[str] | None = None) -> None:
    launch("grok", argv)


def launch_agy(argv: list[str] | None = None) -> None:
    launch("agy", argv)


def launch_kimi(argv: list[str] | None = None) -> None:
    launch("kimi", argv)


def launch_cmdc(argv: list[str] | None = None) -> None:
    launch("cmdc", argv)


def launch_cursor(argv: list[str] | None = None) -> None:
    launch("cursor", argv)


def launch_devin(argv: list[str] | None = None) -> None:
    launch("devin", argv)


def launch_genie(argv: list[str] | None = None) -> None:
    launch("genie", argv)


def launch_cline(argv: list[str] | None = None) -> None:
    launch("cline", argv)


if __name__ == "__main__":
    launch(sys.argv[1], sys.argv[2:])
