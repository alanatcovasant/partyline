"""`partyline setup` (once per machine) and `partyline init` (once per clone).

setup  registers the live channel as a *user-scope* Claude Code MCP server, installs a
       `claude-pl` launcher that passes the development-channel flag, and remembers
       owner/url/profile in ~/.partyline/user.json.
init   writes this clone's gitignored .partyline/config.json with the next free
       `<owner>_<repo>_<n>` agent id, then registers the agent.

The channel resolves its identity from the nearest .partyline/config.json (cli._load_config
walks up from the cwd), so one user-scope server definition serves every clone — the
per-clone id never has to live in a tracked file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from . import cli

CHANNEL_SERVER = "partyline-channel"
CHANNEL_FLAG = f"server:{CHANNEL_SERVER}"
USER_FILE = Path.home() / ".partyline" / "user.json"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "repo"


def next_agent_id(owner: str, repo: str, existing: Iterable[str]) -> str:
    """Lowest `<owner>_<repo>_<n>` (n>=1) not already registered on the line."""
    prefix = f"{_slug(owner)}_{_slug(repo)}_"
    taken = {
        int(i[len(prefix) :]) for i in existing if i.startswith(prefix) and i[len(prefix) :].isdigit()
    }
    n = 1
    while n in taken:
        n += 1
    return f"{prefix}{n}"


def _agent_ids(payload: object) -> list[str]:
    rows = payload.get("agents", []) if isinstance(payload, dict) else payload or []
    return [r["agent_id"] for r in rows if isinstance(r, dict) and r.get("agent_id")]


def _load_user() -> dict:
    try:
        return json.loads(USER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure_login(profile: str, host: str | None) -> None:
    """Make sure `profile` can mint a Databricks token, logging in interactively if not.

    The app requires OAuth on every route, so nothing else works until this does. The
    login opens a browser — unavoidable for U2M OAuth, but it is a command, not a UI hunt.
    """
    if cli._mint_token(profile):
        return
    db = shutil.which("databricks")
    if not db:
        sys.exit("partyline: the `databricks` CLI is not installed or not on PATH")
    if not host:
        sys.exit(
            f"partyline: profile '{profile}' has no valid login and no workspace host is known.\n"
            f"  run: databricks auth login --host <workspace-url> --profile {profile}\n"
            "  then re-run, or pass --workspace-host so setup can do it for you"
        )
    print(f"==> No valid Databricks login for profile '{profile}'; opening browser login ({host})")
    subprocess.run([db, "auth", "login", "--host", host, "--profile", profile], check=False)
    if not cli._mint_token(profile):
        sys.exit(f"partyline: login for profile '{profile}' did not complete; re-run `partyline setup`")


def _windows_path_to_wsl(path: str) -> str | None:
    """Map `C:\\Python314\\python.exe` to `/mnt/c/Python314/python.exe`.

    A drive-letter path is not an executable name inside WSL. Git Bash can run the
    Windows spelling; WSL has to use the mount point.
    """
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not match:
        return None
    rest = match.group(2).replace("\\", "/")
    return f"/mnt/{match.group(1).lower()}/{rest}"


def _shell_launcher(executable: str | None, command: str) -> str:
    """POSIX launcher body. Always LF: a CR in the shebang makes WSL look up `/bin/sh\\r`.

    `executable` is the interpreter that has this package, or None when `command` is
    already the program to exec (the claude wrapper). A Windows interpreter gets a
    WSL branch so the same file works from Git Bash and from WSL.
    """
    if executable is None:
        return f'#!/bin/sh\nexec {command} "$@"\n'
    wsl = _windows_path_to_wsl(executable)
    if wsl is None:
        return f'#!/bin/sh\nexec "{executable}" {command} "$@"\n'
    return (
        "#!/bin/sh\n"
        'if [ -n "${WSL_DISTRO_NAME-}" ]; then\n'
        f'  exec "{wsl}" {command} "$@"\n'
        "fi\n"
        f'exec "{executable}" {command} "$@"\n'
    )


def _write_text(path: Path, text: str, newline: str) -> None:
    path.write_text(text, encoding="utf-8", newline=newline)


def _launchers(bin_dir: Path) -> list[Path]:
    cmd = bin_dir / "claude-pl.cmd"
    sh = bin_dir / "claude-pl"
    claude_args = f"claude --dangerously-load-development-channels {CHANNEL_FLAG}"
    # newline= is required. The default translator turns `\n` into `\r\n` on Windows, and a
    # shebang of `#!/bin/sh\r` is the WSL error "cannot execute: required file not found".
    _write_text(cmd, f"@echo off\n{claude_args} %*\n", "\r\n")
    _write_text(sh, _shell_launcher(None, claude_args), "\n")
    # The other launchers run under the interpreter that has this package (a pipx venv is not the
    # system python), so each pins sys.executable rather than relying on a console script.
    py = sys.executable
    written = [cmd, sh]
    for name, module, extra in (
        ("codex-pl", "covasant_partyline.codex_runner", ""),
        ("grok-pl", "covasant_partyline.harness_runner", " grok"),
        ("agy-pl", "covasant_partyline.harness_runner", " agy"),
        ("kimi-pl", "covasant_partyline.harness_runner", " kimi"),
        ("cmdc-pl", "covasant_partyline.harness_runner", " cmdc"),
        ("cursor-agent-pl", "covasant_partyline.harness_runner", " cursor"),
        ("devin-pl", "covasant_partyline.harness_runner", " devin"),
        ("genie-pl", "covasant_partyline.harness_runner", " genie"),
        ("cline-pl", "covasant_partyline.harness_runner", " cline"),
        ("omni-pl", "covasant_partyline.omni_runner", ""),
    ):
        bat, script = bin_dir / f"{name}.cmd", bin_dir / name
        _write_text(bat, f'@echo off\n"{py}" -m {module}{extra} %*\n', "\r\n")
        _write_text(script, _shell_launcher(py, f"-m {module}{extra}"), "\n")
        written += [bat, script]
    for p in written:
        if p.suffix != ".cmd":
            try:
                p.chmod(0o755)
            except OSError:
                pass
    return written


def _wsl_install_script(client_wsl_path: str) -> str:
    """Bash that pipx-installs the client in the default WSL distro and writes Linux launchers.

    The scripts land in ~/.local/bin, which a normal Ubuntu login puts ahead of the
    imported Windows PATH. Those copies pin the Linux pipx interpreter, so WSL runs
    Linux `agy` / `grok` / `claude` rather than `python.exe`.
    """
    src = shlex.quote(client_wsl_path)
    return f"""set -euo pipefail
