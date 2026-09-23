#!/usr/bin/env python3
"""partyline — a tiny CLI for the Covasant Partyline coordination bus.

Zero third-party dependencies (stdlib urllib only), so it runs anywhere Python 3.10+
does. It talks to the Partyline app's REST API.

Connecting:
  --url URL          app base URL (or set PARTYLINE_URL)
  PARTYLINE_TOKEN    Databricks OAuth bearer token for the deployed app; mint one with
                       databricks auth token -p <profile>
                     The ./partyline.sh / partyline.cmd wrapper fills both in for you.

Commands:
  register   announce/refresh this agent's presence
  post       post a message (status/question/claim/release/blocked/done/ack/…)
  unread     messages since your checkpoint (--mine = addressed to you only)
  ack        advance your read checkpoint
  claim-issue  lease a GH issue/PR plus the directories it touches (preferred)
  claim      lease one target: an absolute directory, or issue:owner/repo#N
  release    release a claim you hold
  agents     list active agents (with their last status)
  claims     list active resource claims
  search     field-filtered search over live + archived history
  conversation  read a full thread by conversation_id
  watch      stream live messages; --exit-on-mention makes it a wake-up trigger

Examples:
  partyline register alan_web_1 --owner alan --repo web --branch main --workdir . \\
      --harness cursor --model composer-2
  partyline set-runtime alan_web_1 --model gpt-5.4
  partyline post alan_web_1 --type status --payload "starting #123"
  partyline post alan_web_1 --type question --recipient sam_api_1 --payload "API shape?"
  partyline claim-issue alan_web_1 web issue:covasant/web#123 --path .
  partyline unread alan_web_1 --mine
  partyline agents
  partyline search --issue 123 --repo web
  partyline search -q "pool fix" --type done
  partyline conversation 3f2a1b4c-...-...
  partyline watch --exit-on-mention alan_web_1 --timeout 3600
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .targets import normalize_target

DEFAULT_URL = os.getenv("PARTYLINE_URL", "http://localhost:8000")

# No network call may block forever. Requests get a hard timeout; the watch stream uses a
# read timeout (keep-alives arrive ~every 15s, so silence past this means a dead socket).
_HTTP_TIMEOUT = float(os.getenv("PARTYLINE_HTTP_TIMEOUT", "30"))
_WATCH_READ_TIMEOUT = float(os.getenv("PARTYLINE_WATCH_READ_TIMEOUT", "60"))


def _explain_http_error(code: int, raw: str) -> str:
    """Turn a server refusal into something the caller can act on in one step.

    An unknown recipient is the refusal agents will hit most, and it always carries a fix
    (the id they probably meant), so it gets prose instead of a wall of JSON.
    """
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        return f"HTTP {code}: {raw}"

    if isinstance(detail, dict) and detail.get("error") == "unknown_recipient":
        lines = [detail.get("message") or f"unknown recipient {detail.get('recipient')!r}"]
        if detail.get("suggestions"):
            lines.append("  registered ids that look close: " + ", ".join(detail["suggestions"]))
        lines.append("  see who is on the line: partyline agents --all")
        return "\n".join(lines)
    return f"HTTP {code}: {raw}"


def _request(method: str, base: str, path: str, body: dict | None = None) -> object:
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = os.getenv("PARTYLINE_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        sys.exit(f"partyline: {_explain_http_error(e.code, raw)}")
    except (urllib.error.URLError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        sys.exit(f"Connection failed: {reason}")


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_register(a: argparse.Namespace) -> None:
    _print(
        _request(
            "POST",
            a.url,
            "/api/agents",
            {
                "agent_id": a.agent_id,
                "owner": a.owner,
                "repo": a.repo,
                "branch": a.branch,
                "display_name": a.display_name,
                "harness": a.harness,
                "model": a.model,
                "workdirs": [normalize_target(d) for d in (a.workdir or [])],
            },
        )
    )


def cmd_set_runtime(a: argparse.Namespace) -> None:
    body: dict = {}
    if a.harness is not None:
        body["harness"] = a.harness if a.harness != "" else None
    if a.model is not None:
        body["model"] = a.model if a.model != "" else None
    if a.clear_harness:
        body["harness"] = None
    if a.clear_model:
        body["model"] = None
    if not body:
        raise SystemExit("set-runtime: pass --harness/--model and/or --clear-harness/--clear-model")
    _print(
        _request(
            "PATCH",
            a.url,
            f"/api/agents/{urllib.parse.quote(a.agent_id)}/runtime",
            body,
        )
    )


def cmd_post(a: argparse.Namespace) -> None:
    result = _request(
        "POST",
        a.url,
        "/api/messages",
        {
            "agent_id": a.agent_id,
            "message_type": a.type,
            "recipient": a.recipient,
            "repo": a.repo,
            "branch": a.branch,
            "resource_path": a.resource,
            "issue_ref": a.issue,
            "summary": a.summary,
            "payload": a.payload,
            "metadata": json.loads(a.metadata) if a.metadata else {},
            "mentions": a.mention or [],
            "reply_to_id": a.reply_to,
            "allow_unregistered_recipient": a.allow_unregistered_recipient,
            "tag_everyone": a.tag_everyone,
        },
    )
    _print(result)
    # On stderr, so it is visible to a human and to an agent reading the tool output, but
    # never corrupts the JSON on stdout that scripts parse.
    for warning in (result or {}).get("delivery_warnings") or []:
        print(f"partyline: warning: {warning}", file=sys.stderr)


def cmd_unread(a: argparse.Namespace) -> None:
    q = "?only_for_me=true" if a.mine else ""
    _print(_request("GET", a.url, f"/api/agents/{urllib.parse.quote(a.agent_id)}/unread{q}"))


def cmd_ack(a: argparse.Namespace) -> None:
    _print(
        _request(
            "POST",
            a.url,
            "/api/checkpoints",
            {
                "agent_id": a.agent_id,
                "last_read_message_id": a.message_id,
            },
        )
    )


def cmd_claim(a: argparse.Namespace) -> None:
    _print(
        _request(
            "POST",
            a.url,
            "/api/claims",
            {
                "agent_id": a.agent_id,
                "repo": a.repo,
                "resource_path": normalize_target(a.resource_path),
                "branch": a.branch,
                "resource_type": a.resource_type,
                "claim_scope": a.scope,
                "reason": a.reason,
                "ttl_seconds": a.ttl,
            },
        )
    )


def cmd_claim_issue(a: argparse.Namespace) -> None:
    _print(
        _request(
            "POST",
            a.url,
            "/api/claims/work-item",
            {
                "agent_id": a.agent_id,
                "repo": a.repo,
                "work_item": normalize_target(a.work_item),
                "paths": [normalize_target(p) for p in (a.path or [])],
                "branch": a.branch,
                "claim_scope": a.scope,
                "reason": a.reason,
                "ttl_seconds": a.ttl,
            },
        )
    )


def cmd_release(a: argparse.Namespace) -> None:
    _print(
        _request(
            "POST",
            a.url,
            "/api/claims/release",
            {
                "agent_id": a.agent_id,
                "repo": a.repo,
                "resource_path": normalize_target(a.resource_path),
                "branch": a.branch,
            },
        )
    )


def cmd_agents(a: argparse.Namespace) -> None:
    _print(_request("GET", a.url, "/api/agents" + ("?include_stale=true" if a.all else "")))


def cmd_claims(a: argparse.Namespace) -> None:
    _print(_request("GET", a.url, "/api/claims"))


def cmd_search(a: argparse.Namespace) -> None:
    params = {
        "q": a.text,
        "message_type": a.type,
        "agent_id": a.agent,
        "author": a.author,
        "recipient": a.recipient,
        "mention": a.mention,
        "repo": a.repo,
        "branch": a.branch,
        "resource_path": a.resource,
        "issue_ref": a.issue,
        "conversation_id": a.conversation,
        "since": a.since,
        "until": a.until,
        "limit": a.limit,
    }
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    _print(_request("GET", a.url, f"/api/messages/query?{qs}"))


def cmd_conversation(a: argparse.Namespace) -> None:
    qs = urllib.parse.urlencode({"limit": a.limit})
    _print(_request("GET", a.url, f"/api/conversations/{urllib.parse.quote(a.conversation_id)}?{qs}"))


def cmd_watch(a: argparse.Namespace) -> None:
    """Stream live messages. With --exit-on-mention, exit 0 as soon as a message
    addressed to that agent (recipient or mention) arrives — this turns the stream
    into a wake-up trigger: run it as a background task and react when it exits.
    Exits 2 on --timeout expiry or stream disconnect (no match). With --heartbeat N,
    a keep-alive line goes to stderr every N seconds (wake mode only) so monitors
    with idle timeouts don't reap the silent watcher mid-window.

    The read uses a socket timeout so a silently-dead connection never hangs: on read
    timeout / disconnect the loop reconnects (bounded by the overall --timeout) instead
    of blocking forever."""
    import http.client
    import socket
    import time

    url = a.url.rstrip("/") + "/api/stream"
    deadline = time.monotonic() + a.timeout if a.timeout else None
    last_beat = time.monotonic()
    if not a.exit_on_mention:
        print(f"watching {url} (Ctrl-C to stop)")

    try:
        while deadline is None or time.monotonic() < deadline:
            # Re-mint the token on every (re)connect so the stream survives Databricks
            # OAuth expiry (~1h). Capturing it once — as this used to — meant that once
            # the token expired, every reconnect 401'd forever and the watcher went deaf.
            headers = {"Accept": "text/event-stream"}
            token = resolve_stream_token(a.url, getattr(a, "profile", None), os.getenv("PARTYLINE_TOKEN"))
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=_WATCH_READ_TIMEOUT) as resp:
                    for raw in resp:
                        now = time.monotonic()
                        if deadline and now > deadline:
                            break
                        # Heartbeat for harness monitors with idle timeouts (Qwen Code's
                        # monitor reaps a silent watcher at <=10 min): a beat line every
                        # --heartbeat seconds resets the idle timer. It goes to STDOUT,
                        # not stderr — measured on Windows: the monitor's idle timer and
                        # event counter ignore stderr entirely. The match contract keeps:
                        # the addressed message's JSON is still the FINAL stdout line
                        # before exit 0; beats are the recognizably-prefixed noise before
                        # it. Server keep-alives (~15s) keep this loop turning, so the
                        # beat fires on schedule.
                        if a.heartbeat and a.exit_on_mention and now - last_beat >= a.heartbeat:
                            print("partyline-watch: alive", flush=True)
                            last_beat = now
                        line = raw.decode(errors="replace").rstrip()
                        if not line.startswith("data:"):
                            continue  # keep-alives arrive ~every 15s
                        data = line[5:].strip()
                        if not a.exit_on_mention:
                            print(data, flush=True)
                            continue
                        try:
                            evt = json.loads(data)
                        except ValueError:
                            continue
                        me = a.exit_on_mention
                        if evt.get("recipient") == me or me in (evt.get("mentions") or []):
                            print(data, flush=True)
                            return  # exit 0: message for me — caller wakes and calls unread
                # Stream closed cleanly (server side) — reconnect until the deadline.
            except (TimeoutError, socket.timeout):
                continue  # idle/dead socket — reconnect (never hang)
            except urllib.error.HTTPError as e:
                # 401/403 = expired/invalid token — the classic ~1h drop. The next loop
                # iteration re-mints, so this self-heals; surface it clearly meanwhile.
                if not a.exit_on_mention:
                    hint = " — token expired/invalid, re-minting" if e.code in (401, 403) else ""
                    print(f"reconnecting after HTTP {e.code}{hint}", file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                # Any transient connect/stream failure — DNS blip, reset, half-open socket
                # (ConnectionResetError/IncompleteRead are OSError/HTTPException, not
                # URLError), the Databricks Apps proxy dropping a long stream, etc. Reconnect
                # until the deadline instead of exiting 2, so a single armed watcher survives
                # transient drops rather than dying on the first one.
                if not a.exit_on_mention:
                    print(f"reconnecting ({getattr(e, 'reason', e)})", file=sys.stderr, flush=True)
                time.sleep(2)  # brief backoff on transient network error
                continue
    except KeyboardInterrupt:
        pass
    if a.exit_on_mention:
        sys.exit(2)  # stream ended or timed out without a match


_EPILOG = """\
environment:
  PARTYLINE_URL                 base URL (overridden by --url)
  PARTYLINE_TOKEN               Bearer token for the deployed app (Databricks OAuth)
  PARTYLINE_HTTP_TIMEOUT        per-request timeout, seconds (default 30)
  PARTYLINE_WATCH_READ_TIMEOUT  watch socket read timeout, seconds (default 60)

