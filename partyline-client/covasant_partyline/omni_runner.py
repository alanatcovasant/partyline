"""`omni-pl <harness>`: start an agent inside Omnigent with Partyline notification.

omni-pl does not decide which harnesses exist. Omnigent's harness registry is
extensible (plugin and new native harnesses appear without Partyline knowing),
so whatever name you type goes to Omnigent:

- a CLI name (`claude`, `cmdc`, `cline`, `goose`, ...) runs `omnigent <name>`;
- a harness id spelled `<x>-native` or `native-<x>` runs
  `omnigent run --harness <id>`.

`ATTACH` below is an alias layer, not a gate. It only says how Partyline
reaches a harness it has a launcher for: Omnigent's harness executable is
pointed at the matching `*-pl` binary for that one launch, and some harnesses
also get their Partyline hooks installed. A name with no row still launches,
and a side watcher appends Partyline mail that arrives during the session into
the live Omnigent conversation (Cline works this way on purpose: its hooks are
per-clone files and it has no `*-pl` launcher).

A CLI name with no row is checked once with `omnigent <name> --help` before
anything is registered or posted, so a typo dies with Omnigent's own
"No such command" message. A success is cached per Omnigent install in
`~/.partyline/omni-harnesses.json`.

A change of agent type is an Omnigent fork: `POST /v1/sessions/{id}/fork`,
then a resume of the new session, because `run --fork` is rejected for the
native TUIs. `--resume` stays on the same session. `--fork` and `--resume`
cannot be combined.

    omni-pl grok
    omni-pl cline
    omni-pl cmdc
    omni-pl cline-native
    omni-pl claude --fork conv_abc123
    omni-pl codex --resume conv_abc123 -- --sandbox workspace-write
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import cli
from .autoregister import register_self
from .harness_runner import HARNESSES

Which = Callable[[str], str | None]
# Runs `omnigent <name> --help`. Returns (exit code, combined output).
Probe = Callable[[str, str], tuple[int, str]]
OMNIGENT_URL = "http://127.0.0.1:6767"
USAGE = """omni-pl <harness> [--fork CONV] [--resume CONV] [-- extra args]

harness: any harness the installed Omnigent knows. Either a CLI name from
         `omnigent --help` (claude, codex, cursor, kimi, devin, agy, grok, cmdc,
         cline, goose, opencode, pi, qwen, ...) or a harness id such as
         cline-native or native-grok. Omnigent validates it, not omni-pl.

