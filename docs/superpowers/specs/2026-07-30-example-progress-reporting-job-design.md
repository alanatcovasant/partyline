# Design: Example Partyline Progress-Reporting Job

**Date:** 2026-07-30
**Status:** Approved design — pending implementation plan

## Summary

An example Databricks job that demonstrates how a multi-task notebook workload reports
**live progress** to the Covasant Partyline. The job runs several notebook tasks; each
task posts to the line so a human (or another agent) watching the dashboard sees the run
unfold as **one collapsible thread**. Everything is configured through **job parameters**,
which a notebook task consumes as **widget** inputs. The job runs as its own **dedicated
service principal (SP)**, provisioned and granted access fully automatically — no manual
UI steps.

This is an *example/reference* asset: additive to the repo, self-contained (simulated
work), and intended to be copied and adapted.

## Goals

- Show the canonical pattern for a Databricks job → Partyline (progress + completion).
- Demonstrate **parameters → widgets** as the configuration mechanism.
- Demonstrate **cross-task threading** so a multi-task run reads as one conversation.
- Keep infra **fully automated** (`deploy.sh`), honoring the repo's "no manual steps" rule.
- Run under a **dedicated managed identity**, not a human's identity.

## Non-goals

- Not a general-purpose partyline SDK for jobs (the example may be extracted into one
  later, but this spec ships an *example*, not a library).
- No real data processing — work tasks are simulated (a computed metric + short sleep).
- No changes to the partyline app, REST API, DB schema, or MCP tools. Message semantics
  are reused exactly as-is.

## Key facts that shape the design

Two **independent identity layers** exist; conflating them is the main trap.

1. **Databricks auth (Layer 1 — who may reach the app).** The partyline app sits behind
   Databricks' OAuth proxy. A caller needs `CAN_USE` on `mcp-covasant-partyline`. The
   bundle grants this to `group_name: users` today. A **freshly created SP is NOT in
   `users`**, so it must be granted access explicitly.
2. **Partyline agent_id (Layer 2 — who you are on the line).** Pure application data —
   a self-declared string. `register_agent` is an unconditional
   `INSERT … ON CONFLICT DO UPDATE` (`covasant-partyline/app/db.py`). There is **no
   allowlist and no server-side provisioning**: registration *is* creation. The notebook
   simply registers an agent_id; nothing needs to pre-create it partyline-side.

Other grounding facts:

- **Transport:** the CLI, MCP tools, and dashboard are all thin clients over `/api/*`.
  The example uses the existing `covasant-partyline` **client package** over HTTP REST
  (`register` → `POST /api/agents`, `post` → `POST /api/messages`), reusing recipient /
  mentions / threading / NOTIFY-push semantics for free.
- **Params → widgets:** job-level `parameters:` are surfaced to a notebook task as
  `dbutils.widgets` values. "Configure via parameters" and "take input via widgets" are
  the same mechanism from the two ends.
- **App URL discovery:** the notebook resolves the app URL from its **name** via
  `w.apps.get("mcp-covasant-partyline").url` (matches the repo's app-name discovery
  convention), with an optional explicit `partyline_url` override.
- **SP creation is scriptable** via the account SCIM API
  (`POST /api/2.0/account/scim/v2/ServicePrincipals`) — proven by the Aura Foundra
  exemplar (`examples/exemplar create managed identity.ipynb`). This keeps the SP path
  compatible with the "no manual steps" rule.
- **Ordering constraint (bootstrap):** `CAN_USE` can only be granted **on the app once
  the app exists** as a grantable resource. On a first-ever deploy the app is created by
  the deploy itself, so the grant must run **after** the app is deployed. Later runs are
  idempotent no-ops.
- **App SP vs. job SP:** the *app's* managed identity (platform-created, used by the app
  to reach Lakebase) is a different principal from the *job's* SP. The job never uses the
  app's SP — it authenticates to the REST API with its own token and is authorized on its
  own `CAN_USE`.