tip: prefer the ./partyline.sh (or partyline.cmd) wrapper — it reads PARTYLINE_URL from
     .partyline/config.json and mints PARTYLINE_TOKEN via the databricks CLI, so you can
     just run:  ./partyline.sh agents
"""


def _load_config() -> dict:
    """Find .partyline/config.json in the cwd or any ancestor; return {} if none.

    Lets the CLI be self-sufficient from inside any repo that has been onboarded — no
    need to export PARTYLINE_URL or wrap it in a shell script.
    """
    d = os.getcwd()
    while True:
        cfg = os.path.join(d, ".partyline", "config.json")
        if os.path.isfile(cfg):
            try:
                with open(cfg, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                return {}
        parent = os.path.dirname(d)
        if parent == d:
            return {}
        d = parent


# --------------------------------------------------------------------------- #
# Shared identity resolution — one home, five consumers
# --------------------------------------------------------------------------- #
# Pure: no network, no raise, no argparse. `cfg` defaults to _load_config() but is
# injectable, so tests never touch the filesystem. Every consumer delegates here
# (cli.main, channel.resolve_settings, outbox.resolve_agent_id, check_unread, hook):
# five sites resolving identity five ways is exactly how the peek hook drifted back
# onto the pre-#1545 env-only contract and went silently deaf on any clone that
# carries its identity in .partyline/config.json.

_LOCAL_URL_PREFIXES = ("http://localhost", "http://127.")


def is_local_url(url: str) -> bool:
    """True for an app on this machine — it needs no Databricks token or profile."""
    return url.startswith(_LOCAL_URL_PREFIXES)


def resolve_agent_id(cli_arg: str | None = None, cfg: dict | None = None) -> str | None:
    """flag → PARTYLINE_AGENT_ID → config agentId/agent_id → None.

    Env deliberately shadows config (#1545): config is the per-clone default, env is the
    explicit per-session override.
    """
    if cli_arg:
        return cli_arg
    env = os.getenv("PARTYLINE_AGENT_ID")
    if env:
        return env
    cfg = _load_config() if cfg is None else cfg
    return cfg.get("agentId") or cfg.get("agent_id")


def resolve_url(cli_arg: str | None = None, cfg: dict | None = None) -> str:
    """flag → PARTYLINE_URL → config url → localhost default.

    Trailing slash stripped: every caller builds `url + "/api/..."`, and a config value
    with a slash would otherwise produce a double-slash path.
    """
    cfg = _load_config() if cfg is None else cfg
    url = cli_arg or os.getenv("PARTYLINE_URL") or cfg.get("url") or "http://localhost:8000"
    return url.rstrip("/")


def resolve_profile(cli_arg: str | None = None, *, url: str, cfg: dict | None = None) -> str | None:
    """None for a local url; else flag → PARTYLINE_PROFILE → config
    databricksProfile/profile → DATABRICKS_CONFIG_PROFILE → dev-eastus2.

    The app-specific config profile outranks the *ambient* DATABRICKS_CONFIG_PROFILE, which
    often targets another workspace and would 401. PARTYLINE_PROFILE is partyline-specific
    intent (the hooks' documented knob), so it ranks alongside the flag instead.
    """
    if is_local_url(url):
        return None
    cfg = _load_config() if cfg is None else cfg
    return (
        cli_arg
        or os.getenv("PARTYLINE_PROFILE")
        or cfg.get("databricksProfile")
        or cfg.get("profile")
        or os.getenv("DATABRICKS_CONFIG_PROFILE")
        or "dev-eastus2"
    )


def resolve_teams_webhook(cli_arg: str | None = None, cfg: dict | None = None) -> str | None:
    """flag → TEAMS_WEBHOOK_URL → config teamsWebhookUrl → None.

    The URL's query string is the signature, so it lives only in the gitignored
    per-clone config — never in a tracked file.
    """
    if cli_arg:
        return cli_arg
    env = os.getenv("TEAMS_WEBHOOK_URL")
    if env:
        return env
    cfg = _load_config() if cfg is None else cfg
    return cfg.get("teamsWebhookUrl")


def _mint_token(profile: str) -> str | None:
    """Best-effort Databricks OAuth token via the `databricks` CLI (stdlib subprocess only).

    Returns None if the CLI is missing or auth fails — the caller then proceeds tokenless
    (fine for a local/unauthenticated app; the deployed app will answer 401, which is a
    clearer signal than a crash).
    """
    try:
        out = subprocess.run(
            ["databricks", "auth", "token", "-p", profile],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        return json.loads(out).get("access_token")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def resolve_stream_token(url: str, profile: str | None, fallback: str | None = None) -> str | None:
    """A *fresh* bearer token for a (re)connect on a long-lived stream.

    Databricks OAuth tokens live ~1h (and the CLI hands out a *cached* token whose
    remaining life may be far less), so a token captured once and reused on every
    reconnect is why SSE connections silently died after "an hour or so, maybe less":
    once it expired, the Apps auth proxy 401'd every reconnect forever. Call this once
    per (re)connect instead:

      - prefer re-minting from the CLI profile (``databricks auth token`` auto-refreshes
        from its cache, so this is cheap and always current);
      - fall back to PARTYLINE_TOKEN / the initial token when there is no profile or the
        CLI mint fails (e.g. no databricks CLI on the box);
      - stay tokenless for a localhost app (unauthenticated dev server).
    """
    if url.startswith("http://localhost"):
        return None
    if profile:
        minted = _mint_token(profile)
        if minted:
            return minted
    return os.getenv("PARTYLINE_TOKEN") or fallback


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="partyline",
        description=__doc__,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--url",
        default=None,
        help=f"App base URL (default: PARTYLINE_URL, else .partyline/config.json, else {DEFAULT_URL})",
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Databricks CLI profile for auto-minting the auth token when "
        "PARTYLINE_TOKEN is unset (default: .partyline/config.json, else "
        "DATABRICKS_CONFIG_PROFILE, else dev-eastus2)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("register", help="Register/refresh an agent")
    sp.add_argument("agent_id")
    sp.add_argument("--owner", required=True)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--branch")
    sp.add_argument("--display-name", dest="display_name")
    sp.add_argument(
        "--harness",
        help="Optional runtime/harness id (e.g. cursor, claude-code). Omit when unknown.",
    )
    sp.add_argument(
        "--model",
        help="Optional model id the session is running. Omit when the harness cannot report it.",
    )
    sp.add_argument(
        "--workdir",
        action="append",
        help="Directory this session owns (repeatable; '.' = here). Claimed "
        "on register; an overlap reports the holder but still registers.",
    )
    sp.set_defaults(func=cmd_register)

    sp = sub.add_parser(
        "set-runtime",
        help="Update harness/model mid-session (without a full re-register)",
    )
    sp.add_argument("agent_id")
    sp.add_argument("--harness", help="Set harness (pass empty string to clear)")
    sp.add_argument("--model", help="Set model (pass empty string to clear)")
    sp.add_argument("--clear-harness", action="store_true", help="Clear stored harness")
    sp.add_argument("--clear-model", action="store_true", help="Clear stored model")
    sp.set_defaults(func=cmd_set_runtime)

    sp = sub.add_parser("post", help="Post a message")
    sp.add_argument("agent_id")
    sp.add_argument(
        "--type",
        default="status",
        choices=["status", "question", "claim", "release", "blocked", "done", "ack", "escalation", "summary"],
    )
    sp.add_argument("--recipient", help="Primary addressee agent id ('To:')")
    sp.add_argument("--repo")
    sp.add_argument("--branch")
    sp.add_argument("--resource", help="resource_path")
    sp.add_argument("--issue", help="issue_ref (GH issue/PR)")
    sp.add_argument("--summary", help="Concise one-line 'what' (read by default; put detail in --payload)")
    sp.add_argument("--payload", help="Full detail (fetched on demand)")
    sp.add_argument("--metadata", help="JSON string")
    sp.add_argument("--mention", action="append", help="Mentioned agent id (repeatable)")
    sp.add_argument(
        "--everyone", action="store_true", dest="tag_everyone",
        help="Mention every non-stale agent except the sender",
    )
    sp.add_argument("--reply-to", type=int, dest="reply_to")
    sp.add_argument(
        "--allow-unregistered-recipient",
        action="store_true",
        dest="allow_unregistered_recipient",
        help="Permit a --recipient that has never registered. Off by default: "
        "an unknown id is a typo, and a typo'd handoff is undeliverable "
        "forever. Use only for a session that hasn't started yet.",
    )
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("unread", help="Get messages since an agent's checkpoint")
    sp.add_argument("agent_id")
    sp.add_argument("--mine", action="store_true", help="Only messages addressed to me (recipient or mention)")
    sp.set_defaults(func=cmd_unread)

    sp = sub.add_parser("ack", help="Advance an agent's checkpoint")
    sp.add_argument("agent_id")
    sp.add_argument("message_id", type=int)
    sp.set_defaults(func=cmd_ack)

    sp = sub.add_parser("claim-issue", help="Claim a GH issue/PR and the directories it touches")
    sp.add_argument("agent_id")
    sp.add_argument("repo")
    sp.add_argument("work_item", help="issue:owner/repo#123 or pr:owner/repo#123")
    sp.add_argument("--path", action="append", help="Directory this work touches (repeatable; '.' = here)")
    sp.add_argument("--branch")
    sp.add_argument("--scope", default="exclusive", choices=["exclusive", "shared_read", "intent"])
    sp.add_argument("--reason")
    sp.add_argument("--ttl", type=int, help="Lease seconds")
    sp.set_defaults(func=cmd_claim_issue)

    sp = sub.add_parser("claim", help="Claim one target (dir, work item, or source)")
    sp.add_argument("agent_id")
    sp.add_argument("repo")
    sp.add_argument(
        "resource_path",
        help="Absolute directory ('.' = here), issue:owner/repo#N, or "
        "src:owner/repo/path — the last claims the source in EVERY "
        "clone, which a path claim cannot do",
    )
    sp.add_argument("--branch")
    sp.add_argument(
        "--resource-type", dest="resource_type", choices=["issue", "pr", "source", "directory", "workdir", "file"]
    )
    sp.add_argument("--scope", default="exclusive", choices=["exclusive", "shared_read", "intent"])
    sp.add_argument("--reason")
    sp.add_argument("--ttl", type=int, help="Lease seconds")
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("release", help="Release a resource claim")
    sp.add_argument("agent_id")
    sp.add_argument("repo")
    sp.add_argument("resource_path")
    sp.add_argument("--branch")
    sp.set_defaults(func=cmd_release)

    sp = sub.add_parser("agents", help="List agents with presence (live/idle/stale)")
    sp.add_argument(
        "--all",
        action="store_true",
        help="Include stale agents. Nothing is ever deleted, so this answers "
        "'does this id exist at all' — which the trimmed list cannot.",
    )
    sp.set_defaults(func=cmd_agents)
    sub.add_parser("claims", help="List active claims").set_defaults(func=cmd_claims)

    sp = sub.add_parser("search", help="Field-filtered search over live + archived history")
    sp.add_argument("--text", "-q", dest="text", help="Full-text query (summary+payload)")
    sp.add_argument("--type", dest="type", help="message_type")
    sp.add_argument("--agent", help="agent_id")
    sp.add_argument("--author")
    sp.add_argument("--recipient")
    sp.add_argument("--mention")
    sp.add_argument("--repo")
    sp.add_argument("--branch")
    sp.add_argument("--resource", help="resource_path")
    sp.add_argument("--issue", help="issue_ref")
    sp.add_argument("--conversation", help="conversation_id")
    sp.add_argument("--since", help="event_time lower bound (ISO-8601)")
    sp.add_argument("--until", help="event_time upper bound (ISO-8601)")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("conversation", help="Read a full thread by conversation_id")
    sp.add_argument("conversation_id")
    sp.add_argument("--limit", type=int, default=500)
    sp.set_defaults(func=cmd_conversation)

    sp = sub.add_parser("watch", help="Stream live messages (SSE)")
    sp.add_argument(
        "--exit-on-mention",
        metavar="AGENT_ID",
        help="Exit 0 when a message addressed to AGENT_ID arrives "
        "(recipient or mention) — use as a background wake-up trigger",
    )
    sp.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="With --exit-on-mention: give up (exit 2) after this long"
    )
    sp.add_argument(
        "--heartbeat",
        type=int,
        default=0,
        metavar="SECONDS",
        help="With --exit-on-mention: print a keep-alive line to stderr every N seconds "
        "so harness monitors with idle timeouts (e.g. Qwen Code's, max 10 min) don't "
        "reap the silent watcher — lets one arm last the full --timeout instead of "
        "being re-armed every few minutes",
    )
    sp.set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = _load_config()

    # URL: --url > PARTYLINE_URL env > config > localhost default.
    args.url = args.url or os.getenv("PARTYLINE_URL") or cfg.get("url") or "http://localhost:8000"

    # Token: reuse PARTYLINE_TOKEN if set; otherwise mint one for a remote app. The
    # app-specific config profile wins over the ambient DATABRICKS_CONFIG_PROFILE (which
    # often targets another workspace and would 401). Local apps need no token.
    is_local = args.url.startswith(("http://localhost", "http://127."))
    # Resolve the profile for remote apps even when PARTYLINE_TOKEN is already set: it's
    # what lets the long-lived `watch` stream re-mint a fresh token on each reconnect
    # (resolve_stream_token) instead of dying when the initial token expires (~1h).
    args.profile = (
        None
        if is_local
        else (args.profile or cfg.get("databricksProfile") or os.getenv("DATABRICKS_CONFIG_PROFILE") or "dev-eastus2")
    )
    if not os.getenv("PARTYLINE_TOKEN") and args.profile:
        token = _mint_token(args.profile)
        if token:
            os.environ["PARTYLINE_TOKEN"] = token

    args.func(args)


if __name__ == "__main__":
    main()
