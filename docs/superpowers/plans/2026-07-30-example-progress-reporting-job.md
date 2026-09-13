# Example Partyline Progress-Reporting Job — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an additive, self-contained example Databricks job whose multi-task notebook workload reports live progress to the Covasant Partyline as one collapsible thread, configured through job parameters → widgets, running as a dedicated service principal that is provisioned and granted access fully automatically.

**Architecture:** A single widget-driven notebook (`notebooks/partyline_progress.py`) backs every task; an `action` widget (`start`/`step`/`finish`) selects behavior. It imports pure, stdlib-only helpers (`notebooks/partyline_progress_helpers.py`) that build message payloads, derive the agent id from run context, and POST to the app's REST API over `urllib` — no wheel install. Cross-task threading uses `dbutils.jobs.taskValues` to pass the seed message id down the DAG. Two idempotent bootstrap scripts (`scripts/provision_partyline_sp.py`, `scripts/grant_app_access.py`) create the job's dedicated SP via account SCIM and grant it `CAN_USE` on the deployed app; `deploy.sh` runs them in order (app deploy → SP create → grant → job deploy).

**Tech Stack:** Databricks Asset Bundles (jobs, serverless notebook tasks, `parameters`, target-gated `run_as`, bundle variables); Databricks SDK (`WorkspaceClient`, account SCIM REST, `apps.*`); Python stdlib `urllib` for REST; pytest for the pure helpers and mocked-SDK script tests.

## Global Constraints

