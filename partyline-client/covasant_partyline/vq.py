"""Validation-queue commands (prototype); stdlib only. See docs/PROPOSAL_Validation_Queue_2026-11-02.md.

`record` stores a result the agent TYPED IN (self_reported). `run` executes the item's own
script and records the captured exit code (executed), the only evidence that can validate.
"""
import json
import subprocess
import sys
import urllib.parse
from uuid import uuid4

TAIL = 3500


def _agent(args):
    from . import cli

    agent = args.agent_id or cli.resolve_agent_id()
    if not agent:
        raise SystemExit("no agent identity; run partyline init or pass --agent-id")
    return agent


def _out(data):
    print(json.dumps(data, indent=2, default=str))


def run_script(script: str, timeout: int) -> tuple[int, str]:
    """Run the recorded validation command; map anything but 0/1/2 to INCONCLUSIVE (2)."""
    try:
        proc = subprocess.run(script, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 2, f"timed out after {timeout}s"
    text = (proc.stdout or "") + (proc.stderr or "")
    return (proc.returncode if proc.returncode in (0, 1, 2) else 2), text[-TAIL:]


def command(args):
    from . import cli

    act = args.action
    if act == "list":
        q = {k: v for k, v in (("target", args.target), ("state", args.state)) if v}
        return _out(cli._request("GET", args.url, "/api/vq/items?" + urllib.parse.urlencode(q)))
    if act == "show":
        q = urllib.parse.urlencode({"issue_ref": args.issue, "target": args.target})
        return _out(cli._request("GET", args.url, "/api/vq/show?" + q))
    if act == "purge":
        q = urllib.parse.urlencode({"target": args.target})
        return _out(cli._request("DELETE", args.url, "/api/vq/items?" + q))
    agent = _agent(args)
    if act == "enqueue":
        body = {"issue_ref": args.issue, "target": args.target, "agent_id": agent,
                "script": args.script, "proc": args.proc, "pr": args.pr}
        return _out(cli._request("POST", args.url, "/api/vq/items", body))
    if act == "next":
        item = cli._request("POST", args.url, "/api/vq/claim",
                            {"agent_id": agent, "target": args.target, "issue_ref": args.issue})
        return _out(item)
    if act == "record":
        body = {"issue_ref": args.issue, "target": args.target, "agent_id": agent, "exit_code": args.exit_code,
                "executed": False, "commit": args.commit, "output_tail": args.output,
                "operation_id": args.operation_id or str(uuid4())}
        return _out(cli._request("POST", args.url, "/api/vq/runs", body))
    if act == "run":
        q = urllib.parse.urlencode({"issue_ref": args.issue, "target": args.target})
        item = cli._request("GET", args.url, "/api/vq/show?" + q)
        if not item.get("script"):
            raise SystemExit("item has no script; enqueue it with --script or use `record`")
        code, tail = run_script(item["script"], args.timeout)
        sys.stderr.write(f"vq run: exit {code}\n")
        body = {"issue_ref": args.issue, "target": args.target, "agent_id": agent, "exit_code": code,
                "executed": True, "commit": args.commit, "output_tail": tail,
                "operation_id": args.operation_id or str(uuid4())}
        _out(cli._request("POST", args.url, "/api/vq/runs", body))
        raise SystemExit(code)


def add_parser(sub):
    parser = sub.add_parser("vq", help="Validation queue: code-complete work awaiting validation on a target")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("enqueue", "list", "next", "record", "run", "show", "purge"):
        p = actions.add_parser(action)
        p.add_argument("--target", required=action != "list", help="Deployed target, e.g. dev-eastus2")
        if action in {"enqueue", "record", "run", "show"}:
            p.add_argument("--issue", required=True, help="owner/repo#NNNN")
        if action == "next":
            p.add_argument("--issue", help="Claim this item by name (needed for FAILED items)")
        if action == "list":
            p.add_argument("--state", help="pending|claimed|validated|self_reported|failed|inconclusive")
        if action == "enqueue":
            p.add_argument("--script", help="Validation command, run by `vq run`")
            p.add_argument("--proc", help="Path to the per-issue procedure file")
            p.add_argument("--pr")
        if action == "record":
            p.add_argument("--exit-code", type=int, required=True, choices=(0, 1, 2),
                           help="0 PASS, 1 FAIL, 2 INCONCLUSIVE (self-reported: cannot validate)")
            p.add_argument("--output")
        if action in {"record", "run"}:
            p.add_argument("--commit", help="Deployed commit the run was made against")
            p.add_argument("--operation-id", help="UUID to reuse when retrying the same run")
        if action == "run":
            p.add_argument("--timeout", type=int, default=900)
        if action not in {"list", "show", "purge"}:
            p.add_argument("--agent-id")
        p.set_defaults(func=command)
