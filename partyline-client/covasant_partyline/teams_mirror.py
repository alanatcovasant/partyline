"""One-way mirror from Partyline into a Microsoft Teams channel.

Polls ``GET /api/messages`` on a timer and posts a filtered subset to a Teams
Workflow webhook (the "Post to a channel when a webhook request is received"
template). It does not hold ``/api/stream``. A standing SSE connection would
keep the Databricks app warm, which fights the line's idle-cost design; a
minute of delay is enough for a human channel.

The cursor is a local file, not a Partyline checkpoint. This process is not an
agent: it does not register, and it does not move anyone's read cursor.
Delivery is at-least-once. A crash after Teams accepts a card and before the
cursor is saved will post that card again on the next poll.

The first run records the current high-water message id and posts nothing, so
enabling the mirror does not replay history. ``--backfill N`` posts the last N
matching messages from the latest page, once, when no cursor file exists yet.

``GET /api/messages`` selects a page by id and then reorders it by event_time.
The mirror sorts each page by id before advancing the cursor, so a later
event_time on a lower id cannot move the cursor backwards and replay the page.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import cli

log = logging.getLogger("partyline.teams")

DEFAULT_TYPES = ("escalation", "blocked", "question", "done")
KNOWN_TYPES = frozenset(
    {"status", "question", "claim", "release", "blocked", "done", "ack", "escalation", "summary"}
)
DEFAULT_INTERVAL = 60.0
PAGE_LIMIT = 500
# One poll catches up at most this many pages, then resumes next interval.
MAX_PAGES_PER_POLL = 20
SUMMARY_LIMIT = 300
PAYLOAD_LIMIT = 1000
FACT_LIMIT = 200

TYPE_COLOR = {
    "escalation": "Attention",
    "blocked": "Attention",
    "question": "Accent",
    "done": "Good",
}

_HTTP_TIMEOUT = float(os.getenv("PARTYLINE_HTTP_TIMEOUT", "30"))


class MirrorError(Exception):
    """A poll failed. ``retryable`` failures leave the cursor where it is."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class MirrorConfig:
    base_url: str
    types: frozenset[str]
    cursor_path: Path
    page_limit: int = PAGE_LIMIT
    backfill: int = 0
    # False for --dry-run: compute the same cards, but do not move the cursor file.
    persist: bool = True
    # One-shot override from --from-id. Cleared after the poll that consumes it so a
    # long-running process does not rewind to this id on every interval.
    start_at: int | None = None
    # Dry-run never writes the file, so the in-memory cursor is what keeps the next
    # poll from seeding again and reprinting the same cards.
    memory_cursor: int | None = None


@dataclass
class RunStats:
    posted: int
    skipped: int
    cursor: int
    seeded: bool


def parse_types(raw: str) -> frozenset[str]:
    types = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = types - KNOWN_TYPES
    if unknown:
        raise SystemExit(f"unknown message types: {', '.join(sorted(unknown))}")
    if not types:
        raise SystemExit("at least one message type is required")
    return types


def default_cursor_path() -> Path:
    env = os.getenv("PARTYLINE_TEAMS_CURSOR")
    if env:
        return Path(env)
    return Path.home() / ".partyline" / "teams-cursor.json"


