"""Unit tests for the pure notebook helpers — no cluster, no network."""
import importlib.util
import pathlib

_HELPERS = pathlib.Path(__file__).resolve().parent.parent / "notebooks" / "partyline_progress_helpers.py"
_spec = importlib.util.spec_from_file_location("partyline_progress_helpers", _HELPERS)
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)


def test_derive_agent_id_prefers_nonblank_override():
    assert h.derive_agent_id("11", "22", "alan_demo_1") == "alan_demo_1"


def test_derive_agent_id_blank_override_uses_run_context():
    assert h.derive_agent_id("11", "22", "") == "job_11_run_22"
    assert h.derive_agent_id("11", "22", "   ") == "job_11_run_22"
    assert h.derive_agent_id("11", "22", None) == "job_11_run_22"


def test_derive_agent_id_missing_ids_falls_back():
    assert h.derive_agent_id(None, None, None) == "job_adhoc"


def test_build_start_summary_with_and_without_label():
    assert h.build_start_summary(None) == "run started"
    assert h.build_start_summary("") == "run started"
    assert h.build_start_summary("nightly") == "run started — nightly"


def test_build_step_summary_formats_thousands():
    assert h.build_step_summary("work_a", 1000) == "work_a: processed 1,000 rows"


def test_build_finish_summary():
    assert h.build_finish_summary(["work_a: ok", "work_b: ok"]) == "run complete — work_a: ok; work_b: ok"
    assert h.build_finish_summary([]) == "run complete — no task outcomes reported"


def test_build_failure_summary():
    assert h.build_failure_summary(["work_a: ok"]).startswith("run finished with upstream failure — work_a: ok")
    assert h.build_failure_summary([]) == "run finished with upstream failure — no task outcomes reported"


def test_simulated_metric_is_deterministic_and_in_range():
    m1 = h.simulated_metric("work_a")
    assert m1 == h.simulated_metric("work_a")      # deterministic
    assert 1000 <= m1 < 2000                        # bounded band


import io
import json as _json
import urllib.error


class _FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def _capture(monkeypatch, response_obj):
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = _json.loads(req.data.decode()) if req.data else None
        captured["timeout"] = timeout
        return _FakeResp(_json.dumps(response_obj).encode())
    monkeypatch.setattr(h.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_register_agent_posts_expected_shape(monkeypatch):
    cap = _capture(monkeypatch, {"agent_id": "job_1_run_2"})
    row = h.register_agent("https://app", "tok", "job_1_run_2", "covasant", "covasant-partyline", "main")
    assert row["agent_id"] == "job_1_run_2"
    assert cap["url"] == "https://app/api/agents"
    assert cap["method"] == "POST"
    assert cap["headers"]["Authorization"] == "Bearer tok"
    assert cap["body"]["owner"] == "covasant"


def test_post_message_returns_id_and_threads(monkeypatch):
    cap = _capture(monkeypatch, {"id": 4242})
    row = h.post_message("https://app", "tok", agent_id="a", message_type="status",
                         summary="work_a: processed 1,000 rows", reply_to_id=17)
    assert row["id"] == 4242
    assert cap["body"]["reply_to_id"] == 17
    assert cap["body"]["message_type"] == "status"


def test_post_message_no_token_omits_auth_header(monkeypatch):
    cap = _capture(monkeypatch, {"id": 1})
    h.post_message("http://localhost:8000", None, agent_id="a", message_type="status", summary="s")
    assert "Authorization" not in cap["headers"]


def test_post_message_raises_partyline_error_on_http_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"nope"))
    monkeypatch.setattr(h.urllib.request, "urlopen", boom)
    try:
        h.post_message("https://app", "tok", agent_id="a", message_type="status", summary="s")
        assert False, "expected PartylineError"
    except h.PartylineError as e:
        assert e.status == 401
