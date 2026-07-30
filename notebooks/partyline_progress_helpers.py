"""Pure, stdlib-only helpers for the Partyline progress-reporting example job.

Deliberately free of dbutils / WorkspaceClient / Databricks imports so the logic is
unit-testable off-cluster and the file is a clean copy-and-adapt reference. The notebook
(partyline_progress.py) supplies the run-context values (job id, run id, token, url) and
these functions turn them into REST calls. REST is done with stdlib urllib — the same
mechanism the partyline CLI uses internally — so no client wheel needs to be installed on
the serverless task.
"""
from __future__ import annotations


def derive_agent_id(job_id: str | None, run_id: str | None, override: str | None) -> str:
    """Agent id for the line. A non-blank override wins; otherwise derive from run context.

    The derived form (job_<job_id>_run_<run_id>) is intentionally computed from runtime
    context, never a checked-in constant — a committed id is exactly the coupling that
    causes cross-delivery (defect #1545), so the example must not reintroduce it.
    """
    if override and override.strip():
        return override.strip()
    if job_id and run_id:
        return f"job_{job_id}_run_{run_id}"
    return "job_adhoc"


def build_start_summary(run_label: str | None) -> str:
    base = "run started"
    if run_label and run_label.strip():
        return f"{base} — {run_label.strip()}"
    return base


def build_step_summary(task_label: str, metric: int) -> str:
    return f"{task_label}: processed {metric:,} rows"


def build_finish_summary(outcomes: list[str]) -> str:
    if outcomes:
        return "run complete — " + "; ".join(outcomes)
    return "run complete — no task outcomes reported"


def build_failure_summary(outcomes: list[str]) -> str:
    if outcomes:
        return "run finished with upstream failure — " + "; ".join(outcomes)
    return "run finished with upstream failure — no task outcomes reported"


def simulated_metric(task_label: str) -> int:
    """A deterministic stand-in for 'work done' — no RNG so runs/tests are reproducible."""
    return 1000 + (sum(ord(c) for c in task_label) % 1000)
