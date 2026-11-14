"""`omni-pl <harness>`: start an agent inside Omnigent with Partyline notification.

Every harness is a native Omnigent terminal harness (Claude, Codex, Cursor,
Kimi, Devin, Antigravity, Grok, Command Code, Cline). For that one launch,
Omnigent's harness executable is pointed at the matching `*-pl` binary, so the
existing channel, watcher, or hooks run inside the process Omnigent owns.

Cline is different. It has no `*-pl` launcher and its hooks are per-clone
files in `.clinerules/hooks/`, so a side watcher appends Partyline mail that
arrives during the session into the live Omnigent conversation.

A change of agent type is an Omnigent fork. Native harnesses are forked
through the sessions API, then resumed, because `run --fork` is rejected
for those TUIs. `--resume` stays on the same session. `--fork` and `--resume`
cannot be combined.

    omni-pl grok
    omni-pl cline
    omni-pl cmdc
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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from . import cli
from .autoregister import register_self
from .harness_runner import HARNESSES

Which = Callable[[str], str | None]
OMNIGENT_URL = "http://127.0.0.1:6767"
USAGE = """omni-pl <harness> [--fork CONV] [--resume CONV] [-- extra args]

harness: claude, codex, cursor, kimi, devin, agy, grok, cmdc, cline

--fork CONV    continue that Omnigent conversation on this harness
--resume CONV  reattach the same conversation
extra args     appended to the omnigent command
"""


@dataclass(frozen=True)
class Spec:
    """How one harness is launched and how Partyline mail reaches it."""

    fresh: tuple[str, ...]
    fork_harness: str
    resume_prefix: tuple[str, ...]
    path_env: str | None
    pl_bin: str | None
    hooks: str | None
    watch: bool
    # Built-in agent name for POST /v1/sessions/{id}/fork. None means the
    # harness still accepts `omnigent run --fork`.
    agent_name: str | None
    # The runner reads this env var. The CLI does not put it on the terminal command.
    runner_reads_path: bool


SPECS: dict[str, Spec] = {
    "claude": Spec(("claude",), "claude-native", ("claude", "--resume"), "OMNIGENT_CLAUDE_PATH", "claude-pl", None, False, "claude-native-ui", False),
    "codex": Spec(("codex",), "codex-native", ("codex", "--resume"), "OMNIGENT_CODEX_PATH", "codex-pl", None, False, "codex-native-ui", False),
    "cursor": Spec(("cursor",), "cursor-native", ("cursor", "--resume"), "OMNIGENT_CURSOR_PATH", "cursor-agent-pl", "cursor", False, "cursor-native-ui", True),
    "kimi": Spec(("kimi",), "kimi-native", ("kimi", "--resume"), "OMNIGENT_KIMI_PATH", "kimi-pl", "kimi", False, "kimi-native-ui", True),
    "devin": Spec(("devin",), "devin-native", ("devin", "--resume"), "OMNIGENT_DEVIN_PATH", "devin-pl", "devin", False, "devin-native-ui", True),
    "agy": Spec(("agy",), "antigravity-native", ("agy", "--resume"), "OMNIGENT_ANTIGRAVITY_PATH", "agy-pl", "agy", False, "antigravity-native-ui", False),
    "grok": Spec(("grok",), "grok-native", ("grok", "--resume"), "OMNIGENT_GROK_PATH", "grok-pl", "grok", False, "grok-native-ui", True),
    "cmdc": Spec(("cmdc",), "commandcode-native", ("cmdc", "--resume"), "OMNIGENT_COMMANDCODE_PATH", "cmdc-pl", "cmdc", False, "commandcode-native-ui", True),
    # No cline-pl and no global hook file: the watcher delivers mail.
    "cline": Spec(("cline",), "cline-native", ("cline", "--resume"), None, None, None, True, "cline-native-ui", False),
}


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


def prepare(argv: list[str], which: Which | None = None) -> Plan:
    """Resolve argv to an Omnigent command. Exits on a bad invocation."""
    which = shutil.which if which is None else which
    harness, fork_id, resume_id, extra = _parse(argv)
    spec = SPECS[harness]
    omni = _which(which, "omnigent") or _which(which, "omni")
    if not omni:
        sys.exit("omni-pl: `omnigent` not found on PATH")
    # `run --harness *-native --fork` is rejected by Omnigent. Fork through the
    # API, then resume the new session.
    api_fork = bool(fork_id and spec.agent_name)
    if api_fork:
        command = (omni, *spec.resume_prefix)
    elif fork_id:
        command = (omni, "run", "--fork", fork_id, "--harness", spec.fork_harness, *extra)
    elif resume_id:
        command = (omni, *spec.resume_prefix, resume_id, *extra)
    else:
        command = (omni, *spec.fresh, *extra)
    env: list[tuple[str, str]] = []
    warnings: list[str] = []
    if spec.path_env and spec.pl_bin:
        path = _which(which, spec.pl_bin)
        if path:
            env.append((spec.path_env, path))
            if spec.runner_reads_path:
                # The runner allowlist drops this name. Forward it when this
                # process is the one that spawns the daemon. A daemon that is
                # already up keeps the environment it was started with.
                current = os.environ.get("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "")
                names = [part.strip() for part in current.split(",") if part.strip()]
                if spec.path_env not in names:
                    names.append(spec.path_env)
                env.append(("OMNIGENT_RUNNER_ENV_PASSTHROUGH", ",".join(names)))
                warnings.append(
                    f"omni-pl: Omnigent's runner drops {spec.path_env} when a daemon is already "
                    "running, so this launch may start the stock CLI. Partyline hooks are "
                    "installed either way."
                )
        else:
            warnings.append(
                f"omni-pl: `{spec.pl_bin}` not on PATH; Omnigent will start the stock CLI "
                "and Partyline will not be attached to this session"
            )
    return Plan(
        harness,
        command,
        tuple(env),
        spec.hooks,
        spec.watch,
        fork_id,
        resume_id,
        extra,
        api_fork,
        spec.agent_name if api_fork else None,
        tuple(warnings),
    )


def _parse(argv: list[str]) -> tuple[str, str | None, str | None, tuple[str, ...]]:
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(USAGE)
    harness = argv[0]
    if harness not in SPECS:
        known = ", ".join(sorted(SPECS))
        sys.exit(f"omni-pl: unknown harness {harness!r}. Known: {known}")
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
        exc.close()
        return exc.code, None
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


def find_agent_id(base: str, agent_name: str) -> str | None:
    status, payload = _request_json(method="GET", url=f"{base}/v1/agents?limit=100")
    if status != 200:
        return None
    for agent in sessions_from_payload(payload):
        if agent.get("name") == agent_name and isinstance(agent.get("id"), str):
            return agent["id"]
    return None


def fork_session(base: str, source_id: str, agent_name: str) -> str:
    """Copy a conversation onto a built-in agent. Returns the new session id."""
    agent_id = find_agent_id(base, agent_name)
    if not agent_id:
        sys.exit(f"omni-pl: Omnigent has no agent named {agent_name}")
    status, payload = _request_json(
        method="POST",
        url=f"{base}/v1/sessions/{quote(source_id)}/fork",
        body={"agent_id": agent_id},
    )
    new_id = payload.get("id") if isinstance(payload, dict) else None
    if status != 201 or not isinstance(new_id, str) or not new_id:
        sys.exit(f"omni-pl: fork of {source_id} onto {agent_name} failed (HTTP {status})")
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
                rc = subprocess.run(command, env=env).returncode
                break
            except KeyboardInterrupt:
                continue
    finally:
        stop.set()
    return rc


if __name__ == "__main__":
    sys.exit(launch())
