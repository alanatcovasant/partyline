"""`omni-pl <harness>`: start an agent inside Omnigent with Partyline notification.

Native terminal harnesses (Claude, Codex, Cursor, Kimi, Devin, Antigravity) are
started by Omnigent itself. For that one launch, Omnigent's harness executable
is pointed at the matching `*-pl` binary, so the existing channel, watcher, or
hooks run inside the process Omnigent owns.

Grok is different. Omnigent drives `grok agent stdio` over ACP, which does not
load `~/.grok/hooks/partyline.json`. `omni-pl grok` still installs those hooks
for a direct `grok` launch, and a side watcher appends Partyline mail that
arrives during the session into the live Omnigent conversation.

A change of agent type is an Omnigent fork. `--resume` stays on the same
session. `--fork` and `--resume` cannot be combined.

    omni-pl grok
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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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

harness: grok, claude, codex, cursor, kimi, devin, agy

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


SPECS: dict[str, Spec] = {
    "claude": Spec(("claude",), "claude-native", ("claude", "--resume"), "OMNIGENT_CLAUDE_PATH", "claude-pl", None, False),
    "codex": Spec(("codex",), "codex-native", ("codex", "--resume"), "OMNIGENT_CODEX_PATH", "codex-pl", None, False),
    "cursor": Spec(("cursor",), "cursor-native", ("cursor", "--resume"), "OMNIGENT_CURSOR_PATH", "cursor-agent-pl", "cursor", False),
    "kimi": Spec(("kimi",), "kimi-native", ("kimi", "--resume"), "OMNIGENT_KIMI_PATH", "kimi-pl", "kimi", False),
    "devin": Spec(("devin",), "devin-native", ("devin", "--resume"), "OMNIGENT_DEVIN_PATH", "devin-pl", "devin", False),
    "agy": Spec(("agy",), "antigravity-native", ("agy", "--resume"), "OMNIGENT_AGY_PATH", "agy-pl", "agy", False),
    # ACP. Do not point Omnigent at grok-pl: that binary is the TUI wrapper.
    "grok": Spec(("run", "--harness", "grok"), "grok", ("run", "--harness", "grok", "--resume"), None, None, "grok", True),
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
    warnings: tuple[str, ...]


def prepare(argv: list[str], which: Which | None = None) -> Plan:
    """Resolve argv to an Omnigent command. Exits on a bad invocation."""
    which = shutil.which if which is None else which
    harness, fork_id, resume_id, extra = _parse(argv)
    spec = SPECS[harness]
    omni = which("omnigent") or which("omni")
    if not omni:
        sys.exit("omni-pl: `omnigent` not found on PATH")
    if fork_id:
        command = (omni, "run", "--fork", fork_id, "--harness", spec.fork_harness, *extra)
    elif resume_id:
        command = (omni, *spec.resume_prefix, resume_id, *extra)
    else:
        command = (omni, *spec.fresh, *extra)
    env: list[tuple[str, str]] = []
    warnings: list[str] = []
    if spec.path_env and spec.pl_bin:
        path = which(spec.pl_bin)
        if path:
            env.append((spec.path_env, path))
        else:
            warnings.append(
                f"omni-pl: `{spec.pl_bin}` not on PATH; Omnigent will start the stock CLI "
                "and Partyline will not be attached to this session"
            )
    return Plan(harness, command, tuple(env), spec.hooks, spec.watch, fork_id, resume_id, tuple(warnings))


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
    sessions: list[dict], *, started: float, resume_id: str | None, fork_id: str | None
) -> str | None:
    """Pick the Omnigent conversation the watcher should write into."""
    if resume_id:
        return resume_id
    fresh: list[dict] = []
    for session in sessions:
        sid = session.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        if fork_id and sid == fork_id:
            continue
        created = session.get("created_at") or 0
        if isinstance(created, (int, float)) and created >= started - 2:
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


def discover_session(base: str, started: float, fork_id: str | None, resume_id: str | None) -> str | None:
    if resume_id:
        return resume_id
    status, payload = _request_json(method="GET", url=f"{base}/v1/sessions?limit=20&visibility=mine")
    if status != 200:
        return None
    return choose_session(sessions_from_payload(payload), started=started, resume_id=None, fork_id=fork_id)


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


def watch_omnigent(agent_id: str, plan: Plan, stop: threading.Event, started: float) -> None:
    """Append Partyline mail that arrives during this session into Omnigent."""
    cfg = cli._load_config()
    url = cli.resolve_url(cfg=cfg)
    base = _omnigent_base()
    seen = load_seen(agent_id)
    session_id = plan.resume_id
    while not stop.is_set():
        if session_id is None:
            session_id = discover_session(base, started, plan.fork_id, None)
        if session_id is not None:
            delivered = False
            for msg in fetch_unread(agent_id, url):
                mid = msg.get("id")
                if not isinstance(mid, int) or mid in seen or msg.get("agent_id") == agent_id:
                    continue
                if not message_is_new(msg, started):
                    seen.add(mid)
                    delivered = True
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
    if plan.fork_id:
        post_fork_notice(plan.harness, plan.fork_id)
    stop = threading.Event()
    started = time.time()
    if plan.watch:
        cfg = cli._load_config()
        agent_id = cli.resolve_agent_id(cfg=cfg)
        if agent_id:
            threading.Thread(
                target=watch_omnigent, args=(agent_id, plan, stop, started), daemon=True
            ).start()
        else:
            print("omni-pl: no .partyline/config.json agentId; Grok session watcher not started", file=sys.stderr)
    env = os.environ.copy()
    env.update(dict(plan.env_updates))
    try:
        while True:
            try:
                rc = subprocess.run(plan.command, env=env).returncode
                break
            except KeyboardInterrupt:
                continue
    finally:
        stop.set()
    return rc


if __name__ == "__main__":
    sys.exit(launch())
