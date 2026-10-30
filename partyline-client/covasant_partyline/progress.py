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
        "issue_ref": args.issue, "task_id": args.task, "operation_id": args.operation_id or str(uuid4())}
    print(json.dumps(cli._request("POST", args.url, "/api/progress", body), indent=2))


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
                p.add_argument("--operation-id", help="UUID to reuse when retrying the same update")
        p.set_defaults(func=command)
