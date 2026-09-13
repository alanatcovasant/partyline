# Ruff Baseline Cleanup Design

**Date:** 2026-08-10
**Status:** Approved
**Scope:** Behavior-preserving cleanup of the repository-wide Ruff baseline

## Problem

`ruff check .` reports 54 errors. Most come from one Databricks notebook whose runtime
injects `dbutils` and whose cell-oriented imports intentionally occur after executable
widget setup. The remaining findings are mechanical test-code issues: combined imports,
unused imports, imports placed after test functions, and multiple statements on one line.

The repository should have a clean Ruff baseline without weakening lint enforcement or
changing runtime behavior.

## Design

### Databricks notebook

Add a file-local Ruff directive to `notebooks/partyline_progress.py`:

```python
# ruff: noqa: E402, F821
```

`F821` is limited to this file because Databricks injects `dbutils` at runtime. `E402` is
limited to this file because Databricks notebook cell ordering intentionally permits
imports after widget and context initialization. No fake `dbutils` shim, import movement,
or runtime statement will be introduced.

### Tests

Make only mechanical edits:

- split combined imports into one import per line;
- remove unused `pytest` imports;
- move standard-library imports to the existing top import block when safe;
- split semicolon-separated setup statements into separate lines;
- preserve dynamic module loading and `sys.path` setup order;
- preserve every assertion, fixture, mock, and tested behavior.

Files in scope:

- `partyline-client/tests/test_sender.py`
- `tests/test_cli.py`
- `tests/test_grant_app_access.py`
- `tests/test_partyline_progress_helpers.py`
- `tests/test_provision_partyline_sp.py`
- `notebooks/partyline_progress.py`

No project-wide ignore, test-directory exclusion, or Ruff rule suppression will be added.

## Verification

Run, in order:

1. `ruff check .` and require zero findings.
2. `pytest -q` from the repository root.
3. `pytest -q` from `partyline-client`.
4. `npm run build` from `covasant-partyline/client`.
5. `git diff --check` and require no whitespace errors.

Because this cleanup changes no production behavior, the existing tests are the behavioral
contract. No new test cases are required; any test failure is a regression and blocks the
cleanup.

## Completion criteria

- Repository-wide Ruff passes without broad exclusions.
- All existing Python tests pass.
- The frontend production build passes.
- Only the six listed files change during implementation.
- Runtime code and test assertions remain semantically unchanged.

