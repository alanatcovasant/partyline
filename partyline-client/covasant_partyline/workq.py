"""Work-queue commands (phase 2, slice 1); stdlib only. See docs/PROPOSAL_Unified_Work_Queue_2026-11-03.md.

`queue sync` reads a repo's open issues with the caller's own `gh` login and pushes them to Partyline's
read-through mirror (client-pushed sync, option A). `work next` atomically claims the best ready issue in
scope (or resumes this seat's own); `--github` also leaves the visible claim marks in GitHub.
"""
import json
import sys
import urllib.parse

LIMIT = 1000
FIELDS = "number,title,url,state,labels,assignees,createdAt,updatedAt"


def _out(data):
    print(json.dumps(data, indent=2, default=str))


def _agent(args):
    from . import cli

    agent = args.agent_id or cli.resolve_agent_id()
    if not agent:
        raise SystemExit("no agent identity; run partyline init or pass --agent-id")
    return agent


def fetch_issues(gh, repo: str, limit: int = LIMIT) -> dict:
    """Open issues of one repo plus the acting login. `complete` is False when the list hit the limit, because
    then an open issue missing from it may simply not have been fetched, and must not be treated as closed."""
    rc, out, err = gh.run(["issue", "list", "-R", repo, "--state", "open", "--limit", str(limit), "--json", FIELDS])
    if rc != 0:
        raise RuntimeError(f"gh issue list failed: {err.strip() or out.strip()}")
    try:
        raw = json.loads(out)
    except ValueError:
        raise RuntimeError("gh issue list returned something that is not JSON")
    rc, who, _ = gh.run(["api", "user", "--jq", ".login"])
    issues = [{"number": i["number"], "title": i["title"], "url": i.get("url"), "state": i.get("state", "open").lower(),
               "labels": [x["name"] for x in i.get("labels") or []],
               "assignees": [x["login"] for x in i.get("assignees") or []],
               "created_at": i.get("createdAt"), "updated_at": i.get("updatedAt")} for i in raw]
    return {"issues": issues, "viewer": who.strip() if rc == 0 and who.strip() else None,
            "complete": len(raw) < limit}


def command(args):
    from . import cli

    scope, act = args.cmd, args.action
    if scope == "queue" and act == "list":
        q = {k: v for k, v in (("repo", args.repo), ("state", args.state)) if v}
        return _out(cli._request("GET", args.url, "/api/queue?" + urllib.parse.urlencode(q)))
    if scope == "queue" and act == "audit":
        q = {"repo": args.repo} if args.repo else {}
        return _out(cli._request("GET", args.url, "/api/queue/audit?" + urllib.parse.urlencode(q)))
    if scope == "queue" and act == "purge":
        return _out(cli._request("DELETE", args.url, "/api/queue?" + urllib.parse.urlencode({"repo": args.repo})))
    agent = _agent(args)
    if scope == "queue" and act == "sync":
        from . import ghclaim

        try:
            got = fetch_issues(ghclaim.Gh(), args.repo)
        except RuntimeError as exc:
            raise SystemExit(f"partyline: {exc}")
        body = {"agent_id": agent, "repo": args.repo, "viewer": got["viewer"], "issues": got["issues"],
                "complete": got["complete"] and not args.partial}
        res = cli._request("POST", args.url, "/api/queue/sync", body)
        if not got["complete"]:
            print(f"partyline: fetched {len(got['issues'])} issues (the limit); closure is not inferred", file=sys.stderr)
        return _out(res)
    if scope == "work" and act == "next":
        item = cli._request("POST", args.url, "/api/queue/next",
                            {"agent_id": agent, "queue": args.queue, "branch": args.branch})
        _out(item)
        if args.github or _gh_env():
            from . import cli as _cli, ghclaim
            from pathlib import Path

            _cli._gh_report(ghclaim.project_claim(ghclaim.Gh(), "issue:" + item["source_ref"], agent,
                                                  Path.cwd().name.lower(), args.branch))
        return
    if scope == "work" and act == "release":
        res = cli._request("POST", args.url, "/api/queue/release", {"agent_id": agent, "issue_ref": args.issue})
        _out(res)
        if args.github or _gh_env():
            from . import cli as _cli, ghclaim

            _cli._gh_report(ghclaim.release_claim(ghclaim.Gh(), "issue:" + args.issue, agent, args.note))


def _gh_env() -> bool:
    import os

    return os.getenv("PARTYLINE_GH_CLAIM", "").lower() in ("1", "true", "yes")


def add_parser(sub):
    q = sub.add_parser("queue", help="The work queue: a mirror of GitHub issues, in the order they should be worked")
    qa = q.add_subparsers(dest="action", required=True)
    p = qa.add_parser("sync", help="Push this repo's open issues (read with your gh login) into the queue")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--partial", action="store_true", help="Never infer that a missing issue was closed")
    p.add_argument("--agent-id")
    p = qa.add_parser("list", help="List queue entries in order")
    p.add_argument("--repo", help="owner/repo or just repo")
    p.add_argument("--state", help="ready|untriaged|claimed|external|closed")
    p = qa.add_parser("audit", help="Untriaged intake, external claims, stale labels, work closed while held")
    p.add_argument("--repo")
    p = qa.add_parser("purge", help="Delete a test repo's entries (zz/... only)")
    p.add_argument("--repo", required=True)
    for p in qa.choices.values():
        p.set_defaults(func=command)

    w = sub.add_parser("work", help="Pull and release work from the queue")
    wa = w.add_subparsers(dest="action", required=True)
    p = wa.add_parser("next", help="Claim the next ready issue in scope (or resume your own)")
    p.add_argument("--queue", help="A repo (Aura or covasant/Aura). Default: the repo you registered with")
    p.add_argument("--branch")
    p.add_argument("--github", action="store_true", help="Also leave the claim comment and label in GitHub")
    p.add_argument("--agent-id")
    p = wa.add_parser("release", help="Give an issue back to the queue")
    p.add_argument("--issue", required=True, help="owner/repo#N")
    p.add_argument("--note")
    p.add_argument("--github", action="store_true")
    p.add_argument("--agent-id")
    for p in wa.choices.values():
        p.set_defaults(func=command)
