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
