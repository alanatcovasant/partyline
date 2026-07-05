#!/usr/bin/env python3
"""partyline — a tiny CLI for the Covasant Partyline coordination bus.

Zero third-party dependencies (stdlib urllib) so it runs anywhere Python 3.10+ does.
Talks to the app's REST API. Point it at your app with --url or PARTYLINE_URL.

Examples:
  partyline register alan_aura_3 --owner alan --repo aura --branch alan_develop
  partyline post alan_aura_3 --type claim --repo aura --resource src/x.py
  partyline claim alan_aura_3 aura src/x.py --branch alan_develop --ttl 600
  partyline unread alan_aura_3
  partyline agents
  partyline watch
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = os.getenv("PARTYLINE_URL", "http://localhost:8000")


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
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"Connection failed: {e.reason}")


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_register(a: argparse.Namespace) -> None:
    _print(_request("POST", a.url, "/api/agents", {
        "agent_id": a.agent_id, "owner": a.owner, "repo": a.repo,
        "branch": a.branch, "display_name": a.display_name,
    }))


def cmd_post(a: argparse.Namespace) -> None:
    _print(_request("POST", a.url, "/api/messages", {
        "agent_id": a.agent_id, "message_type": a.type, "recipient": a.recipient,
        "repo": a.repo, "branch": a.branch, "resource_path": a.resource,
        "issue_ref": a.issue, "payload": a.payload,
        "metadata": json.loads(a.metadata) if a.metadata else {},
        "mentions": a.mention or [], "reply_to_id": a.reply_to,
    }))


def cmd_unread(a: argparse.Namespace) -> None:
    q = "?only_for_me=true" if a.mine else ""
    _print(_request("GET", a.url, f"/api/agents/{urllib.parse.quote(a.agent_id)}/unread{q}"))


def cmd_ack(a: argparse.Namespace) -> None:
    _print(_request("POST", a.url, "/api/checkpoints", {
        "agent_id": a.agent_id, "last_read_message_id": a.message_id,
    }))


def cmd_claim(a: argparse.Namespace) -> None:
    _print(_request("POST", a.url, "/api/claims", {
        "agent_id": a.agent_id, "repo": a.repo, "resource_path": a.resource_path,
        "branch": a.branch, "resource_type": a.resource_type,
        "claim_scope": a.scope, "reason": a.reason, "ttl_seconds": a.ttl,
    }))


def cmd_release(a: argparse.Namespace) -> None:
    _print(_request("POST", a.url, "/api/claims/release", {
        "agent_id": a.agent_id, "repo": a.repo,
        "resource_path": a.resource_path, "branch": a.branch,
    }))


def cmd_agents(a: argparse.Namespace) -> None:
    _print(_request("GET", a.url, "/api/agents"))


def cmd_claims(a: argparse.Namespace) -> None:
    _print(_request("GET", a.url, "/api/claims"))


def cmd_watch(a: argparse.Namespace) -> None:
    url = a.url.rstrip("/") + "/api/stream"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"watching {url} (Ctrl-C to stop)")
            for raw in resp:
                line = raw.decode(errors="replace").rstrip()
                if line.startswith("data:"):
                    print(line[5:].strip())
    except KeyboardInterrupt:
        pass
    except urllib.error.URLError as e:
        sys.exit(f"Connection failed: {e.reason}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="partyline", description=__doc__.splitlines()[0])
    p.add_argument("--url", default=DEFAULT_URL, help=f"Base URL (default {DEFAULT_URL})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("register", help="Register/refresh an agent")
    sp.add_argument("agent_id")
    sp.add_argument("--owner", required=True)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--branch")
    sp.add_argument("--display-name", dest="display_name")
    sp.set_defaults(func=cmd_register)

    sp = sub.add_parser("post", help="Post a message")
    sp.add_argument("agent_id")
    sp.add_argument("--type", default="status", choices=[
        "status", "question", "claim", "release", "blocked", "done", "ack",
        "escalation", "summary"])
    sp.add_argument("--recipient", help="Primary addressee agent id ('To:')")
    sp.add_argument("--repo")
    sp.add_argument("--branch")
    sp.add_argument("--resource", help="resource_path")
    sp.add_argument("--issue", help="issue_ref (GH issue/PR)")
    sp.add_argument("--payload", help="Freeform text content")
    sp.add_argument("--metadata", help="JSON string")
    sp.add_argument("--mention", action="append", help="Mentioned agent id (repeatable)")
    sp.add_argument("--reply-to", type=int, dest="reply_to")
    sp.set_defaults(func=cmd_post)

    sp = sub.add_parser("unread", help="Get messages since an agent's checkpoint")
    sp.add_argument("agent_id")
    sp.add_argument("--mine", action="store_true",
                    help="Only messages addressed to me (recipient or mention)")
    sp.set_defaults(func=cmd_unread)

    sp = sub.add_parser("ack", help="Advance an agent's checkpoint")
    sp.add_argument("agent_id")
    sp.add_argument("message_id", type=int)
    sp.set_defaults(func=cmd_ack)

    sp = sub.add_parser("claim", help="Claim a resource")
    sp.add_argument("agent_id")
    sp.add_argument("repo")
    sp.add_argument("resource_path")
    sp.add_argument("--branch")
    sp.add_argument("--resource-type", dest="resource_type",
                    choices=["file", "directory", "module", "service", "migration"])
    sp.add_argument("--scope", default="exclusive",
                    choices=["exclusive", "shared_read", "intent"])
    sp.add_argument("--reason")
    sp.add_argument("--ttl", type=int, help="Lease seconds")
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("release", help="Release a resource claim")
    sp.add_argument("agent_id")
    sp.add_argument("repo")
    sp.add_argument("resource_path")
    sp.add_argument("--branch")
    sp.set_defaults(func=cmd_release)

    sub.add_parser("agents", help="List active agents").set_defaults(func=cmd_agents)
    sub.add_parser("claims", help="List active claims").set_defaults(func=cmd_claims)
    sub.add_parser("watch", help="Stream live messages (SSE)").set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