--fork CONV    continue that Omnigent conversation on this harness
--resume CONV  reattach the same conversation
extra args     appended to the omnigent command
"""


@dataclass(frozen=True)
class Attach:
    """How Partyline reaches one harness. Absence never blocks a launch."""

    path_env: str | None
    pl_bin: str | None
    hooks: str | None
    watch: bool
    # The runner reads this env var. The CLI does not put it on the terminal command.
    runner_reads_path: bool
    # Built-in Omnigent agent for the API fork, when it does not follow the
    # `<name>-native-ui` convention every other native harness uses.
    agent_name: str | None = None


# Non-authoritative: names Partyline has a launcher or hooks for. Omnigent
# decides which harnesses exist.
ATTACH: dict[str, Attach] = {
    "claude": Attach("OMNIGENT_CLAUDE_PATH", "claude-pl", None, False, False),
    "codex": Attach("OMNIGENT_CODEX_PATH", "codex-pl", None, False, False),
    "cursor": Attach("OMNIGENT_CURSOR_PATH", "cursor-agent-pl", "cursor", False, True),
    "kimi": Attach("OMNIGENT_KIMI_PATH", "kimi-pl", "kimi", False, True),
    "devin": Attach("OMNIGENT_DEVIN_PATH", "devin-pl", "devin", False, True),
    "agy": Attach("OMNIGENT_ANTIGRAVITY_PATH", "agy-pl", "agy", False, False, "antigravity-native-ui"),
    "grok": Attach("OMNIGENT_GROK_PATH", "grok-pl", "grok", False, True),
    "cmdc": Attach("OMNIGENT_COMMANDCODE_PATH", "cmdc-pl", "cmdc", False, True, "commandcode-native-ui"),
    # No cline-pl and no global hook file: the watcher delivers mail.
    "cline": Attach(None, None, None, True, False),
}
# Other spellings Omnigent accepts for a harness in ATTACH. Lookup only.
SYNONYMS = {"cmd": "cmdc", "commandcode": "cmdc", "antigravity": "agy"}
# Any harness Partyline has no launcher for: the session watcher delivers mail.
WATCH_ONLY = Attach(None, None, None, True, False)


@dataclass(frozen=True)
class Plan:
    harness: str
    command: tuple[str, ...]
    env_updates: tuple[tuple[str, str], ...]
    hooks: str | None
    watch: bool
    fork_id: str | None
    resume_id: str | None
    extra: tuple[str, ...]
    api_fork: bool
    fork_agent: str | None
    warnings: tuple[str, ...]


def _which(which: Which, name: str) -> str | None:
    """Resolve a CLI. On Linux, also try the Windows `name.exe` form."""
    found = which(name)
    if found:
        return found
    if os.name != "nt" and not name.endswith(".exe"):
        return which(name + ".exe")
    return None


def native_base(name: str) -> str | None:
    """`cline` for `cline-native` or `native-cline`; None for a CLI name."""
    if name.endswith("-native") and len(name) > len("-native"):
        return name[: -len("-native")]
    if name.startswith("native-") and len(name) > len("native-"):
        return name[len("native-") :]
    return None


def resolve_attach(name: str) -> tuple[str, Attach]:
    """The ATTACH key for a typed name and how Partyline reaches it."""
    base = native_base(name) or name
    key = SYNONYMS.get(base, base)
    return key, ATTACH.get(key, WATCH_ONLY)


def fork_agent_for(name: str) -> str:
    key, attach = resolve_attach(name)
    return attach.agent_name or f"{key}-native-ui"


def _cache_path() -> Path:
    return Path.home() / ".partyline" / "omni-harnesses.json"


def _install_stamp(omni: str) -> str:
    try:
        st = Path(omni).resolve().stat()
    except OSError:
        return omni
    return f"{Path(omni).resolve()}:{st.st_mtime_ns}:{st.st_size}"


def _load_known(stamp: str) -> set[str]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, dict) or raw.get("stamp") != stamp:
        return set()
    names = raw.get("names")
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def _save_known(stamp: str, names: set[str]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stamp": stamp, "names": sorted(names)}), encoding="utf-8")
    except OSError:
        pass  # the cache only saves a probe next time


def _run_probe(omni: str, name: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            [omni, name, "--help"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return done.returncode, (done.stderr or "") + (done.stdout if done.returncode else "")


def confirm_with_omnigent(omni: str, name: str, probe: Probe | None = None) -> None:
    """Exit with Omnigent's own error when it has no `omnigent <name>` command.

    Not a list check: Omnigent answers. Names in ATTACH/SYNONYMS and harness
    ids (`*-native`) skip the probe; Omnigent still rejects a bad one at launch.
    """
    if name in ATTACH or name in SYNONYMS or native_base(name):
        return
    stamp = _install_stamp(omni)
    known = _load_known(stamp)
    if name in known:
        return
    code, output = (probe or _run_probe)(omni, name)
    if code == 0:
        _save_known(stamp, known | {name})
        return
    if code == -1:
        # Could not ask (timeout, exec failure). Pass through; Omnigent reports.
        return
    sys.stderr.write(output if output.endswith("\n") or not output else output + "\n")
    sys.exit(code)


def prepare(argv: list[str], which: Which | None = None, probe: Probe | None = None) -> Plan:
    """Resolve argv to an Omnigent command. Exits on a bad invocation."""
    which = shutil.which if which is None else which
    harness, fork_id, resume_id, extra = _parse(argv)
    _, attach = resolve_attach(harness)
    omni = _which(which, "omnigent") or _which(which, "omni")
    if not omni:
        sys.exit("omni-pl: `omnigent` not found on PATH")
    confirm_with_omnigent(omni, harness, probe)
    launcher = (omni, "run", "--harness", harness) if native_base(harness) else (omni, harness)
    # `run --fork` is rejected for the native TUIs. Fork through the API,
    # then resume the new session.
    api_fork = bool(fork_id)
    if api_fork:
        command = (*launcher, "--resume")
    elif resume_id:
        command = (*launcher, "--resume", resume_id, *extra)
    else:
        command = (*launcher, *extra)
    env: list[tuple[str, str]] = []
    warnings: list[str] = []
    if attach.path_env and attach.pl_bin:
        path = _which(which, attach.pl_bin)
        if path:
            env.append((attach.path_env, path))
            if attach.runner_reads_path:
                # The runner allowlist drops this name. Forward it when this
                # process is the one that spawns the daemon. A daemon that is
                # already up keeps the environment it was started with.
                current = os.environ.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "")
                names = [part.strip() for part in current.split(",") if part.strip()]
                if attach.path_env not in names:
                    names.append(attach.path_env)
                env.append(("OMNIGENT_RUNNER_ENV_PASSTHROUGH", ",".join(names)))
                warnings.append(
                    f"omni-pl: Omnigent's runner drops {attach.path_env} when a daemon is already "
                    "running, so this launch may start the stock CLI. Partyline hooks are "
                    "installed either way."
                )
        else:
            warnings.append(
                f"omni-pl: `{attach.pl_bin}` not on PATH; Omnigent will start the stock CLI "
                "and Partyline will not be attached to this session"
            )
    return Plan(
        harness,
        command,
        tuple(env),
        attach.hooks,
        attach.watch,
        fork_id,
        resume_id,
        extra,
        api_fork,
        fork_agent_for(harness) if api_fork else None,
        tuple(warnings),
    )


def _parse(argv: list[str]) -> tuple[str, str | None, str | None, tuple[str, ...]]:
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(USAGE)
    harness = argv[0]
    if harness.startswith("-"):
        sys.exit(f"omni-pl: the harness name comes first, got {harness!r}\n\n{USAGE}")
    fork_id = resume_id = None
    extra: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            extra.extend(argv[i + 1 :])
            break
        if arg in ("--fork", "--resume"):
            i += 1
            if i >= len(argv):
                sys.exit(f"omni-pl: {arg} needs a conversation id")
            if arg == "--fork":
                fork_id = argv[i]
            else:
                resume_id = argv[i]
        else:
            extra.append(arg)
        i += 1
    if fork_id and resume_id:
        sys.exit("omni-pl: pass only one of --fork and --resume")
    return harness, fork_id, resume_id, tuple(extra)


def mail_text(msg: dict) -> str:
    summary = (msg.get("summary") or "").strip()
    payload = (msg.get("payload") or "").strip()
    body = summary if not payload else (f"{summary}\n{payload}" if summary else payload)
    return f"Partyline #{msg.get('id')} [{msg.get('message_type')}] from {msg.get('agent_id')}: {body}"


def omnigent_event(text: str) -> dict:
    return {"type": "message", "data": {"role": "user", "content": [{"type": "input_text", "text": text}]}}


def message_is_new(msg: dict, started: float) -> bool:
    """True when the message was emitted at or after this launch.

    Unread is everything since the checkpoint, which includes old mail. Only
    mail that arrives while Omnigent is running should enter the session.
    """
    raw = msg.get("event_time") or msg.get("created_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp() >= started - 2


def choose_session(
    sessions: list[dict],
    *,
    resume_id: str | None,
    fork_id: str | None,
    known_ids: set[str] | None = None,
    started: float | None = None,
) -> str | None:
    """Pick the Omnigent conversation the watcher should write into.

    `known_ids` is the session list from before this launch. A session that
    was not in that set is the one Omnigent just created. Comparing
    `created_at` to the local clock drops that session when the machine is
    ahead of the Omnigent host.
    """
    if resume_id:
        return resume_id
    fresh: list[dict] = []
    for session in sessions:
        sid = session.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        if fork_id and sid == fork_id:
            continue
        if known_ids is not None:
            if sid not in known_ids:
                fresh.append(session)
            continue
        created = session.get("created_at") or 0
        if started is not None and isinstance(created, (int, float)) and created >= started - 2:
            fresh.append(session)
    if not fresh:
        return None
    fresh.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return str(fresh[0]["id"])


def sessions_from_payload(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _seen_path(agent_id: str) -> Path:
    return Path.home() / ".partyline" / f"omni-delivered-{agent_id}.json"


def load_seen(agent_id: str) -> set[int]:
    try:
        raw = json.loads(_seen_path(agent_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, int)}


def save_seen(agent_id: str, seen: set[int]) -> None:
    path = _seen_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = sorted(seen)[-400:]
    path.write_text(json.dumps(kept), encoding="utf-8")


def post_fork_notice(harness: str, conv_id: str) -> None:
    """Tell the fleet this conversation moved onto another harness. Best-effort."""
    cfg = cli._load_config()
    agent_id = cli.resolve_agent_id(cfg=cfg)
    if not agent_id:
        return
    url = cli.resolve_url(cfg=cfg)
    profile = cli.resolve_profile(url=url, cfg=cfg)
    if not os.getenv("PARTYLINE_TOKEN") and profile:
        token = cli._mint_token(profile)
        if token:
            os.environ["PARTYLINE_TOKEN"] = token
    try:
        cli._request(
            "POST",
            url,
            "/api/messages",
            {
                "agent_id": agent_id,
                "message_type": "status",
                "summary": f"omnigent fork {conv_id} onto {harness}",
                "payload": f"Continue Omnigent conversation {conv_id} on {harness}.",
                "metadata": {"omnigent_fork_from": conv_id, "omnigent_harness": harness},
                "mentions": [],
                "tag_everyone": False,
            },
        )
    except SystemExit:
        print("omni-pl: partyline handoff post failed", file=sys.stderr)


def _omnigent_base() -> str:
    return os.environ.get("OMNIGENT_URL", OMNIGENT_URL).rstrip("/")


def _request_json(method: str, url: str, body: dict | None = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    token = os.environ.get("OMNIGENT_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        # Keep Omnigent's error body: it names the problem (e.g. a stale
        # server's "invalid agent spec ... got 'cline-native'").
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        finally:
            exc.close()
        try:
            return exc.code, json.loads(raw.decode()) if raw else None
        except ValueError:
            return exc.code, {"detail": raw.decode(errors="replace").strip()}
    except (OSError, urllib.error.URLError):
        return 0, None
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode())
    except ValueError:
        return status, None


def discover_session(
    base: str, known_ids: set[str] | None, fork_id: str | None, resume_id: str | None
) -> str | None:
    if resume_id:
        return resume_id
    if known_ids is None:
        return None
    status, payload = _request_json(method="GET", url=f"{base}/v1/sessions?limit=20&visibility=mine")
    if status != 200:
        return None
    return choose_session(
        sessions_from_payload(payload), resume_id=None, fork_id=fork_id, known_ids=known_ids
    )


def snapshot_session_ids(base: str) -> set[str] | None:
    """Session ids that already exist. None when the list could not be read."""
    status, payload = _request_json(method="GET", url=f"{base}/v1/sessions?limit=100&visibility=mine")
    if status != 200:
        return None
    ids: set[str] = set()
    for session in sessions_from_payload(payload):
        sid = session.get("id")
        if isinstance(sid, str) and sid:
            ids.add(sid)
    return ids


def omnigent_detail(payload: object) -> str:
    """Omnigent's own error text from a response body, verbatim where possible."""
    if isinstance(payload, dict):
        for key in ("detail", "error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                inner = value.get("message") or value.get("detail")
                if isinstance(inner, str) and inner:
                    return inner
            if value:
                return json.dumps(value)
    if payload is None:
        return ""
    return json.dumps(payload)


def _omnigent_failure(what: str, base: str, status: int, payload: object) -> str:
    if status == 0:
        return f"omni-pl: {what}: Omnigent server not reachable at {base} (set OMNIGENT_URL)"
    detail = omnigent_detail(payload)
    return f"omni-pl: {what}: Omnigent returned HTTP {status}" + (f": {detail}" if detail else "")


def find_agent_id(base: str, agent_name: str) -> str | None:
    status, payload = _request_json(method="GET", url=f"{base}/v1/agents?limit=100")
    if status != 200:
        sys.exit(_omnigent_failure("listing agents", base, status, payload))
    for agent in sessions_from_payload(payload):
        if agent.get("name") == agent_name and isinstance(agent.get("id"), str):
            return agent["id"]
    return None


def fork_session(base: str, source_id: str, agent_name: str) -> str:
    """Copy a conversation onto a built-in agent. Returns the new session id."""
    agent_id = find_agent_id(base, agent_name)
    if not agent_id:
        sys.exit(
            f"omni-pl: Omnigent has no agent named {agent_name}, so this harness cannot "
            "take a fork. Check `omnigent agent` or fork with omnigent directly."
        )
    status, payload = _request_json(
        method="POST",
        url=f"{base}/v1/sessions/{quote(source_id)}/fork",
        body={"agent_id": agent_id},
    )
    new_id = payload.get("id") if isinstance(payload, dict) else None
    if status != 201 or not isinstance(new_id, str) or not new_id:
        sys.exit(_omnigent_failure(f"fork of {source_id} onto {agent_name}", base, status, payload))
    return new_id


def prime_delivered(agent_id: str, url: str) -> set[int]:
    """Remember mail that already existed so the watcher does not replay it.

    Ids are taken from the unread list, not from the server clock. Mail that
    arrives after this snapshot is delivered even when the local clock is ahead.
    """
    seen = load_seen(agent_id)
    for msg in fetch_unread(agent_id, url):
        mid = msg.get("id")
        if isinstance(mid, int):
            seen.add(mid)
    save_seen(agent_id, seen)
    return seen


def deliver_mail(base: str, session_id: str, text: str) -> str:
    """POST one user message. Returns ok, gone, or retry."""
    status, _ = _request_json(
        method="POST",
        url=f"{base}/v1/sessions/{quote(session_id)}/events",
        body=omnigent_event(text),
    )
    if status in (200, 202):
        return "ok"
    if status == 404:
        return "gone"
    return "retry"


def fetch_unread(agent_id: str, url: str) -> list[dict]:
    try:
        data = cli._request("GET", url, f"/api/agents/{quote(agent_id)}/unread?only_for_me=true")
    except SystemExit:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def watch_omnigent(
    agent_id: str,
    plan: Plan,
    stop: threading.Event,
    seen: set[int],
    known_sessions: set[str] | None,
) -> None:
    """Append Partyline mail that arrives during this session into Omnigent."""
    cfg = cli._load_config()
    url = cli.resolve_url(cfg=cfg)
    base = _omnigent_base()
    session_id = plan.resume_id
    while not stop.is_set():
        if known_sessions is None:
            known_sessions = snapshot_session_ids(base)
        if session_id is None and known_sessions is not None:
            session_id = discover_session(base, known_sessions, plan.fork_id, None)
        if session_id is not None:
            delivered = False
            for msg in fetch_unread(agent_id, url):
                mid = msg.get("id")
                if not isinstance(mid, int) or mid in seen or msg.get("agent_id") == agent_id:
                    continue
                result = deliver_mail(base, session_id, mail_text(msg))
                if result == "gone" and plan.resume_id is None:
                    session_id = None
                    break
                if result == "ok":
                    seen.add(mid)
                    delivered = True
            if delivered:
                save_seen(agent_id, seen)
        stop.wait(5)


def launch(argv: list[str] | None = None) -> int:
    """Entry point for `omni-pl`."""
    args = list(sys.argv[1:] if argv is None else argv)
    plan = prepare(args)
    for warning in plan.warnings:
        print(warning, file=sys.stderr)
    if plan.hooks:
        HARNESSES[plan.hooks][1]()
    register_self(plan.harness)
    command = plan.command
    if plan.api_fork and plan.fork_id and plan.fork_agent:
        new_id = fork_session(_omnigent_base(), plan.fork_id, plan.fork_agent)
        command = (*plan.command, new_id, *plan.extra)
        # The new session exists before the watcher snapshots the list, so
        # it would never look new. Watch it by id.
        plan = replace(plan, resume_id=new_id)
    if plan.fork_id:
        post_fork_notice(plan.harness, plan.fork_id)
    stop = threading.Event()
    if plan.watch:
        cfg = cli._load_config()
        agent_id = cli.resolve_agent_id(cfg=cfg)
        if agent_id:
            url = cli.resolve_url(cfg=cfg)
            seen = prime_delivered(agent_id, url)
            known = snapshot_session_ids(_omnigent_base())
            threading.Thread(
                target=watch_omnigent,
                args=(agent_id, plan, stop, seen, known),
                daemon=True,
            ).start()
        else:
            print(f"omni-pl: no .partyline/config.json agentId; {plan.harness} session watcher not started", file=sys.stderr)
    env = os.environ.copy()
    env.update(dict(plan.env_updates))
    try:
        while True:
            try:
                rc = subprocess.run(command, env=env, check=False).returncode
                break
            except KeyboardInterrupt:
                continue
    finally:
        stop.set()
    return rc


if __name__ == "__main__":
    sys.exit(launch())
