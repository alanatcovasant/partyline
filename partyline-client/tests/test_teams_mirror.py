"""Unit tests for the one-way Teams mirror. No live Partyline and no live Teams."""

import io
import json
import urllib.error
import urllib.parse
from email.message import Message

import pytest

from covasant_partyline import teams_mirror
from covasant_partyline.teams_mirror import MirrorConfig, MirrorError


def _cfg(tmp_path, **overrides) -> MirrorConfig:
    values = dict(
        base_url="https://app.example",
        types=frozenset({"escalation", "blocked", "question", "done"}),
        cursor_path=tmp_path / "cursor.json",
        page_limit=500,
        backfill=0,
        persist=True,
    )
    values.update(overrides)
    return MirrorConfig(**values)


def _msg(mid, mtype, summary="hello", **extra):
    row = {"id": mid, "message_type": mtype, "agent_id": "alan_web_1", "summary": summary, "payload": None}
    row.update(extra)
    return row


def _routes(*, high=None, latest=None, pages=None):
    """Build a fetch(path) stub. ``pages`` maps after_id -> rows."""
    pages = pages or {}

    def fetch(path):
        parsed = urllib.parse.urlsplit(path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/query"):
            return [] if high is None else [{"id": high}]
        if qs.get("latest") == ["true"]:
            return list(latest or [])
        after = int(qs.get("after_id", ["0"])[0])
        limit = int(qs.get("limit", ["500"])[0])
        rows = [row for row in pages.get(after, []) if int(row["id"]) > after]
        return rows[:limit]

    return fetch


def test_card_uses_summary_recipient_and_type_color():
    card = teams_mirror.card_for(
        _msg(12, "question", summary="API shape?", recipient="sam_api_1", repo="web", mentions=["sam_api_1"])
    )
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard"
    assert content["version"] == "1.2"
    title = content["body"][0]
    assert title["text"] == "question · alan_web_1 → sam_api_1"
    assert title["color"] == "Accent"
    assert content["body"][1]["text"] == "API shape?"
    facts = {fact["title"]: fact["value"] for fact in content["body"][2]["facts"]}
    assert facts["Repo"] == "web"
    assert facts["Mentions"] == "sam_api_1"
    assert facts["Message"] == "12"
    assert "Branch" not in facts


def test_card_falls_back_to_payload_and_truncates():
    card = teams_mirror.card_for(_msg(1, "blocked", summary=None, payload="x" * 1200))
    body = card["attachments"][0]["content"]["body"]
    text = body[1]["text"]
    assert text.endswith("...")
    assert len(text) == teams_mirror.PAYLOAD_LIMIT
    # No second block when summary is empty: the payload is already the main text.
    assert all(block.get("isSubtle") is not True for block in body)


def test_card_shows_payload_detail_under_a_different_summary():
    card = teams_mirror.card_for(_msg(1, "done", summary="shipped", payload="the migration landed"))
    texts = [block.get("text") for block in card["attachments"][0]["content"]["body"]]
    assert "shipped" in texts
    assert "the migration landed" in texts


def test_parse_types_rejects_unknown_and_blank():
    assert teams_mirror.parse_types(" done, question ") == frozenset({"done", "question"})
    with pytest.raises(SystemExit):
        teams_mirror.parse_types("nope")
    with pytest.raises(SystemExit):
        teams_mirror.parse_types(" , ")


def test_redact_webhook_strips_signature_query():
    url = "https://prod-00.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=secret&sp=1"
    assert teams_mirror.redact_webhook(url) == (
        "https://prod-00.logic.azure.com/workflows/abc/triggers/manual/paths/invoke"
    )
    assert "secret" not in teams_mirror.redact_webhook(url)


def test_webhook_status_classification():
    assert teams_mirror.webhook_retryable(429) is True
    assert teams_mirror.webhook_retryable(503) is True
    assert teams_mirror.webhook_retryable(404) is True
    assert teams_mirror.webhook_retryable(401) is True
    assert teams_mirror.webhook_retryable(400) is False
    assert teams_mirror.webhook_retryable(413) is False


def test_first_run_seeds_then_delivers_only_messages_past_the_high_water_mark(tmp_path):
    posted = []
    stats = teams_mirror.run_once(
        _cfg(tmp_path),
        fetch=_routes(high=40, pages={40: [_msg(41, "question", summary="just arrived")]}),
        post=posted.append,
    )
    assert stats.seeded is True
    assert stats.cursor == 41
    assert stats.posted == 1
    assert posted[0]["attachments"][0]["content"]["body"][1]["text"] == "just arrived"
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"last_id": 41}