if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx is not installed in the default WSL distro" >&2
  exit 1
fi
pipx install --force {src}
PY="$HOME/.local/share/pipx/venvs/covasant-partyline/bin/python"
if [ ! -x "$PY" ]; then
  echo "pipx venv python not found at $PY" >&2
  exit 1
fi
mkdir -p "$HOME/.local/bin"
"$PY" - <<'PY'
from pathlib import Path
from covasant_partyline.setup import _launchers
for p in _launchers(Path.home() / ".local" / "bin"):
    print(p)
PY
"""


def install_wsl_launchers(*, strict: bool) -> bool:
    """Install Linux `*-pl` launchers into the default WSL distro. No-op off Windows.

    Windows `partyline setup` writes launchers next to the Windows interpreter. WSL
    imports that directory onto PATH and then refuses the CRLF shebang. This puts a
    real Linux copy on ~/.local/bin, which sorts first.
    """
    if os.name != "nt":
        return False
    if not shutil.which("wsl"):
        msg = "wsl.exe not on PATH; Linux launchers not installed"
        if strict:
            sys.exit(f"partyline: {msg}")
        print(f"note: {msg}")
        return False
    client = Path(__file__).resolve().parents[1]
    src = _windows_path_to_wsl(str(client.resolve()))
    if not src:
        msg = f"cannot map {client} into a WSL path"
        if strict:
            sys.exit(f"partyline: {msg}")
        print(f"note: {msg}")
        return False
    # Bytes, not text mode: on Windows text stdin turns `\n` into `\r\n`, and bash
    # then rejects `set -o pipefail\r`. The source file may already contain CR too.
    script = _wsl_install_script(src).replace("\r\n", "\n").replace("\r", "\n").encode()
    result = subprocess.run(
        ["wsl", "-e", "bash", "-s"],
        input=script,
        capture_output=True,
    )
    out = result.stdout.decode(errors="replace")
    err = result.stderr.decode(errors="replace")
    if out.strip():
        print(out.strip())
    if result.returncode != 0:
        detail = (err or out or "wsl exited non-zero").strip()
        if strict:
            sys.exit(f"partyline: WSL launcher install failed\n{detail}")
        print(f"note: WSL launcher install failed\n{detail}")
        return False
    print("Linux launchers are on ~/.local/bin in the default WSL distro (ahead of the Windows copies).")
    return True


def cmd_install_wsl_launchers(a: argparse.Namespace | None = None) -> None:
    """Write Linux launchers. Inside WSL this uses the current interpreter.

    On Windows, also rewrite the launchers beside `partyline` so the Git Bash copies
    use LF shebangs. WSL still prefers the ~/.local/bin copies installed below.
    """
    if os.name != "nt":
        dest = Path.home() / ".local" / "bin"
        dest.mkdir(parents=True, exist_ok=True)
        for p in _launchers(dest):
            print(f"wrote launcher {p}")
        return
    exe = shutil.which("partyline")
    if exe:
        for p in _launchers(Path(exe).parent):
            print(f"wrote launcher {p}")
    install_wsl_launchers(strict=True)


def cmd_setup(a: argparse.Namespace) -> None:
    user = _load_user()
    user.update(
        {k: v for k, v in (("owner", a.owner), ("url", a.url_), ("profile", a.profile_), ("host", a.host_)) if v}
    )
    if not user.get("owner"):
        sys.exit("partyline setup: pass --owner (your handle, e.g. alan) the first time")
    USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_FILE.write_text(json.dumps(user, indent=2), encoding="utf-8")
    print(f"remembered {USER_FILE}: {user}")
    ensure_login(user.get("profile") or "dev-eastus2", user.get("host"))

    claude = shutil.which("claude")
    if not claude:
        sys.exit("partyline setup: `claude` not found on PATH")
    have = subprocess.run([claude, "mcp", "get", CHANNEL_SERVER], capture_output=True, text=True)
    if have.returncode == 0:
        print(f"user-scope MCP server '{CHANNEL_SERVER}' already registered")
    else:
        profile = user.get("profile") or "dev-eastus2"
        subprocess.run(
            # name first: `-e` is variadic and would otherwise swallow it
            [claude, "mcp", "add", CHANNEL_SERVER, "--scope", "user", "-e", f"DATABRICKS_CONFIG_PROFILE={profile}",
             "--", "partyline-channel"],
            check=True,
        )
        print(f"registered user-scope MCP server '{CHANNEL_SERVER}'")

    exe = shutil.which("partyline")
    if exe:
        for p in _launchers(Path(exe).parent):
            print(f"wrote launcher {p}")
    else:
        print("note: `partyline` not on PATH, skipped the claude-pl launcher")
    install_wsl_launchers(strict=False)
    print(
        "\nNext: in each clone run `partyline init`, then start Claude with `claude-pl` "
        "(or `codex-pl` / `grok-pl` / `agy-pl` / `kimi-pl` / `cmdc-pl` / `cursor-agent-pl` / `devin-pl` / `genie-pl` / `cline-pl` / `omni-pl`). "
        "From WSL those commands are the Linux copies on ~/.local/bin."
    )


def _repo_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else Path.cwd()


def _ensure_ignored(root: Path) -> None:
    gi = root / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if not any(line.strip() in (".partyline", ".partyline/") for line in text.splitlines()):
        gi.write_text(text + ("" if text.endswith("\n") or not text else "\n") + ".partyline/\n", encoding="utf-8")
        print(f"added .partyline/ to {gi}")


def cmd_init(a: argparse.Namespace) -> None:
    root = _repo_root()
    cfg_path = root / ".partyline" / "config.json"
    if cfg_path.exists() and not a.force:
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8")).get("agentId")
        except (OSError, ValueError):
            existing = None
        if existing:
            print(f"{cfg_path} already has agentId {existing} (use --force to reassign)")
            return

    user = _load_user()
    owner = a.owner or user.get("owner")
    if not owner:
        sys.exit("partyline init: no owner — run `partyline setup --owner <handle>` first or pass --owner")
    url = a.url if a.url and not cli.is_local_url(a.url) else user.get("url") or a.url
    profile = a.profile or user.get("profile") or "dev-eastus2"
    repo = a.repo or root.name

    if not os.getenv("PARTYLINE_TOKEN") and not cli.is_local_url(url):
        ensure_login(profile, user.get("host"))
        token = cli._mint_token(profile)
        if token:
            os.environ["PARTYLINE_TOKEN"] = token
    ids = _agent_ids(cli._request("GET", url, "/api/agents?include_stale=true"))
    agent_id = next_agent_id(owner, repo, ids)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps({"url": url, "agentId": agent_id, "databricksProfile": profile}, indent=2), encoding="utf-8"
    )
    _ensure_ignored(root)
    cli._request(
        "POST", url, "/api/agents",
        {"agent_id": agent_id, "owner": owner, "repo": repo, "workdirs": [cli.normalize_target(str(root))]},
    )
    print(f"initialised {root} as {agent_id}")
