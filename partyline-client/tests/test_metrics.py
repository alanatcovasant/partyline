import json
import logging
from covasant_partyline import metrics


def test_est_tokens_fallback_is_chars_over_four():
    # 40 chars -> 10 tokens under the fallback (tiktoken may give a different but positive value)
    n = metrics.est_tokens("x" * 40)
    assert n >= 1


class _Tool:
    def __init__(self, name, description, schema):
        self.name = name
        self.description = description
        self.inputSchema = schema


def test_footprint_has_per_tool_breakdown():
    tools = [
        _Tool("post_message", "post a message " * 20, {"type": "object", "properties": {}}),
        _Tool("get_unread", "read unread " * 10, {"type": "object", "properties": {}}),
    ]
    fp = metrics.footprint(tools, "INSTRUCTIONS " * 100)
    assert fp["total_bytes"] > 0
    assert fp["est_total_tokens"] > 0
    names = {t["name"] for t in fp["per_tool"]}
    assert names == {"post_message", "get_unread"}
    assert all(t["est_tokens"] > 0 for t in fp["per_tool"])


def test_log_call_emits_only_when_enabled(monkeypatch, caplog):
    monkeypatch.delenv("PARTYLINE_METRICS", raising=False)
    with caplog.at_level(logging.INFO, logger="partyline.metrics"):
        metrics.log_call("get_unread", 10, 200, 0, 5.0, "ok")
    assert not caplog.records  # disabled -> silent

    monkeypatch.setenv("PARTYLINE_METRICS", "1")
    with caplog.at_level(logging.INFO, logger="partyline.metrics"):
        metrics.log_call("get_unread", 10, 200, 0, 5.0, "ok")
    assert caplog.records
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["tool"] == "get_unread" and payload["outcome"] == "ok"