def test_first_run_with_nothing_new_records_high_water(tmp_path):
    stats = teams_mirror.run_once(_cfg(tmp_path), fetch=_routes(high=40), post=lambda card: None)
    assert stats == teams_mirror.RunStats(posted=0, skipped=0, cursor=40, seeded=True)
    assert json.loads((tmp_path / "cursor.json").read_text())["last_id"] == 40


def test_empty_log_seeds_at_zero(tmp_path):
    stats = teams_mirror.run_once(_cfg(tmp_path), fetch=_routes(high=None), post=lambda card: None)
    assert stats.cursor == 0
    assert stats.posted == 0


def test_forward_posts_matching_types_and_advances_through_the_rest(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_id": 10}))
    posted = []
    rows = [
        _msg(11, "status", summary="ignore me"),
        _msg(12, "question", summary="help"),
        _msg(13, "ack", summary="ok"),
        _msg(14, "done", summary="shipped"),
        _msg(15, "blocked", summary="stuck"),
    ]
    stats = teams_mirror.run_once(
        _cfg(tmp_path),
        fetch=_routes(pages={10: rows}),
        post=posted.append,
    )
    assert stats.posted == 3
    assert stats.seeded is False
    assert stats.cursor == 15
    titles = [card["attachments"][0]["content"]["body"][0]["text"] for card in posted]
    assert titles == [
        "question · alan_web_1",
        "done · alan_web_1",
        "blocked · alan_web_1",
    ]


def test_forward_sorts_by_id_before_advancing_the_cursor(tmp_path):
    """The API reorders a page by event_time. Saving cursor in that order would rewind it."""
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_id": 10}))
    posted = []
    # event_time order: 12 then 11. Id order must win.
    rows = [_msg(12, "question", summary="second"), _msg(11, "question", summary="first")]
    teams_mirror.run_once(_cfg(tmp_path), fetch=_routes(pages={10: rows}), post=posted.append)
    summaries = [card["attachments"][0]["content"]["body"][1]["text"] for card in posted]
    assert summaries == ["first", "second"]
    assert json.loads(cursor.read_text())["last_id"] == 12


def test_retryable_post_failure_does_not_advance_past_that_message(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_id": 10}))
    calls = []

    def post(card):
        calls.append(card)
        if len(calls) == 2:
            raise MirrorError("teams 429", retryable=True)

    rows = [
        _msg(11, "status"),
        _msg(12, "question", summary="first"),
        _msg(13, "done", summary="second"),
    ]
    with pytest.raises(MirrorError):
        teams_mirror.run_once(_cfg(tmp_path), fetch=_routes(pages={10: rows}), post=post)
    # 11 was skipped (status), 12 posted, 13 failed. Cursor stays on 12.
    assert json.loads(cursor.read_text())["last_id"] == 12
    assert len(calls) == 2


def test_rejected_card_is_skipped_and_the_cursor_moves_on(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_id": 10}))
    posted = []

    def post(card):
        if "bad" in json.dumps(card):
            raise MirrorError("HTTP 400: rejected", retryable=False)
        posted.append(card)

    rows = [_msg(11, "question", summary="bad"), _msg(12, "done", summary="good")]
    stats = teams_mirror.run_once(_cfg(tmp_path), fetch=_routes(pages={10: rows}), post=post)
    assert stats.posted == 1
    assert stats.skipped == 1
    assert stats.cursor == 12
    assert posted[0]["attachments"][0]["content"]["body"][1]["text"] == "good"


def test_full_page_is_followed_by_the_next_page(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_id": 0}))
    posted = []
    pages = {
        0: [_msg(1, "question", summary="one"), _msg(2, "done", summary="two")],
        2: [_msg(3, "blocked", summary="three")],
    }
    stats = teams_mirror.run_once(
        _cfg(tmp_path, page_limit=2),
        fetch=_routes(pages=pages),
        post=posted.append,
    )
    assert stats.posted == 3
    assert stats.cursor == 3


def test_backfill_posts_the_last_n_matches_then_seeks_to_high_water(tmp_path):
    posted = []
    latest = [
        _msg(1, "question", summary="old"),
        _msg(2, "done", summary="keep"),
        _msg(3, "status", summary="noise"),
        _msg(4, "escalation", summary="newest"),
    ]
    stats = teams_mirror.run_once(
        _cfg(tmp_path, backfill=2),
        fetch=_routes(high=4, latest=latest),
        post=posted.append,
    )
    summaries = [card["attachments"][0]["content"]["body"][1]["text"] for card in posted]
    assert summaries == ["keep", "newest"]
    assert stats.posted == 2
    assert stats.cursor == 4
    assert stats.seeded is False


def test_backfill_failure_does_not_write_a_cursor(tmp_path):
    def post(card):
        raise MirrorError("down", retryable=True)

    latest = [_msg(4, "escalation", summary="newest")]
    with pytest.raises(MirrorError):
        teams_mirror.run_once(
            _cfg(tmp_path, backfill=1),
            fetch=_routes(high=4, latest=latest),
            post=post,
        )
    assert not (tmp_path / "cursor.json").exists()


def test_existing_cursor_ignores_backfill(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"last_id": 5}))
    posted = []
    latest = [_msg(4, "escalation", summary="already past")]
    stats = teams_mirror.run_once(
        _cfg(tmp_path, backfill=10),
        fetch=_routes(high=5, latest=latest, pages={5: []}),
        post=posted.append,
    )
    assert posted == []
    assert stats.cursor == 5
    assert stats.seeded is False


def test_dry_run_does_not_write_the_cursor_and_does_not_reseed(tmp_path):
    posted = []
    cfg = _cfg(tmp_path, persist=False)
    fetch = _routes(high=7, pages={7: [_msg(8, "done", summary="later")]})
    first = teams_mirror.run_once(cfg, fetch=fetch, post=posted.append)
    assert first.seeded is True
    assert first.posted == 1
    assert not (tmp_path / "cursor.json").exists()
    second = teams_mirror.run_once(cfg, fetch=fetch, post=posted.append)
    assert second.seeded is False
    assert second.posted == 0
    assert second.cursor == 8


def test_corrupt_cursor_is_not_retryable(tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{")
    with pytest.raises(MirrorError) as caught:
        teams_mirror.run_once(_cfg(tmp_path), fetch=_routes(high=1), post=lambda card: None)
    assert caught.value.retryable is False


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://example.test/x",
        code,
        "no",
        Message(),
        io.BytesIO(body),
    )


def test_partyline_http_errors_stay_retryable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(400, b'{"detail":"bad"}')

    monkeypatch.setattr(teams_mirror.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MirrorError) as caught:
        teams_mirror.fetch_messages("https://app.example", "/api/messages?after_id=1&limit=10", token="t", timeout=5)
    assert caught.value.retryable is True


def test_webhook_post_sends_adaptive_card_without_a_bearer_token(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["type"] = req.headers.get("Content-type")
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(teams_mirror.urllib.request, "urlopen", fake_urlopen)
    teams_mirror.post_webhook("https://example.test/hook?sig=secret", {"type": "message"}, timeout=5)
    assert captured["url"] == "https://example.test/hook?sig=secret"
    assert captured["auth"] is None
    assert captured["type"] == "application/json"
    assert captured["body"] == {"type": "message"}


def test_webhook_400_is_not_retryable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(400, b"rejected")

    monkeypatch.setattr(teams_mirror.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MirrorError) as caught:
        teams_mirror.post_webhook("https://example.test/hook", {"type": "message"}, timeout=5)
    assert caught.value.retryable is False


def test_build_fetch_remints_a_token_per_request(monkeypatch):
    tokens = iter(["t1", "t2"])
    monkeypatch.setattr(teams_mirror.cli, "resolve_stream_token", lambda url, profile, fallback: next(tokens))
    seen = []

    def fake_fetch(base_url, path, *, token, timeout):
        seen.append(token)
        return [{"id": 1}] if path.endswith("limit=1") else []

    monkeypatch.setattr(teams_mirror, "fetch_messages", fake_fetch)
    fetch = teams_mirror.build_fetch("https://app.example", "dev-eastus2", 5)
    assert fetch("/api/messages/query?limit=1") == [{"id": 1}]
    assert fetch("/api/messages?after_id=1&limit=500") == []
    assert seen == ["t1", "t2"]


def test_resolve_teams_webhook_precedence(monkeypatch):
    from covasant_partyline import cli

    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    cfg = {"teamsWebhookUrl": "https://cfg.test/hook?sig=cfg"}
    assert cli.resolve_teams_webhook(None, cfg) == "https://cfg.test/hook?sig=cfg"
    assert cli.resolve_teams_webhook("https://flag.test/hook?sig=flag", cfg) == "https://flag.test/hook?sig=flag"
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://env.test/hook?sig=env")
    assert cli.resolve_teams_webhook(None, cfg) == "https://env.test/hook?sig=env"
    monkeypatch.delenv("TEAMS_WEBHOOK_URL")
    assert cli.resolve_teams_webhook(None, {}) is None