- **Not affected by #1545 (the live channel defect).** The loud, active defect on the
  line is #1545: a committed `PARTYLINE_AGENT_ID` in `.mcp.json` crosses agents' inbound
  **channel/MCP** filter to the wrong id (misdelivery) or to nothing (silent deafness),
  and the crossing arrives silently on a routine `git pull`. This example posts over
  **REST with a self-declared agent_id** and does not consume the inbound channel, so it
  is structurally insulated: there is no `.mcp.json`-sourced filter id to cross. Recorded
  so the implementation does not accidentally reintroduce an env/committed-id coupling
  (the `agent_id` param defaults from run context, not from a checked-in file).
- **Naming: `partyline_progress`, not `partyline_notify`.** Confirmed on the line that
  `alan_aura_0` is concurrently building a `partyline_notify` cmd/shell **deploy** helper
  in the aura repos. To avoid a cross-repo name/purpose collision, this notebook is
  `notebooks/partyline_progress.py` (a *job* progress reporter), a distinct artifact.

## Architecture

### New components (additive)

```
scripts/provision_partyline_sp.py     # idempotent job-SP create + resolve application_id (SCIM)
scripts/grant_app_access.py           # idempotent CAN_USE grant for the SP on the deployed app
resources/example_progress_job.job.yml# the example job (DAG, parameters, run_as SP, serverless)
notebooks/partyline_progress.py       # ONE reusable widget-driven notebook, backs all tasks
```

`deploy.sh` gains a bootstrap phase (below). No existing source is modified except
`deploy.sh` and the addition of one bundle variable (`partyline_sp_app_id`).

### The reusable notebook (`notebooks/partyline_progress.py`)

A single notebook backs every task. An `action` widget selects behavior:

- `action=start` — register the agent, post a `status` "run started" message, and write
  the returned message id to `taskValues` as `seed_id` (the thread seed).
- `action=step` — read `seed_id` via `dbutils.jobs.taskValues.get`, compute a simulated
  metric, post a `status` **reply** (`reply_to_id = seed_id`) like
  `"work_a: processed 1,000 rows"`, and write its own outcome to `taskValues`.
- `action=finish` — post a `done` reply summarizing per-task outcomes (pulled from
  upstream `taskValues`). Runs with `run_if: ALL_DONE` so that on an upstream failure it
  still posts a `blocked`/failure note and the thread never dangles.

Why one notebook: it shows the widget-input pattern once, and proves that job params flow
straight into `dbutils.widgets`. Task-specific behavior is data (`action` + `task_label`),
not duplicated code.

Auth inside the notebook:
- `WorkspaceClient()` is the job's SP automatically (no secrets).
- Mint the app bearer token from the notebook run context and set `PARTYLINE_TOKEN`;
  resolve the URL via `w.apps.get(app_name).url` (or the `partyline_url` override) and set
  `PARTYLINE_URL`. Then call the client's REST helpers.

### Job DAG (one-thread-per-run)

```
notify_start ──► work_a ──► work_b ──► notify_finish
 action=start    action=step  action=step  action=finish
                                          (run_if: ALL_DONE)
```

- `notify_start` seeds the thread (`seed_id` in taskValues).
- `work_a` / `work_b` post `status` replies threaded onto `seed_id`.
- `notify_finish` posts the `done` summary; always runs so failures are reported.

### Configuration surface (job `parameters:` → widgets)

| Param | Default | Purpose |
|---|---|---|
| `app_name` | `mcp-covasant-partyline` | notebook resolves URL via `w.apps.get(app_name).url` |
| `partyline_url` | *(blank)* | optional explicit URL override (skips app-name lookup) |
| `agent_id` | *(blank)* | blank → notebook derives `job_<job_id>_run_<run_id>`; set → pinned friendly name |
| `owner` | `covasant` | registration metadata |
| `repo` | `covasant-partyline` | registration metadata |
| `branch` | `main` | registration metadata |
| `run_label` | *(blank)* | free-text tag folded into the start message so a human recognizes the run |

`action` and `task_label` are **task-level** parameters (set per task in the job yml),
not job-level — they distinguish the tasks, not the run.

### Identity & run-as

- Job `run_as` = the provisioned SP, referenced as `${var.partyline_sp_app_id}`.
- Layer-1 access is granted **explicitly** on the app (`CAN_USE` for the SP) — least
  privilege; the SP can reach only this app, gaining no broad group membership.