def redact_webhook(url: str) -> str:
    """Drop the query string. On a Workflow URL that string is the signature."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def load_cursor(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["last_id"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MirrorError(f"cursor file {path} is unreadable: {exc}", retryable=False) from exc


def save_cursor(path: Path, last_id: int, *, persist: bool) -> None:
    if not persist:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"last_id": last_id}) + "\n", encoding="utf-8")
    tmp.replace(path)


def message_matches(msg: dict, types: frozenset[str]) -> bool:
    return str(msg.get("message_type") or "") in types


def _clip(text: object, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def card_for(msg: dict) -> dict:
    """Adaptive Card envelope the Teams Workflow webhook template posts as-is."""
    mtype = str(msg.get("message_type") or "message")
    agent = str(msg.get("agent_id") or "unknown")
    summary = _clip(msg.get("summary"), SUMMARY_LIMIT)
    payload = _clip(msg.get("payload"), PAYLOAD_LIMIT)
    title = f"{mtype} · {agent}"
    recipient = msg.get("recipient")
    if recipient:
        title += f" → {recipient}"
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": title,
            "weight": "Bolder",
            "wrap": True,
            "color": TYPE_COLOR.get(mtype, "Default"),
        }
    ]
    main = summary or payload
    if main:
        body.append({"type": "TextBlock", "text": main, "wrap": True})
    if payload and summary and payload != summary:
        body.append({"type": "TextBlock", "text": payload, "wrap": True, "isSubtle": True})
    facts: list[dict] = []
    for label, key in (
        ("Repo", "repo"),
        ("Branch", "branch"),
        ("Issue", "issue_ref"),
        ("Resource", "resource_path"),
    ):
        val = msg.get(key)
        if val:
            facts.append({"title": label, "value": _clip(val, FACT_LIMIT)})
    mentions = msg.get("mentions") or []
    if mentions:
        facts.append({"title": "Mentions", "value": _clip(", ".join(str(m) for m in mentions), FACT_LIMIT)})
    if msg.get("id") is not None:
        facts.append({"title": "Message", "value": str(msg["id"])})
    if facts:
        body.append({"type": "FactSet", "facts": facts})
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": body,
                },
            }
        ],
    }


def webhook_retryable(status: int) -> bool:
    """400-class payload rejections are skipped; auth, absence, and throttling are not."""
    if status in (400, 413, 415):
        return False
    return status in (401, 403, 404, 408, 409, 410, 429) or status >= 500


def _read_http_error(exc: urllib.error.HTTPError) -> MirrorError:
    raw = exc.read().decode(errors="replace")[:300]
    return MirrorError(f"HTTP {exc.code}: {raw}", retryable=webhook_retryable(exc.code))


def http_json(method: str, url: str, *, token: str | None, timeout: float, body: dict | None = None) -> object:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        raise _read_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MirrorError(str(exc), retryable=True) from exc


def fetch_messages(base_url: str, path: str, *, token: str | None, timeout: float) -> list[dict]:
    """Read a Partyline list endpoint.

    Every failure is retryable. A 400 from the line must not be treated like a rejected
    Teams card: skipping a page would drop messages the mirror has not delivered.
    """
    url = base_url.rstrip("/") + path
    try:
        data = http_json("GET", url, token=token, timeout=timeout)
    except MirrorError as exc:
        if exc.retryable:
            raise
        raise MirrorError(str(exc), retryable=True) from exc
    if not isinstance(data, list):
        raise MirrorError(f"{path} did not return a list", retryable=True)
    return data


def post_webhook(webhook_url: str, card: dict, *, timeout: float) -> None:
    data = json.dumps(card).encode()
    req = urllib.request.Request(webhook_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise _read_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MirrorError(str(exc), retryable=True) from exc


def _messages_path(*, after_id: int | None = None, latest: bool = False, limit: int, high_water: bool = False) -> str:
    if high_water:
        # Newest id across the live log and the archive. Seeding here skips history
        # without walking the table from id 0.
        return f"/api/messages/query?limit={limit}"
    query: dict[str, str] = {"limit": str(limit)}
    if latest:
        query["latest"] = "true"
    if after_id is not None:
        query["after_id"] = str(after_id)
    return "/api/messages?" + urllib.parse.urlencode(query)


def _high_water(fetch) -> int:
    """Newest message id, live or archived. ``0`` when the log is empty."""
    rows = fetch(_messages_path(high_water=True, limit=1))
    if not rows:
        return 0
    return max(int(row["id"]) for row in rows)


def _send(post, msg: dict) -> str:
    try:
        post(card_for(msg))
    except MirrorError as exc:
        if exc.retryable:
            raise
        log.error("skipping message id=%s: %s", msg.get("id"), exc)
        return "skipped"
    return "posted"


def _send_backfill(cfg: MirrorConfig, fetch, post, mark: int) -> tuple[int, int]:
    rows = fetch(_messages_path(latest=True, limit=cfg.page_limit))
    ordered = sorted((row for row in rows if int(row["id"]) <= mark), key=lambda row: int(row["id"]))
    matching = [row for row in ordered if message_matches(row, cfg.types)]
    chosen = matching[-cfg.backfill :]
    posted = skipped = 0
    for msg in chosen:
        outcome = _send(post, msg)
        if outcome == "posted":
            posted += 1
        else:
            skipped += 1
    return posted, skipped


def _forward(cfg: MirrorConfig, fetch, post, cursor: int) -> tuple[int, int, int]:
    posted = skipped = 0
    current = cursor
    for _ in range(MAX_PAGES_PER_POLL):
        rows = fetch(_messages_path(after_id=current, limit=cfg.page_limit))
        if not rows:
            return posted, skipped, current
        ordered = sorted(rows, key=lambda row: int(row["id"]))
        if int(ordered[-1]["id"]) <= current:
            raise MirrorError(f"message page did not advance past id {current}", retryable=True)
        for msg in ordered:
            mid = int(msg["id"])
            if mid <= current:
                continue
            if message_matches(msg, cfg.types):
                outcome = _send(post, msg)
                if outcome == "posted":
                    posted += 1
                else:
                    skipped += 1
            current = mid
            save_cursor(cfg.cursor_path, current, persist=cfg.persist)
        if len(rows) < cfg.page_limit:
            return posted, skipped, current
    log.info("catch-up paused at id=%s; remaining messages wait for the next poll", current)
    return posted, skipped, current


def run_once(cfg: MirrorConfig, *, fetch, post) -> RunStats:
    """One poll.

    ``fetch(path) -> list[dict]`` and ``post(card)`` are injected so tests never touch
    the network. A retryable ``MirrorError`` leaves the cursor unmoved past the message
    that failed.
    """
    if cfg.start_at is not None:
        cursor = cfg.start_at
        cfg.start_at = None
    elif not cfg.persist and cfg.memory_cursor is not None:
        cursor = cfg.memory_cursor
    else:
        cursor = load_cursor(cfg.cursor_path)
    posted = skipped = 0
    seeded = False
    if cursor is None:
        mark = _high_water(fetch)
        if cfg.backfill > 0:
            posted, skipped = _send_backfill(cfg, fetch, post, mark)
        else:
            seeded = True
        save_cursor(cfg.cursor_path, mark, persist=cfg.persist)
        cursor = mark
    more_posted, more_skipped, cursor = _forward(cfg, fetch, post, cursor)
    if not cfg.persist:
        cfg.memory_cursor = cursor
    return RunStats(
        posted=posted + more_posted,
        skipped=skipped + more_skipped,
        cursor=cursor,
        seeded=seeded,
    )


def _token(base_url: str, profile: str | None) -> str | None:
    return cli.resolve_stream_token(base_url, profile, os.getenv("PARTYLINE_TOKEN"))


def build_fetch(base_url: str, profile: str | None, timeout: float):
    """Fresh Databricks token on every request. A token captured once dies in about an hour."""

    def fetch(path: str) -> list[dict]:
        return fetch_messages(base_url, path, token=_token(base_url, profile), timeout=timeout)

    return fetch


def build_post(webhook_url: str, timeout: float, *, dry_run: bool):
    def post(card: dict) -> None:
        if dry_run:
            print(json.dumps(card, indent=2))
            return
        post_webhook(webhook_url, card, timeout=timeout)

    return post


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partyline-teams",
        description="Mirror selected Partyline messages into a Microsoft Teams channel.",
    )
    parser.add_argument("--url", default=None, help="App base URL (default: same resolution as the partyline CLI)")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile used to mint a fresh token each poll")
    parser.add_argument(
        "--webhook-url",
        default=None,
        help="Teams Workflow webhook URL (default: TEAMS_WEBHOOK_URL, else teamsWebhookUrl in .partyline/config.json). The query string is a secret.",
    )
    parser.add_argument(
        "--types",
        default=os.getenv("PARTYLINE_TEAMS_TYPES", ",".join(DEFAULT_TYPES)),
        help=f"Comma-separated message types to mirror (default: {','.join(DEFAULT_TYPES)})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("PARTYLINE_TEAMS_INTERVAL", DEFAULT_INTERVAL)),
        help="Seconds between polls (default: 60). Ignored with --once.",
    )
    parser.add_argument(
        "--cursor",
        default=None,
        help="Cursor file (default: PARTYLINE_TEAMS_CURSOR, else ~/.partyline/teams-cursor.json)",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=int(os.getenv("PARTYLINE_TEAMS_BACKFILL", "0")),
        help="On the first run only, also post the last N matching messages from the latest page",
    )
    parser.add_argument(
        "--from-id",
        type=int,
        default=None,
        help="Set the cursor to this id before polling. Messages with a higher id are eligible.",
    )
    parser.add_argument("--once", action="store_true", help="Poll once and exit. For a scheduler.")
    parser.add_argument("--dry-run", action="store_true", help="Print cards and do not move the cursor file")
    parser.add_argument("--allow-insecure", action="store_true", help="Permit a non-https webhook URL")
    parser.add_argument("--timeout", type=float, default=_HTTP_TIMEOUT, help="Per-request timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    webhook = cli.resolve_teams_webhook(args.webhook_url)
    if not args.dry_run and not webhook:
        raise SystemExit("TEAMS_WEBHOOK_URL, --webhook-url, or teamsWebhookUrl in .partyline/config.json is required")
    if webhook and not webhook.lower().startswith("https://") and not args.allow_insecure:
        raise SystemExit("webhook URL must be https (pass --allow-insecure to override)")
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    if args.backfill < 0:
        raise SystemExit("--backfill must be >= 0")

    base_url = cli.resolve_url(args.url)
    profile = cli.resolve_profile(args.profile, url=base_url)
    cursor_path = Path(args.cursor) if args.cursor else default_cursor_path()
    cfg = MirrorConfig(
        base_url=base_url,
        types=parse_types(args.types),
        cursor_path=cursor_path,
        backfill=args.backfill,
        persist=not args.dry_run,
        start_at=args.from_id,
    )
    if args.from_id is not None:
        log.warning("cursor set to %s from --from-id", args.from_id)
        save_cursor(cursor_path, args.from_id, persist=cfg.persist)

    fetch = build_fetch(base_url, profile, args.timeout)
    post = build_post(webhook or "", args.timeout, dry_run=args.dry_run)
    log.info(
        "teams mirror url=%s types=%s interval=%ss cursor=%s webhook=%s",
        base_url,
        ",".join(sorted(cfg.types)),
        args.interval,
        cursor_path,
        redact_webhook(webhook) if webhook else "(dry-run)",
    )
    while True:
        try:
            stats = run_once(cfg, fetch=fetch, post=post)
            log.info(
                "posted=%s skipped=%s cursor=%s seeded=%s",
                stats.posted,
                stats.skipped,
                stats.cursor,
                stats.seeded,
            )
        except MirrorError as exc:
            log.warning("poll failed: %s", exc)
            if args.once or not exc.retryable:
                raise SystemExit(1) from exc
        except KeyboardInterrupt:
            return
        if args.once:
            return
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
