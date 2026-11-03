"""Make a Partyline claim visible in GitHub, and notice claims made in GitHub (stdlib only).

Partyline's lease is authoritative (docs/PROPOSAL_Unified_Work_Queue_2026-11-03.md, 2.2). This module
writes a PROJECTION of it with the caller's own `gh` login: one claim comment (edited in place, with a
hidden machine marker) and one static label. It never blocks a claim: every failure is returned as a
warning. The Project field `Claimed by` is a later slice (it needs the organization Project's ids).

Foreign claims (made in GitHub by someone not using Partyline) are: the label with no marker, a `/claim`
comment from a collaborator, or an assignee. A foreign claim is never overwritten.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone

LABEL = "partyline:claimed"
LABEL_COLOR = "FBCA04"
LABEL_DESC = "Claimed by a Partyline seat (see the claim comment)"
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
_MARKER = re.compile(r"<!--\s*partyline-claim\s+v1\s+(.*?)\s*-->", re.S)
_KV = re.compile(r'(\w+)="([^"]*)"')
_REF = re.compile(r"^(?:issue:|pr:)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")
_CMD = re.compile(r"^/claim(?:\s+seat=(\S+))?\s*$", re.M)


class Gh:
    """Runs the `gh` CLI; replace with a fake in tests."""

    def run(self, args: list[str]) -> tuple[int, str, str]:
        try:
            p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return 127, "", str(exc)
        return p.returncode, p.stdout, p.stderr


def parse_ref(value: str) -> tuple[str, str, int]:
    m = _REF.match(value.strip())
    if not m:
        raise ValueError(f"not an issue reference: {value!r} (expected owner/repo#N or issue:owner/repo#N)")
    return m.group(1), m.group(2), int(m.group(3))


def build_comment(seat: str, folder: str | None, branch: str | None, since: str, next_action: str | None = None,
                  state: str = "claimed", note: str | None = None) -> str:
    marker = f'<!-- partyline-claim v1 seat="{seat}" folder="{folder or ""}" since="{since}" state="{state}" -->'
    if state == "released":
        text = f"Released by `{seat}`" + (f": {note}" if note else ".")
    else:
        bits = [f"Claimed by `{seat}`" + (f" ({folder})" if folder else "")]
        if branch:
            bits.append(f"branch `{branch}`")
        bits.append(f"since {since}")
        text = ", ".join(bits) + "."
        if next_action:
            text += f" Next: {next_action}"
    return f"{marker}\n{text}\n\n_Managed by Partyline; edit nothing here. Remove the `{LABEL}` label to release._"


def parse_marker(body: str) -> dict | None:
    m = _MARKER.search(body or "")
    return dict(_KV.findall(m.group(1))) if m else None


def _json(out: str):
    try:
        return json.loads(out)
    except ValueError:
        return None


def _comments(gh: Gh, owner: str, repo: str, number: int) -> list[dict]:
    rc, out, err = gh.run(["api", "--paginate", "--slurp", f"repos/{owner}/{repo}/issues/{number}/comments"])
    data = _json(out) if rc == 0 else None
    if data is None:
        raise RuntimeError(f"could not read comments: {err.strip() or out.strip()}")
    flat = [c for page in data for c in (page if isinstance(page, list) else [page])]
    return [{"id": c.get("id"), "body": c.get("body") or "", "user": (c.get("user") or {}).get("login"),
             "assoc": c.get("author_association")} for c in flat]


def read_state(gh: Gh, ref: str) -> dict:
    """What GitHub currently says about the claim on one issue."""
    owner, repo, number = parse_ref(ref)
    rc, out, err = gh.run(["issue", "view", str(number), "--repo", f"{owner}/{repo}", "--json", "state,labels,assignees"])
    issue = _json(out) if rc == 0 else None
    if issue is None:
        raise RuntimeError(f"could not read issue: {err.strip() or out.strip()}")
    comments = _comments(gh, owner, repo, number)
    rc, out, _ = gh.run(["api", "user", "--jq", ".login"])
    viewer = out.strip() if rc == 0 else None
    mine = next((c for c in reversed(comments) if parse_marker(c["body"])), None)
    commands = [{"seat": (m.group(1) or None), "author": c["user"]}
                for c in comments if c["assoc"] in TRUSTED for m in [_CMD.search(c["body"])] if m]
    return {
        "ref": f"{owner}/{repo}#{number}", "open": issue.get("state", "").upper() == "OPEN",
        "labeled": any(l.get("name") == LABEL for l in issue.get("labels") or []),
        "marker": parse_marker(mine["body"]) if mine else None, "comment_id": mine["id"] if mine else None,
        "commands": commands, "assignees": [a.get("login") for a in issue.get("assignees") or []],
        "viewer": viewer,
    }


def foreign_claim(state: dict, my_seat: str) -> dict | None:
    """A claim not made by this seat's Partyline projection: who, and how we know."""
    mk = state["marker"]
    if mk and mk.get("state") == "claimed" and mk.get("seat") != my_seat:
        return {"claimant": mk.get("seat"), "via": "marker", "partyline": True}
    if state["labeled"] and not (mk and mk.get("state") == "claimed"):
        return {"claimant": None, "via": "label", "partyline": False}
    for c in state["commands"]:
        if c["seat"] != my_seat:
            return {"claimant": c["seat"] or c["author"], "via": "command", "partyline": bool(c["seat"])}
    # An assignee is a claim only when it is someone OTHER than the account we act as: every seat uses
    # that account, so an issue assigned to it is ordinary and must not block the fleet's own work.
    others = [a for a in state.get("assignees", []) if a and a != state.get("viewer")]
    if others:
        return {"claimant": others[0], "via": "assignee", "partyline": False}
    return None