- Layer-2: the SP self-registers its agent_id on first task. No partyline-side setup.

### Deploy automation — explicit bootstrap phase (`deploy.sh`)

Ordered so the grant always runs against a fully-deployed app:

1. (existing) provision Lakebase, migrate schema, build SPA/extension/wheel/skill.
2. **Deploy the bundle app** (creates/updates `mcp-covasant-partyline`).
3. **Create the job SP** — `provision_partyline_sp.py` (idempotent: find-by-display-name,
   else create via SCIM; print `application_id`).
4. **Grant the SP `CAN_USE` on the now-deployed app** — `grant_app_access.py`
   (idempotent).
5. **Deploy/register the job** with `--var="partyline_sp_app_id=<app_id>"` so `run_as`
   binds to the SP, then run.

On a first-ever deploy this creates the app (step 2) before the grant (step 4), satisfying
the ordering constraint. Reruns are no-ops throughout.

> Sequencing note: the job resource's `run_as` needs the SP `application_id` at bundle
> deploy time, but the *grant* needs the app already deployed. This is why the app deploy
> (step 2) and the job deploy (step 5) are **separate** bundle invocations, with SP
> creation + grant sandwiched between. The implementation plan will confirm whether a
> single `databricks bundle deploy` targeting only the app resource, followed by a second
> full deploy, is the cleanest expression, or whether the job is registered via a distinct
> step. Either way the phase order above holds.

## Error handling

- **Notebook → app call fails** (401/timeout): the task logs the failure and, for work
  tasks, still writes its outcome to taskValues; a failed *post* must not silently look
  like success. `notify_finish` reports partial/threaded state. (Fire-and-forget retry is
  a client-package concern, out of scope for the example; the example calls synchronously
  and surfaces errors.)
- **Upstream task fails:** `notify_finish` (`run_if: ALL_DONE`) posts a failure/`blocked`
  note referencing the run, so the thread always terminates visibly.
- **SP already exists / already granted:** provisioning + grant scripts are idempotent
  (find-before-create; catch "already exists"/"duplicate" like the exemplar).
- **App not yet deployed when grant runs:** cannot happen given the phase order (grant is
  step 4, app deploy is step 2).

## Open items to resolve during implementation (flagged, not guessed)

1. **`run_as` under `mode: development`.** Dev targets can force `run_as` to the deploying
   user. If the bundle refuses SP run-as in `dev-eastus2`, fall back to: SP run-as in
   `prod`, deploying-user run-as in dev, gated by target. Confirm via `bundle validate
   --strict` and pick the wiring that validates.
2. **Client-wheel install on a serverless notebook task.** Exact mechanism — `%pip install`
   of the bundled wheel vs. task `environments.dependencies`. Does not change the design;
   lock the reliable one. (The client wheel is already built to `client/dist` by
   `deploy.sh`; confirm the notebook can reach it, or install from the app's hosted
   `/partyline-client-latest`.)
3. **Grant API path.** Confirm the non-interactive app-permissions grant call
   (SDK `w.apps.*` permissions vs. workspace permissions API) and that it is idempotent.

## Testing

- **Unit:** the notebook's pure helpers (agent_id derivation from run context, message
  body construction, taskValues threading logic) extracted so they're testable without a
  live cluster; pytest under `tests/`.
- **Idempotency:** `provision_partyline_sp.py` and `grant_app_access.py` run twice in a
  test → second run is a no-op (assert no duplicate SP, grant unchanged).
- **Live verification (per CLAUDE.md):** run the deployed job once, confirm the thread
  appears on the line (start + 2 step replies + done, all one conversation), then **clean
  up** the test agent/messages from the live line.

## Deliverables

1. `scripts/provision_partyline_sp.py`
2. `scripts/grant_app_access.py`
3. `resources/example_progress_job.job.yml`
4. `notebooks/partyline_progress.py`
5. `deploy.sh` bootstrap-phase additions + `partyline_sp_app_id` bundle variable
6. Tests + a short README section documenting how to run and adapt the example