- **No manual steps.** All infra must be created by `./deploy.sh`; if a resource type isn't automatable, flag it as a blocker — never add a "click this in the UI" step. (Account SCIM SP creation *is* scriptable — proven by the Aura Foundra exemplar — so the dedicated-SP path stays automated.)
- **Always pass `--profile dev-eastus2`** (or `-p dev-eastus2`) to every `databricks` CLI call; the default CLI profile points elsewhere.
- **Verify against the deployed app** (`databricks bundle run …`), not just locally. **Clean up** any test agents/messages posted to the live line afterward.
- **Additive only.** No changes to the partyline app, REST API, DB schema, or MCP tools. Only new files plus edits to `deploy.sh`, `databricks.yml`, and `README.md`.
- **Claim shared surfaces before editing them** on the line: `deploy.sh`, `databricks.yml`, `resources/*.yml`. `claim_resource(repo, resource_path, ttl_seconds=900)`; if refused, post a `question` to the holder instead of editing. `release_resource` when done.
- **Naming:** the notebook is `partyline_progress`, **not** `partyline_notify` (collides with `alan_aura_0`'s in-flight deploy helper).
- **`agent_id` must default from run context, not from any committed file** — do not reintroduce a `.mcp.json`/env-sourced id coupling (keeps the example structurally clear of defect #1545).
- **Python floor:** stdlib-only helpers must run on Python 3.10+ (matches the CLI's stated floor).

---

## File Structure

**Create:**
- `notebooks/partyline_progress_helpers.py` — pure, stdlib-only, importable + unit-testable: agent-id derivation, payload builders, `register_agent`/`post_message` over `urllib`.
- `notebooks/partyline_progress.py` — the Databricks notebook (source format). Reads widgets, resolves URL + token from run context, imports the helpers, dispatches on `action`, threads via `taskValues`.
- `scripts/provision_partyline_sp.py` — idempotent: find-or-create the job SP via account SCIM, ensure it can use the workspace, print `applicationId`.
- `scripts/grant_app_access.py` — idempotent: grant the SP `CAN_USE` on the deployed app.
- `resources/example_progress_job.job.yml` — the example job (DAG, job-level `parameters`, task-level params, serverless notebook tasks).
- `tests/test_partyline_progress_helpers.py` — unit tests for the helpers (no cluster, no network — `urllib` mocked).
- `tests/test_provision_partyline_sp.py` — unit tests with a mocked `WorkspaceClient` (find-before-create idempotency).
- `tests/test_grant_app_access.py` — unit test with a mocked `WorkspaceClient` (grant call shape + idempotency).

**Modify:**
- `databricks.yml` — add the `partyline_sp_app_id` variable; add a **prod-only** `run_as` override for the example job.
- `deploy.sh` — add the bootstrap phase (SP create → grant) and the job-deploy `--var` wiring.
- `README.md` — add a short "Example: progress-reporting job" section.

---

## Task 1: Pure notebook helpers (agent-id + payload builders)

The testable core. Pure functions only — no `dbutils`, no `WorkspaceClient`, no globals. This is what makes the example verifiable off-cluster and is the spec's "extract pure helpers" requirement.

**Files:**
- Create: `notebooks/partyline_progress_helpers.py`
- Test: `tests/test_partyline_progress_helpers.py`

**Interfaces:**
- Produces (later tasks and the notebook rely on these exact signatures):
  - `derive_agent_id(job_id: str | None, run_id: str | None, override: str | None) -> str` — returns `override` if truthy/non-blank, else `f"job_{job_id}_run_{run_id}"`; if both ids are missing, returns `"job_adhoc"`.
  - `build_start_summary(run_label: str | None) -> str` — `"run started"` optionally suffixed `f" — {run_label}"` when `run_label` is non-blank.
  - `build_step_summary(task_label: str, metric: int) -> str` — `f"{task_label}: processed {metric:,} rows"`.
  - `build_finish_summary(outcomes: list[str]) -> str` — `"run complete — " + "; ".join(outcomes)` when non-empty, else `"run complete — no task outcomes reported"`.
  - `build_failure_summary(outcomes: list[str]) -> str` — `"run finished with upstream failure — " + "; ".join(outcomes)` when non-empty, else `"run finished with upstream failure — no task outcomes reported"`.
  - `simulated_metric(task_label: str) -> int` — deterministic pseudo-metric from `task_label` (no RNG, so tests are stable): `1000 + (sum(ord(c) for c in task_label) % 1000)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_partyline_progress_helpers.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_partyline_progress_helpers.py -v`
Expected: FAIL — `notebooks/partyline_progress_helpers.py` does not exist yet (collection/import error).

- [ ] **Step 3: Write the payload/id helpers**

```python
# notebooks/partyline_progress_helpers.py
"""Pure, stdlib-only helpers for the Partyline progress-reporting example job.

Deliberately free of dbutils / WorkspaceClient / Databricks imports so the logic is
unit-testable off-cluster and the file is a clean copy-and-adapt reference. The notebook
(partyline_progress.py) supplies the run-context values (job id, run id, token, url) and
these functions turn them into REST calls. REST is done with stdlib urllib — the same
mechanism the partyline CLI uses internally — so no client wheel needs to be installed on
the serverless task.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_partyline_progress_helpers.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add notebooks/partyline_progress_helpers.py tests/test_partyline_progress_helpers.py
git commit -m "feat(example-job): pure notebook helpers (agent-id + payload builders)"
```

---

## Task 2: REST helpers in the same module (register + post over urllib)

Adds the two network calls the notebook makes. Kept in the same helpers module so the notebook has a single import, and tested with `urllib.request.urlopen` monkeypatched — no live app needed.

**Files:**
- Modify: `notebooks/partyline_progress_helpers.py`
- Test: `tests/test_partyline_progress_helpers.py`

**Interfaces:**
- Consumes: (none beyond stdlib)
- Produces:
  - `register_agent(url: str, token: str | None, agent_id: str, owner: str, repo: str, branch: str, *, timeout: float = 30.0) -> dict` — `POST {url}/api/agents`; returns the parsed agent row.
  - `post_message(url: str, token: str | None, *, agent_id: str, message_type: str, summary: str, payload: str | None = None, repo: str | None = None, branch: str | None = None, recipient: str | None = None, reply_to_id: int | None = None, timeout: float = 30.0) -> dict` — `POST {url}/api/messages`; returns the parsed message row (its `id` is the value later tasks reply to).
  - Both raise `PartylineError(status:int|None, body:str)` on HTTP/connection failure so the notebook can log a real failure instead of a false success.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_partyline_progress_helpers.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_partyline_progress_helpers.py -k "register or post_message" -v`
Expected: FAIL — `register_agent`/`post_message`/`PartylineError` not defined.

- [ ] **Step 3: Implement the REST helpers**

```python
# append to notebooks/partyline_progress_helpers.py

class PartylineError(RuntimeError):
    """A partyline REST call failed. Carries the HTTP status (or None for a transport error)
    and the response body, so a failed POST surfaces loudly rather than looking like success."""

    def __init__(self, status: int | None, body: str):
        super().__init__(f"partyline call failed (status={status}): {body[:300]}")
        self.status = status
        self.body = body


def _request(method: str, url: str, token: str | None, body: dict | None, timeout: float) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        raise PartylineError(e.code, e.read().decode(errors="replace")) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PartylineError(None, str(getattr(e, "reason", e))) from e


def register_agent(url, token, agent_id, owner, repo, branch, *, timeout=30.0):
    return _request("POST", url.rstrip("/") + "/api/agents", token, {
        "agent_id": agent_id, "owner": owner, "repo": repo,
        "branch": branch, "display_name": agent_id,
    }, timeout)


def post_message(url, token, *, agent_id, message_type, summary, payload=None,
                 repo=None, branch=None, recipient=None, reply_to_id=None, timeout=30.0):
    return _request("POST", url.rstrip("/") + "/api/messages", token, {
        "agent_id": agent_id, "message_type": message_type, "summary": summary,
        "payload": payload, "repo": repo, "branch": branch,
        "recipient": recipient, "reply_to_id": reply_to_id,
        "mentions": [], "metadata": {},
    }, timeout)
```

- [ ] **Step 4: Run the full helper test file to verify it passes**

Run: `python -m pytest tests/test_partyline_progress_helpers.py -v`
Expected: PASS (all tests, ~12).

- [ ] **Step 5: Commit**

```bash
git add notebooks/partyline_progress_helpers.py tests/test_partyline_progress_helpers.py
git commit -m "feat(example-job): urllib register/post REST helpers + PartylineError"
```

---

## Task 3: The widget-driven notebook

Wires the helpers to Databricks runtime: widgets in, `WorkspaceClient` for URL + token, `taskValues` for threading, `action` dispatch. Cannot be unit-tested against a live cluster here; correctness of its *logic* lives in the Task 1–2 helpers. This task ends at a `bundle validate` gate (proves the file is syntactically loadable as a notebook source) and is exercised live in Task 7.

**Files:**
- Create: `notebooks/partyline_progress.py`

**Interfaces:**
- Consumes: everything from `partyline_progress_helpers` (Tasks 1–2).
- Produces: taskValues contract — `notify_start` sets `seed_id` (int); `work_a`/`work_b` each set `outcome` (str). `notify_finish` reads them.

- [ ] **Step 1: Write the notebook (Databricks source format)**

```python
# notebooks/partyline_progress.py
# Databricks notebook source
# COMMAND ----------
# MAGIC %md
# MAGIC # Partyline progress reporter
# MAGIC One notebook backs every task in the example job. The `action` widget selects
# MAGIC behavior: `start` seeds a thread, `step` posts a threaded progress reply, `finish`
# MAGIC posts the summary. Configure via job parameters (surfaced here as widgets).

# COMMAND ----------
import os
import sys

# Import the pure helpers that live next to this notebook in the synced bundle. The bundle
# syncs the whole notebooks/ dir to the workspace, so the sibling .py is on disk beside us.
sys.path.append(os.path.dirname(os.path.abspath(
    dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    if False else "."  # notebookPath is workspace-relative; fall through to cwd append below
)))
# Serverless notebook tasks run with the synced files as CWD-adjacent workspace files;
# appending the notebook's own directory makes the sibling importable regardless.
sys.path.append(os.getcwd())
_here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else None
if _here:
    sys.path.append(_here)

import partyline_progress_helpers as pl  # noqa: E402

# COMMAND ----------
# Job-level parameters (see resources/example_progress_job.job.yml) arrive as widgets.
dbutils.widgets.text("app_name", "mcp-covasant-partyline")
dbutils.widgets.text("partyline_url", "")     # optional explicit override; blank => resolve by app_name
dbutils.widgets.text("agent_id", "")          # blank => derive from run context
dbutils.widgets.text("owner", "covasant")
dbutils.widgets.text("repo", "covasant-partyline")
dbutils.widgets.text("branch", "main")
dbutils.widgets.text("run_label", "")
# Task-level parameters (set per task in the job yml).
dbutils.widgets.dropdown("action", "step", ["start", "step", "finish"])
dbutils.widgets.text("task_label", "work")

app_name = dbutils.widgets.get("app_name")
partyline_url = dbutils.widgets.get("partyline_url").strip()
agent_id_override = dbutils.widgets.get("agent_id")
owner = dbutils.widgets.get("owner")
repo = dbutils.widgets.get("repo")
branch = dbutils.widgets.get("branch")
run_label = dbutils.widgets.get("run_label")
action = dbutils.widgets.get("action")
task_label = dbutils.widgets.get("task_label")

# COMMAND ----------
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
job_id = ctx.tags().get("jobId").getOrElse(None) if hasattr(ctx.tags().get("jobId"), "getOrElse") else None
run_id = ctx.tags().get("multitaskParentRunId").getOrElse(None) if hasattr(ctx.tags().get("multitaskParentRunId"), "getOrElse") else None

agent_id = pl.derive_agent_id(job_id, run_id, agent_id_override)

# Resolve the app URL by name unless an explicit override was given.
url = partyline_url or w.apps.get(name=app_name).url

# Token: the current run identity's OAuth token, accepted by the app's Databricks OAuth proxy.
# Primary source — the notebook run-context API token:
token = ctx.apiToken().get()

print(f"action={action} agent_id={agent_id} url={url} task_label={task_label}")

# COMMAND ----------
if action == "start":
    pl.register_agent(url, token, agent_id, owner, repo, branch)
    row = pl.post_message(
        url, token, agent_id=agent_id, message_type="status",
        summary=pl.build_start_summary(run_label),
        payload=f"Example progress job started. run_label={run_label!r}",
        repo=repo, branch=branch,
    )
    seed_id = int(row["id"])
    dbutils.jobs.taskValues.set(key="seed_id", value=seed_id)
    print(f"seeded thread seed_id={seed_id}")

elif action == "step":
    seed_id = dbutils.jobs.taskValues.get(taskKey="notify_start", key="seed_id", default=0, debugValue=0)
    metric = pl.simulated_metric(task_label)
    import time
    time.sleep(2)  # simulated work
    row = pl.post_message(
        url, token, agent_id=agent_id, message_type="status",
        summary=pl.build_step_summary(task_label, metric),
        repo=repo, branch=branch, reply_to_id=(seed_id or None),
    )
    dbutils.jobs.taskValues.set(key="outcome", value=pl.build_step_summary(task_label, metric))
    print(f"posted step reply id={row.get('id')} onto seed_id={seed_id}")

elif action == "finish":
    seed_id = dbutils.jobs.taskValues.get(taskKey="notify_start", key="seed_id", default=0, debugValue=0)
    outcomes = []
    for upstream in ("work_a", "work_b"):
        val = dbutils.jobs.taskValues.get(taskKey=upstream, key="outcome", default="", debugValue="")
        if val:
            outcomes.append(val)
    # ALL_DONE means we run even if an upstream failed; missing outcomes => report failure.
    ok = len(outcomes) == 2
    summary = pl.build_finish_summary(outcomes) if ok else pl.build_failure_summary(outcomes)
    pl.post_message(
        url, token, agent_id=agent_id,
        message_type="done" if ok else "blocked",
        summary=summary, repo=repo, branch=branch, reply_to_id=(seed_id or None),
    )
    print(f"finished ok={ok}: {summary}")

else:
    raise ValueError(f"unknown action: {action!r}")
```

- [ ] **Step 2: Sanity-check the file parses as Python**

Run: `python -c "import ast; ast.parse(open('notebooks/partyline_progress.py', encoding='utf-8').read()); print('parse ok')"`
Expected: `parse ok` (catches syntax errors before a live run; `dbutils`/`WorkspaceClient` are runtime globals, not import-time).

- [ ] **Step 3: Commit**

```bash
git add notebooks/partyline_progress.py
git commit -m "feat(example-job): widget-driven progress notebook (start/step/finish)"
```

> Note on the token source: `ctx.apiToken().get()` is the primary path. If the live run in Task 7 returns 401 from the app, switch the token line to the SDK-minted header instead:
> ```python
> token = w.config.oauth_token().access_token  # fallback: SDK-minted OAuth token
> ```
> Both are concrete; pick whichever authenticates in Task 7 and delete the other.

---

## Task 4: The example job resource (DAG + parameters + serverless)

**Files:**
- Create: `resources/example_progress_job.job.yml`

**Interfaces:**
- Consumes: `notebooks/partyline_progress.py` (Task 3). `run_as` override + `partyline_sp_app_id` var wired in Task 6.
- Produces: job `example_progress_job` with tasks `notify_start`, `work_a`, `work_b`, `notify_finish`.

- [ ] **Step 1: Claim the resources dir on the line, then write the job yml**

Claim first (shared surface):
```bash
./partyline.sh register alan_partyline_1 --owner alan --repo covasant-partyline --branch develop
./partyline.sh claim alan_partyline_1 covasant-partyline resources/example_progress_job.job.yml --branch develop --ttl 900 --reason "new example job"
```

```yaml
# resources/example_progress_job.job.yml
# Example job: a multi-task notebook workload that reports live progress to the Partyline
# as one threaded conversation. One notebook (notebooks/partyline_progress.py) backs every
# task; the `action` task-parameter selects start/step/finish. Fully parameterized so it is
# copy-and-adapt friendly. run_as is overridden to the dedicated SP in the prod target only
# (databricks.yml) — development mode forces run_as to the deploying user.
resources:
  jobs:
    example_progress_job:
      name: partyline_example_progress

      # Job-level parameters -> widgets in the notebook. Blank agent_id => derived per run.
      parameters:
        - name: app_name
          default: "mcp-covasant-partyline"
        - name: partyline_url
          default: ""
        - name: agent_id
          default: ""
        - name: owner
          default: "covasant"
        - name: repo
          default: "covasant-partyline"
        - name: branch
          default: "main"
        - name: run_label
          default: ""

      tasks:
        - task_key: notify_start
          notebook_task:
            notebook_path: ../notebooks/partyline_progress.py
            base_parameters:
              action: "start"
              task_label: "notify_start"
          environment_key: default

        - task_key: work_a
          depends_on:
            - task_key: notify_start
          notebook_task:
            notebook_path: ../notebooks/partyline_progress.py
            base_parameters:
              action: "step"
              task_label: "work_a"
          environment_key: default

        - task_key: work_b
          depends_on:
            - task_key: work_a
          notebook_task:
            notebook_path: ../notebooks/partyline_progress.py
            base_parameters:
              action: "step"
              task_label: "work_b"
          environment_key: default

        - task_key: notify_finish
          depends_on:
            - task_key: work_b
          run_if: ALL_DONE      # post the terminal message even if an upstream task failed
          notebook_task:
            notebook_path: ../notebooks/partyline_progress.py
            base_parameters:
              action: "finish"
              task_label: "notify_finish"
          environment_key: default

      # Serverless environment for the notebook tasks. No dependencies block: the notebook
      # is stdlib-only (urllib) plus the always-present databricks-sdk, so nothing to install.
      environments:
        - environment_key: default
          spec:
            environment_version: "4"
```

- [ ] **Step 2: Validate the bundle (dev target)**

Run: `databricks bundle validate --strict -t dev-eastus2 --profile dev-eastus2`
Expected: no errors. If it complains that a serverless notebook task cannot carry `environment_key`, remove the `environments` block and each task's `environment_key:` line (serverless notebook tasks default to a managed environment); re-validate. Record which form validated in the commit message.

- [ ] **Step 3: Release the claim and commit**

```bash
./partyline.sh release alan_partyline_1 covasant-partyline resources/example_progress_job.job.yml --branch develop
git add resources/example_progress_job.job.yml
git commit -m "feat(example-job): example_progress_job DAG (start->work_a->work_b->finish)"
```

---

## Task 5: SP provisioning + grant scripts (idempotent, mocked-SDK tests)

Two bootstrap scripts modeled on the Foundra SCIM exemplar and the repo's existing `w.api_client.do` REST pattern (`provision_lakebase.py`, `migrate_schema.py`).

**Files:**
- Create: `scripts/provision_partyline_sp.py`
- Create: `scripts/grant_app_access.py`
- Test: `tests/test_provision_partyline_sp.py`
- Test: `tests/test_grant_app_access.py`

**Interfaces:**
- Produces:
  - `provision_partyline_sp.resolve_or_create_sp(w, display_name: str) -> str` — returns the SP `applicationId`; find-by-displayName first, create via SCIM only if absent. `main()` prints the applicationId as the **last stdout line** (deploy.sh captures it).
  - `grant_app_access.grant(w, app_name: str, sp_app_id: str) -> None` — idempotent `CAN_USE` grant for the SP on the app.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_provision_partyline_sp.py
import importlib.util, pathlib
from unittest.mock import MagicMock

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "provision_partyline_sp.py"
_s = importlib.util.spec_from_file_location("provision_partyline_sp", _P)
m = importlib.util.module_from_spec(_s); _s.loader.exec_module(m)


def test_resolve_existing_sp_does_not_create():
    w = MagicMock()
    w.api_client.do.return_value = {"Resources": [{"applicationId": "app-123", "id": "1"}]}
    app_id = m.resolve_or_create_sp(w, "covasant-partyline-example-job")
    assert app_id == "app-123"
    # Only the GET/list call happened — no POST create.
    methods = [c.args[0] for c in w.api_client.do.call_args_list]
    assert "POST" not in methods


def test_create_sp_when_absent():
    w = MagicMock()
    w.api_client.do.side_effect = [
        {"Resources": []},                                   # list: none found
        {"applicationId": "app-new", "id": "9"},             # create
    ]
    app_id = m.resolve_or_create_sp(w, "covasant-partyline-example-job")
    assert app_id == "app-new"
    methods = [c.args[0] for c in w.api_client.do.call_args_list]
    assert methods[0] == "GET" and methods[1] == "POST"
```

```python
# tests/test_grant_app_access.py
import importlib.util, pathlib
from unittest.mock import MagicMock

_P = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "grant_app_access.py"
_s = importlib.util.spec_from_file_location("grant_app_access", _P)
m = importlib.util.module_from_spec(_s); _s.loader.exec_module(m)


def test_grant_calls_update_permissions_with_can_use():
    w = MagicMock()
    m.grant(w, "mcp-covasant-partyline", "app-123")
    assert w.apps.update_permissions.called
    kwargs = w.apps.update_permissions.call_args.kwargs
    assert kwargs["name"] == "mcp-covasant-partyline"
    acl = kwargs["access_control_list"]
    assert acl and getattr(acl[0], "service_principal_name", None) == "app-123"


def test_grant_falls_back_to_rest_when_sdk_method_missing():
    w = MagicMock()
    del w.apps.update_permissions   # simulate older SDK without the typed method
    m.grant(w, "mcp-covasant-partyline", "app-123")
    # fallback path uses the raw permissions REST endpoint
    call = w.api_client.do.call_args
    assert call.args[0] == "PATCH"
    assert "/permissions/apps/mcp-covasant-partyline" in call.args[1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_provision_partyline_sp.py tests/test_grant_app_access.py -v`
Expected: FAIL — scripts not created yet.

- [ ] **Step 3: Write `scripts/provision_partyline_sp.py`**

```python
"""Idempotent provisioning of the example job's dedicated service principal via account SCIM.

Run (as a user with account SCIM / admin rights) AFTER the app is deployed and BEFORE the
example job is deployed. Prints the SP applicationId as the LAST stdout line so deploy.sh can
capture it for the job's run_as. Safe to re-run: find-by-displayName, create only if absent.

  DATABRICKS_CONFIG_PROFILE=dev-eastus2 python scripts/provision_partyline_sp.py
"""
from __future__ import annotations

import os
import sys

from databricks.sdk import WorkspaceClient

DISPLAY_NAME = os.getenv("PARTYLINE_SP_DISPLAY_NAME", "covasant-partyline-example-job")
_SCIM_SP = "/api/2.0/account/scim/v2/ServicePrincipals"


def resolve_or_create_sp(w: WorkspaceClient, display_name: str) -> str:
    """Return the SP applicationId; create the SP via account SCIM only if not present."""
    found = w.api_client.do(
        "GET", _SCIM_SP, query={"filter": f'displayName eq "{display_name}"'}
    )
    resources = (found or {}).get("Resources") or []
    for r in resources:
        if r.get("applicationId"):
            print(f"[ok] SP '{display_name}' exists (applicationId={r['applicationId']})", file=sys.stderr)
            return r["applicationId"]

    print(f"[+] creating SP '{display_name}' via account SCIM", file=sys.stderr)
    created = w.api_client.do(
        "POST", _SCIM_SP,
        body={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServicePrincipal"],
            "displayName": display_name,
            "active": True,
        },
    )
    app_id = created.get("applicationId")
    if not app_id:
        sys.exit(f"SCIM create returned no applicationId: {created}")
    return app_id


def main() -> None:
    w = WorkspaceClient()
    app_id = resolve_or_create_sp(w, DISPLAY_NAME)
    # LAST stdout line = the applicationId (deploy.sh: APP_ID="$(... | tail -1)").
    print(app_id)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write `scripts/grant_app_access.py`**

```python
"""Idempotent grant of CAN_USE on the deployed app to the example job's SP.

A freshly created SP is not in the `users` group (which the bundle grants CAN_USE), so it
must be granted explicitly — least privilege: it can reach only this app. Run AFTER the app
is deployed. Idempotent: the permissions PATCH merges the ACL, so re-running is a no-op.

  DATABRICKS_CONFIG_PROFILE=dev-eastus2 APP_NAME=mcp-covasant-partyline \\
    PARTYLINE_SP_APP_ID=<applicationId> python scripts/grant_app_access.py
"""
from __future__ import annotations

import os
import sys

from databricks.sdk import WorkspaceClient


def grant(w: WorkspaceClient, app_name: str, sp_app_id: str) -> None:
    """Grant CAN_USE to sp_app_id on app_name. Prefer the typed SDK method; fall back to REST
    for older SDKs (the deployed CLI's SDK may lag — see the version-skew note in the repo)."""
    if hasattr(w.apps, "update_permissions"):
        from databricks.sdk.service import apps as apps_svc
        acl = [apps_svc.AppAccessControlRequest(
            service_principal_name=sp_app_id,
            permission_level=apps_svc.AppPermissionLevel.CAN_USE,
        )]
        w.apps.update_permissions(name=app_name, access_control_list=acl)
    else:
        w.api_client.do(
            "PATCH", f"/api/2.0/permissions/apps/{app_name}",
            body={"access_control_list": [
                {"service_principal_name": sp_app_id, "permission_level": "CAN_USE"}
            ]},
        )


def main() -> None:
    app_name = os.getenv("APP_NAME", "mcp-covasant-partyline")
    sp_app_id = os.getenv("PARTYLINE_SP_APP_ID")
    if not sp_app_id:
        sys.exit("PARTYLINE_SP_APP_ID is required")
    w = WorkspaceClient()
    grant(w, app_name, sp_app_id)
    print(f"[ok] granted CAN_USE on {app_name} to SP {sp_app_id}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_provision_partyline_sp.py tests/test_grant_app_access.py -v`
Expected: PASS (4 tests). If `test_grant_falls_back_to_rest_when_sdk_method_missing` fails because `AppAccessControlRequest`/`AppPermissionLevel` names differ in the installed SDK, keep the REST fallback as the primary path (delete the `hasattr` branch) — the raw PATCH is version-stable. Re-run.

- [ ] **Step 6: Commit**

```bash
git add scripts/provision_partyline_sp.py scripts/grant_app_access.py tests/test_provision_partyline_sp.py tests/test_grant_app_access.py
git commit -m "feat(example-job): idempotent SP provisioning + app CAN_USE grant scripts"
```

---

## Task 6: Bundle variable + prod-only SP run_as

**Files:**
- Modify: `databricks.yml`

**Interfaces:**
- Consumes: `example_progress_job` (Task 4), `partyline_sp_app_id` from deploy.sh (Task 7).
- Produces: the `partyline_sp_app_id` variable and a prod `run_as` override.

- [ ] **Step 1: Claim `databricks.yml` on the line**

```bash
./partyline.sh claim alan_partyline_1 covasant-partyline databricks.yml --branch develop --ttl 900 --reason "add sp var + prod run_as"
```

- [ ] **Step 2: Add the variable declaration**

In `databricks.yml`, under `variables:` (after the `schema:` block), add:

```yaml
  partyline_sp_app_id:
    description: applicationId of the example job's dedicated service principal (fed by deploy.sh after SCIM create); only bound as run_as in prod
    default: ""
```

- [ ] **Step 3: Add the prod-only run_as override**

In `databricks.yml`, under `targets: prod:` (as a sibling of `workspace:`/`variables:`/`permissions:`), add:

```yaml
    resources:
      jobs:
        example_progress_job:
          run_as:
            service_principal_name: ${var.partyline_sp_app_id}
```

Leave `dev-eastus2` untouched — `mode: development` runs the job as the deploying user (an SP `run_as` is rejected in development mode), which still exercises the notebook→partyline path for live verification.

- [ ] **Step 4: Validate both targets**

Run:
```bash
databricks bundle validate --strict -t dev-eastus2 --profile dev-eastus2
databricks bundle validate --strict -t prod --var="partyline_sp_app_id=00000000-0000-0000-0000-000000000000" --profile dev-eastus2
```
Expected: both validate with no errors. (The dummy UUID lets prod validate without a real SP.) If prod validate rejects an empty `default: ""` for the var independently of the override, that's fine — the override only references it and validate passes with the `--var` above.

- [ ] **Step 5: Release the claim and commit**

```bash
./partyline.sh release alan_partyline_1 covasant-partyline databricks.yml --branch develop
git add databricks.yml
git commit -m "feat(example-job): partyline_sp_app_id var + prod-only SP run_as"
```

---

## Task 7: deploy.sh bootstrap phase + live verification

Wire the ordered bootstrap into `deploy.sh` (app deploy → SP create → grant → job deploy) and verify the thread appears on the live line.

**Files:**
- Modify: `deploy.sh`

**Interfaces:**
- Consumes: `scripts/provision_partyline_sp.py`, `scripts/grant_app_access.py`, `partyline_sp_app_id` var, `example_progress_job`.

- [ ] **Step 1: Claim `deploy.sh` on the line**

```bash
./partyline.sh claim alan_partyline_1 covasant-partyline deploy.sh --branch develop --ttl 900 --reason "add SP bootstrap phase"
```

- [ ] **Step 2: Add the bootstrap phase to `deploy.sh`**

After the existing final step (`==> 4/4 Restarting app`, the `databricks bundle run covasant_partyline …` line), append:

```bash
echo "==> 5/5 Bootstrapping example-job service principal + grant"
# Ordering matters: the app must already be deployed (steps 3–4 above) before we can grant
# CAN_USE on it. SP creation needs account SCIM rights; if unavailable this step fails loudly
# (a real prerequisite, not a UI step). Both scripts are idempotent no-ops on reruns.
SP_APP_ID="$(DATABRICKS_CONFIG_PROFILE="$PROFILE" python scripts/provision_partyline_sp.py | tail -1)"
echo "    example job SP applicationId=${SP_APP_ID}"
DATABRICKS_CONFIG_PROFILE="$PROFILE" APP_NAME="mcp-covasant-partyline" \
  PARTYLINE_SP_APP_ID="$SP_APP_ID" python scripts/grant_app_access.py

echo "==> 5.5/5 Deploying example job"
# In prod the job's run_as binds to the SP (databricks.yml override); pass the applicationId.
# In dev the override is absent, so the --var is harmless (the job runs as the deploying user).
databricks bundle deploy -t "$TARGET" --profile "$PROFILE" \
  --var="partyline_sp_app_id=${SP_APP_ID}"

echo "==> Done. App + example job deployed (version=${FULL_VERSION})."
```

- [ ] **Step 3: Run the full deploy against dev**

Run: `./deploy.sh dev-eastus2 dev-eastus2`
Expected: completes through step 5.5; prints a non-empty `applicationId`; the grant step prints `[ok] granted CAN_USE …`. If SCIM create 403s, that's the account-admin prerequisite — stop and flag it to the user (do not add a manual UI step).

- [ ] **Step 4: Run the example job and watch the line**

```bash
databricks bundle run example_progress_job -t dev-eastus2 --profile dev-eastus2
```
While it runs (or after), read the thread:
```bash
./partyline.sh search --repo covasant-partyline --type status --limit 20
# find the derived agent_id (job_<id>_run_<id>) start message, grab its conversation_id, then:
./partyline.sh conversation <conversation_id>
```
Expected: one conversation — `run started` → `work_a: processed N rows` (reply) → `work_b: processed N rows` (reply) → `run complete — …` (`done` reply). If the notebook 401s on the app call, apply the token fallback from Task 3's note and redeploy.

- [ ] **Step 5: Clean up the live test + release claim, then commit**

Per CLAUDE.md, remove the test thread from the live line (archive via the dashboard's archive-all, or leave a `done` note if archival isn't scriptable for a single thread — flag which). Then:
```bash
./partyline.sh release alan_partyline_1 covasant-partyline deploy.sh --branch develop
git add deploy.sh
git commit -m "feat(example-job): deploy.sh SP bootstrap phase + job deploy wiring"
```

---

## Task 8: README section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add an "Example: progress-reporting job" subsection**

Insert after the "Client package" section (before "VS Code Extension"). Cover: what it demonstrates (job → partyline progress thread), how to run it (`databricks bundle run example_progress_job -t dev-eastus2 --profile dev-eastus2`), the parameter table (copy from the spec's config surface), the dev-vs-prod `run_as` behavior (deploying user in dev, dedicated SP in prod), and how to adapt it (replace the `time.sleep`/`simulated_metric` in the `step` branch with real work; keep `action`/`task_label` per task).

```markdown
## Example: progress-reporting job

`resources/example_progress_job.job.yml` is a copy-and-adapt reference: a four-task job
(`notify_start → work_a → work_b → notify_finish`) where every task runs the same
widget-driven notebook (`notebooks/partyline_progress.py`) and posts to the line, so a run
reads as one collapsible thread (start → two progress replies → done). Configuration flows
through job **parameters** surfaced as notebook **widgets**; the notebook talks to the app
over REST with stdlib `urllib` (no client wheel to install).

Run it:

    databricks bundle run example_progress_job -t dev-eastus2 --profile dev-eastus2

| Parameter | Default | Purpose |
|---|---|---|
| `app_name` | `mcp-covasant-partyline` | resolve the app URL via `w.apps.get(app_name).url` |
| `partyline_url` | *(blank)* | optional explicit URL override |
| `agent_id` | *(blank)* | blank → derives `job_<job_id>_run_<run_id>` |
| `owner` / `repo` / `branch` | `covasant` / `covasant-partyline` / `main` | registration metadata |
| `run_label` | *(blank)* | free-text tag folded into the start message |

`action` (`start`/`step`/`finish`) and `task_label` are set per task. **Identity:** in `prod`
the job runs as a dedicated service principal (`deploy.sh` creates it via account SCIM and
grants it `CAN_USE` on the app); in `dev-eastus2` (`mode: development`) it runs as the
deploying user. **Adapt** it by replacing the simulated metric + sleep in the notebook's
`step` branch with real work; add tasks by pointing them at the same notebook with a new
`task_label`.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(example-job): README section for the progress-reporting example"
```

---

## Self-Review

**Spec coverage:**
- Multi-task job → progress thread → Tasks 3, 4 (DAG + notebook).
- parameters → widgets → Task 3 (widgets), Task 4 (`parameters`).
- Cross-task threading (one conversation) → Task 3 (`taskValues` seed_id + `reply_to_id`).
- Fully automated infra / no manual steps → Task 7 (deploy.sh bootstrap).
- Dedicated managed identity + explicit grant → Tasks 5, 6, 7.
- Bootstrap ordering (app → SP → grant → job) → Task 7.
- App-URL discovery by name → Task 3 (`w.apps.get(app_name).url`).
- Reusable single notebook (`action` dispatch) → Task 3.
- Error handling: failed post surfaces (not silent success) → Task 2 (`PartylineError`); ALL_DONE terminal message → Tasks 3, 4.
- Not affected by #1545 (agent_id from run context, not committed) → Task 1 (`derive_agent_id`) + Global Constraints.
- Naming `partyline_progress` not `partyline_notify` → Global Constraints, all tasks.
- Testing: pure helpers unit-tested (Tasks 1–2), idempotency of scripts (Task 5), live verification + cleanup (Task 7).
- Deliverables list (spec §Deliverables, 6 items) → all created/modified across Tasks 1–8.

**Open-item resolution (spec §Open items):**
1. `run_as` under dev mode → prod-only SP override; dev runs as deploying user (Task 6). Validated in Task 6 Step 4.
2. Serverless wheel install → eliminated; stdlib `urllib` only, no `environments.dependencies` (Tasks 2, 4).
3. Grant API path → SDK `apps.update_permissions` with raw-REST fallback, idempotent (Task 5).

**Placeholder scan:** No TBD/TODO. Every code step has concrete code. The two env-dependent forks (notebook token source in Task 3; serverless `environment_key` acceptance in Task 4; SDK permission type names in Task 5) each have a concrete primary path AND a concrete fallback with an explicit trigger condition — not placeholders.

**Type consistency:** `derive_agent_id`, `build_start_summary`, `build_step_summary`, `build_finish_summary`, `build_failure_summary`, `simulated_metric`, `register_agent`, `post_message`, `PartylineError` used consistently across Tasks 1–3 and tests. taskValues keys (`seed_id`, `outcome`) and task keys (`notify_start`, `work_a`, `work_b`, `notify_finish`) consistent between Tasks 3 and 4. `resolve_or_create_sp`/`grant` signatures consistent between Task 5 scripts and their tests. `partyline_sp_app_id` var name consistent across Tasks 6–7.