def release_requested(state: dict) -> bool:
    """A human removed the label from a claim Partyline still shows as held: per the design a human's
    change always wins, so this is a request to release (the caller tells the seat; we do not guess)."""
    mk = state["marker"]
    return bool(mk and mk.get("state") == "claimed" and not state["labeled"])


def _ensure_label(gh: Gh, owner: str, repo: str) -> None:
    gh.run(["label", "create", LABEL, "--repo", f"{owner}/{repo}", "--color", LABEL_COLOR,
            "--description", LABEL_DESC, "--force"])


def project_claim(gh: Gh, ref: str, seat: str, folder: str | None = None, branch: str | None = None,
                  next_action: str | None = None, now: datetime | None = None) -> dict:
    """Write the claim marks after Partyline granted the lease. Never raises."""
    result = {"ref": ref, "written": [], "warnings": []}
    try:
        owner, repo, number = parse_ref(ref)
        state = read_state(gh, ref)
        if not state["open"]:
            result["warnings"].append("issue is closed; no marks written")
            return result
        other = foreign_claim(state, seat)
        if other and not (other["via"] == "marker" and other["partyline"]):
            result["warnings"].append(
                f"issue is already claimed in GitHub ({other['via']}: {other['claimant'] or 'unknown'}); "
                "Partyline recorded your claim but wrote no marks, resolve with that claimant")
            return result
        if other:  # a stale marker from another Partyline seat: Partyline's lease wins, rewrite it
            result["warnings"].append(f"replaced a stale claim marker for {other['claimant']}")
        since = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%MZ")
        if state["marker"] and state["marker"].get("seat") == seat and state["marker"].get("state") == "claimed":
            since = state["marker"].get("since") or since  # re-claim by the same seat keeps the original time
        body = build_comment(seat, folder, branch, since, next_action)
        if state["comment_id"]:
            rc, out, err = gh.run(["api", "-X", "PATCH", f"repos/{owner}/{repo}/issues/comments/{state['comment_id']}",
                                   "-f", f"body={body}"])
            action = "comment edited"
        else:
            rc, out, err = gh.run(["api", f"repos/{owner}/{repo}/issues/{number}/comments", "-f", f"body={body}"])
            action = "comment created"
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        result["written"].append(action)
        if not state["labeled"]:
            _ensure_label(gh, owner, repo)
            rc, _, err = gh.run(["issue", "edit", str(number), "--repo", f"{owner}/{repo}", "--add-label", LABEL])
            if rc != 0:
                raise RuntimeError(err.strip())
            result["written"].append("label added")
    except Exception as exc:  # a GitHub failure must never block the claim
        result["warnings"].append(f"unprojected: {exc}")
    return result


def release_claim(gh: Gh, ref: str, seat: str, note: str | None = None) -> dict:
    """Mark the claim released and drop the label, only if this seat holds the marker. Never raises."""
    result = {"ref": ref, "written": [], "warnings": []}
    try:
        owner, repo, number = parse_ref(ref)
        state = read_state(gh, ref)
        mk = state["marker"]
        if not mk or mk.get("seat") != seat or mk.get("state") != "claimed":
            result["warnings"].append("no claim marker for this seat; nothing released in GitHub")
            return result
        body = build_comment(seat, mk.get("folder"), None, mk.get("since", ""), state="released", note=note)
        rc, out, err = gh.run(["api", "-X", "PATCH", f"repos/{owner}/{repo}/issues/comments/{state['comment_id']}",
                               "-f", f"body={body}"])
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip())
        result["written"].append("comment edited")
        if state["labeled"]:
            rc, _, err = gh.run(["issue", "edit", str(number), "--repo", f"{owner}/{repo}", "--remove-label", LABEL])
            if rc != 0:
                raise RuntimeError(err.strip())
            result["written"].append("label removed")
    except Exception as exc:
        result["warnings"].append(f"unreleased in GitHub: {exc}")
    return result
