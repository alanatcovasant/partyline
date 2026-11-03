"""Folder progress commands; stdlib only, shared across coding-agent vendors."""
import json
import re
import subprocess
import urllib.parse
from pathlib import Path
from uuid import uuid4


def command(args):
    from . import cli

    if args.action == "history":
        path = f"/api/progress/{urllib.parse.quote(args.task_id, safe='')}/history"
        print(json.dumps(cli._request("GET", args.url, path), indent=2))
        return
    if args.action == "handoff":
        return _handoff(args)
    if args.action == "purge":
        q = urllib.parse.urlencode({"folder": args.folder})
        print(json.dumps(cli._request("DELETE", args.url, "/api/progress?" + q), indent=2))
        return
    folder = (args.folder or Path(args.path).resolve().name).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", folder):
        raise SystemExit("folder must be a short folder label, such as aura_1")
    if args.action in {"show", "list"}:
        query = {"include_done": str(args.all).lower()}
        if args.action == "show" or args.folder:
            query["folder"] = folder
        if args.workstream:
            query["workstream"] = args.workstream
        print(json.dumps(cli._request("GET", args.url, "/api/progress?" + urllib.parse.urlencode(query)), indent=2))
        return
    agent = args.agent_id or cli.resolve_agent_id()
    if not agent:
        raise SystemExit("no agent identity; run partyline init or pass --agent-id")
    workdir = str(Path(args.path).resolve())
    try:
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=workdir,
                                capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        branch = None
    body = {"folder": folder, "agent_id": agent, "status": {
        "add": "queued", "start": "started", "note": "progress",
        "block": "blocked", "done": "done", "cancel": "cancelled",
    }[args.action], "summary": args.summary, "details": args.details,
        "workstream": args.workstream, "workdir": workdir, "branch": branch,
        "issue_ref": args.issue, "task_id": args.task, "next_action": args.next,
        "operation_id": args.operation_id or str(uuid4())}
    print(json.dumps(cli._request("POST", args.url, "/api/progress", body), indent=2))


def _handoff(args):
    """offer/accept/decline/takeover a task. accept and takeover move it into THIS folder."""
    from . import cli

    agent = args.agent_id or cli.resolve_agent_id()
    if not agent:
        raise SystemExit("no agent identity; run partyline init or pass --agent-id")
    body = {"agent_id": agent, "action": args.handoff_action, "to": args.to, "note": args.note,
            "next_action": args.next, "operation_id": args.operation_id or str(uuid4())}
    if args.handoff_action in {"accept", "takeover"}:
        body["folder"] = (args.folder or Path(args.path).resolve().name).strip().lower()
    path = f"/api/progress/{urllib.parse.quote(args.task, safe='')}/handoff"
    print(json.dumps(cli._request("POST", args.url, path, body), indent=2))


def add_parser(sub):
    parser = sub.add_parser("progress", help="Folder-based current tasks, backlog and progress history")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("add", "start", "note", "block", "done", "cancel", "show", "list", "history"):
        p = actions.add_parser(action)
        if action == "history":
            p.add_argument("task_id")
        else:
            p.add_argument("--folder", help="Folder label, e.g. aura_1 (default: current directory name)")
            p.add_argument("--path", default=".", help="Working directory")
            p.add_argument("--workstream")
            if action in {"show", "list"}:
                p.add_argument("--all", action="store_true", help="Include completed/cancelled tasks")
            else:
                p.add_argument("summary")
                p.add_argument("--details")
                p.add_argument("--task", help="Task ID; otherwise updates target the folder's current task")
                p.add_argument("--agent-id")
                p.add_argument("--issue")
                p.add_argument("--next", help="Next action: what a resuming agent should do first")
                p.add_argument("--operation-id", help="UUID to reuse when retrying the same update")
        p.set_defaults(func=command)
    pg = actions.add_parser("purge", help="Delete a zz- test folder's tasks (refused for real folders)")
    pg.add_argument("--folder", required=True)
    pg.set_defaults(func=command)
    ho = actions.add_parser("handoff", help="Offer, accept, decline or take over a task (ownership change)")
    ho_actions = ho.add_subparsers(dest="handoff_action", required=True)
    for name, helptext in (("offer", "Offer your task to an agent (or * for anyone)"),
                           ("accept", "Accept an offered task into this folder"),
                           ("decline", "Decline an offer to you, or withdraw your own"),
                           ("takeover", "Take over a task whose owner is stale (needs --note)")):
        p = ho_actions.add_parser(name, help=helptext)
        p.add_argument("--task", required=True, help="Task ID")
        p.add_argument("--agent-id")
        p.add_argument("--path", default=".", help="Working directory (accept/takeover use its folder)")
        p.add_argument("--folder", help="Folder label for accept/takeover (default: current directory name)")
        p.add_argument("--note", help="Why; required for takeover")
        p.add_argument("--next", help="Next action for whoever works it next")
        p.add_argument("--operation-id")
        if name == "offer":
            p.add_argument("--to", required=True, help="Agent id, or * for anyone")
        else:
            p.set_defaults(to=None)
        p.set_defaults(func=command)
