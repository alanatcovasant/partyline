# Ruff Baseline Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ruff check .` pass without changing Partyline runtime behavior or broadly weakening lint enforcement.

**Architecture:** Preserve the Databricks notebook's runtime-injected `dbutils` and cell ordering with one file-local Ruff directive. Resolve every remaining finding through mechanical test-code formatting while preserving dynamic import order and assertions.

**Tech Stack:** Python 3.10+, Ruff, pytest, Databricks notebook source format, TypeScript/Vite verification

## Global Constraints

- No production logic, API contract, test assertion, fixture, or mock behavior changes.
- No project-wide Ruff ignores or test-directory exclusions.
- Preserve dynamic module loading and `sys.path` setup order.
- Only the six implementation files named in the approved spec may change.
- Repository-wide Ruff, both pytest suites, frontend build, and `git diff --check` must pass.

---

### Task 1: Make Databricks notebook semantics explicit to Ruff

**Files:**
- Modify: `notebooks/partyline_progress.py:1`
- Verify: `notebooks/partyline_progress.py`

**Interfaces:**
- Consumes: Databricks runtime injection of the global `dbutils` object and notebook cell import ordering.
- Produces: A notebook that Ruff accepts without adding runtime objects or moving executable statements.

- [ ] **Step 1: Reproduce the notebook findings**

Run:

```powershell
ruff check notebooks/partyline_progress.py
```

Expected: FAIL with `F821` for `dbutils` and `E402` for cell-local imports.

- [ ] **Step 2: Add the minimal file-local directive**

Immediately after the Databricks source marker, add:

```python
# ruff: noqa: E402, F821
```

- [ ] **Step 3: Verify the notebook is clean**

Run:

```powershell
ruff check notebooks/partyline_progress.py
```

Expected: PASS with `All checks passed!`

### Task 2: Normalize lint-only test formatting

**Files:**
- Modify: `partyline-client/tests/test_sender.py:1-30`
- Modify: `tests/test_cli.py:1-10`
- Modify: `tests/test_grant_app_access.py:1-10`
- Modify: `tests/test_partyline_progress_helpers.py:1-60`
- Modify: `tests/test_provision_partyline_sp.py:1-70`
- Test: the same five files

**Interfaces:**
- Consumes: Existing test imports, dynamic module loaders, fixtures, mocks, and assertions.
- Produces: Semantically identical tests formatted according to Ruff's `E401`, `E402`, `E702`, and `F401` rules.

- [ ] **Step 1: Reproduce the test findings**

Run:

```powershell
ruff check partyline-client/tests/test_sender.py tests/test_cli.py tests/test_grant_app_access.py tests/test_partyline_progress_helpers.py tests/test_provision_partyline_sp.py
```

Expected: FAIL with combined-import, unused-import, late-import, and semicolon findings.

- [ ] **Step 2: Apply the mechanical cleanup**

Make these exact classes of edit:

```python
# Combined import
import importlib.util
import pathlib

# Separate dynamic loader execution
m = importlib.util.module_from_spec(_s)
_s.loader.exec_module(m)

# Separate object setup
existing = MagicMock()
existing.id = 42
```

Also:

- remove the unused `pytest` import from `tests/test_cli.py`;
- move `io`, `json as _json`, and `urllib.error` into the initial import block of
  `tests/test_partyline_progress_helpers.py`;
- split the function-local `sys, pathlib` import in `test_sender.py` without moving it,
  because the test intentionally alters `sys.path` before importing application models;
- retain every assertion and call in its original order.

- [ ] **Step 3: Verify the five files are clean**

Run the Task 2 Ruff command again.

Expected: PASS with `All checks passed!`

- [ ] **Step 4: Run the directly affected tests**

Run:

```powershell
pytest -q tests/test_cli.py tests/test_grant_app_access.py tests/test_partyline_progress_helpers.py tests/test_provision_partyline_sp.py
pytest -q partyline-client/tests/test_sender.py
```

Expected: both commands PASS with zero failures.

### Task 3: Verify and deliver the cleanup

**Files:**
- Verify: repository-wide Python, client Python, and frontend sources
- Commit: the six implementation files plus this plan

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: A clean lint baseline and a synchronized `develop` branch.

- [ ] **Step 1: Run repository-wide Ruff**

```powershell
ruff check .
```

Expected: PASS with `All checks passed!`

- [ ] **Step 2: Run both Python suites**

```powershell
pytest -q
Push-Location partyline-client; pytest -q; Pop-Location
```

Expected: both suites PASS with zero failures.

- [ ] **Step 3: Build the frontend**

```powershell
Push-Location covasant-partyline/client; npm run build; Pop-Location
```

Expected: TypeScript and Vite build exit zero.

- [ ] **Step 4: Check the final diff**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors and only the six implementation files are modified after
the plan commit.

- [ ] **Step 5: Commit and synchronize**

```powershell
git add notebooks/partyline_progress.py partyline-client/tests/test_sender.py tests/test_cli.py tests/test_grant_app_access.py tests/test_partyline_progress_helpers.py tests/test_provision_partyline_sp.py
git commit -m "chore: clean Ruff baseline"
git fetch origin develop
git push origin develop
```

Expected: local `develop` and `origin/develop` resolve to the same commit with a clean
working tree.

