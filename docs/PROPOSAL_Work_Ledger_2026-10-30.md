# Proposal: Partyline Work Ledger

**Status:** Draft for discussion; no implementation or deployment authorized by this document.  
**Date:** 2026-10-30  
**Purpose:** Replace maintained `.handoff/handoff.txt` files with durable, queryable work records shared across agents, vendors, sessions, clones, and machines.

## Recommendation

The requirement makes sense. Add a small work ledger to Partyline if retaining independently launched coding agents is important. First evaluate Beads as the closest task-memory alternative, and Omnigent if adopting a shared orchestration environment is also desirable. Do not build another general project-management platform or require agents to keep both files and database rows current.

The central unit is a **work item that outlives an agent session**. Starting work, reaching a meaningful milestone, encountering a blocker, handing off, and completing work produce structured events. A current-state row makes recovery cheap; event history preserves the explanation and evidence.

GitHub remains authoritative for issue/PR state. Partyline records who is doing the work, where, the latest verified result, and the next action. A merged PR or a finished conversation must not silently become proof that the requested outcome is complete.

## Problem and users

Local handoff logs are useful but tied to a directory. Agents must find the correct file, reconstruct current state from chronological entries, and reconcile contradictory claims. Multiple clones and vendors make this harder. Completed items add noise, and moving old entries into archives creates more maintenance.

The primary users are an operator supervising a fleet and an agent resuming another session's work. Both need to answer:

- What work is active, blocked, waiting for review/deployment, or ready to resume?
- Which agent currently owns it, and in which workspace and branch?
- What changed, what evidence supports that claim, and what should happen next?
- Is the owner unavailable, or is the work actually complete?
- Can another agent take it without conflicting with an existing owner?

## What already exists

Current Partyline source has agents, messages, read checkpoints, and expiring resource claims. Messages already support a summary, detailed payload, branch, path, and issue reference. `claim_work_item` atomically acquires the work target and associated paths using `db.claim_targets`.

Those are useful foundations, but an expiring claim is not a durable task record. The reviewed implementation does not give a claimed work item a persistent lifecycle, current handoff, or milestone history independent of messages and leases.

Relevant source:

- [Models](../covasant-partyline/app/models.py)
- [Claim transaction](../covasant-partyline/app/db.py)
- [REST work-item claims](../covasant-partyline/app/rest.py)
- [Existing claim and presence decisions](adr/2026-08-10-observable-delivery-and-logical-claims.md)

Preserve the distinction: **messages communicate; claims prevent conflicting work; the ledger records durable work state.**

## Proposed user experience

All command names and examples below are proposed interfaces, not commands implemented today.

```text
partyline work start "Fix discovery retries" --issue owner/repo#123 --path .
partyline work progress W-123 --summary "Retry policy implemented" --next "Run deployed validator"
partyline work wait W-123 --reason "Waiting for build" --next "Validate when build X is available"
partyline work finish W-123 --summary "Acceptance passed" --evidence <report-or-PR-URL>
partyline work resume --path .
```

`start` resolves the configured agent identity, repository, machine/workspace, branch and HEAD. Explicit issue references or a trusted integration supply the issue; branch-name parsing can suggest a match but must not silently associate an ambiguous issue. No GitHub issue is required for small local tasks.

Agents provide three things humans and Git cannot reliably infer: what the task means, what was learned, and the next action. Details are optional except where needed to explain a blocker or completion.

`resume` is read-only: it returns a short packet containing objective, current state, owner, location, latest milestone, blockers, next action, and relevant evidence. It does not acquire ownership merely because someone reads it. Multiple matches are listed; the client must not silently pick one.

There is also an explicit stop/leave action for ending a session with unfinished work. A normal final response or process exit ends a run, not necessarily the task. Reassignment uses a durable offer/accept flow; until accepted, a targeted message is only a notification.

### Dashboard

Default views: Active, Needs attention, Waiting, Recently completed, and Search/history. Filters: repository, issue, operator, agent, machine/workspace, branch, status, and age of last meaningful progress. No agent should need to scan the entire fleet to resume one task.

Show both last activity and last substantive progress. A live agent with an old milestone can still be stuck. Offer a concise table by default and load details/history on demand.

## Information to capture

| Information | Why it is useful | How it is populated |
|---|---|---|
| Stable work ID, title, short current summary | A task remains identifiable after folder, branch, or agent changes | Server ID; agent supplies text |
| Objective and acceptance criteria | Defines what completion means | Agent/operator, editable with history |
| Detailed notes | Findings, decisions, failed approaches, caveats | Agent writes at meaningful milestones |
| Next action | Makes handoff actionable | Required when leaving unfinished work |
| Status and reason | Distinguishes active work from waiting or blockage | Explicit agent/operator update |
| Agent ID, operator, session/run ID | Separates a persistent identity from one execution | Config plus generated run ID |
| Harness, model/provider when known | Supports cross-vendor recovery and diagnosis | Runtime metadata; unknown is valid |
| Canonical repository identity | Connects clones without relying on folder names | Git remote or explicit configuration |
| Workspace ID, machine ID, absolute path | Identifies the actual working directory | Client context; no assumption that paths are globally unique |
| Branch and HEAD commit | Records which source state an update describes | Captured per run and milestone |
| Issue and PR references | Connects work to the external delivery workflow | Explicit links or verified integration |
| Blocker, dependency, unblock condition | Enables a useful ready-to-work queue | Agent/operator; links to other work IDs |
| Evidence and delivery stage | Distinguishes implementation, tests, merge, deployment and acceptance | Links plus commit/build/environment scope |
| Server timestamps, version, event source | Provides ordering and prevents silent overwrites | Server-generated |
| Parent work item and priority | Groups an epic and helps choose the next task | Optional, with simple defaults |

The highest-value additions beyond the requested fields are **next action, acceptance criteria, blockers, evidence, and session identity**. Cost estimates, percentages complete, full transcripts, and time sheets are optional later features; they should not burden v1.

An absolute path is meaningful only within a machine/workspace. Branch names change and are not task IDs. One issue may have multiple work items, such as implementation, review, and deployed validation; one work item may link multiple issues or PRs.

The multi-machine design also needs machine/workspace-qualified physical claim keys. Two machines can both have `C:/Code/Aura`; that must not create a false filesystem conflict. Logical issue/source claims remain shared across machines. Introduce this with an explicit compatibility mapping for current path-only claims rather than changing their meaning silently.

## State and completion

Suggested task states: `ready`, `active`, `waiting`, `blocked`, `paused`, `done`, `cancelled`.

- `waiting`: an expected external step is pending, with an unblock condition.
- `blocked`: progress needs intervention or a prerequisite that is not currently available.
- `paused`: intentionally stopped, without implying failure or completion.
- `done`: the declared acceptance criteria have been met, with an outcome and evidence.
- Reopening is explicit and preserves the earlier completion event.

Delivery stage is a separate, optional field: implementation, review, deployment, validation. A task can be `waiting` at deployment stage without being done. Task-specific acceptance governs completion; a coding-only task may legitimately finish before deployment if its scope says so.

Agent presence, run termination, lease expiry and task status are independent. A crash leaves the task unfinished and the run interrupted/stale. Do not auto-complete, auto-cancel or silently transfer its work.

## Storage design

Use the existing Lakebase Postgres service. Agents use REST or the CLI, with opt-in MCP parity; they never require direct database access.

| Table | Role |
|---|---|
| `work_items` | Current objective, summary, details, status/stage, next action, owner/run reference, timestamps and revision |
| `work_runs` | Each execution attempt: agent/session, harness/model, machine/workspace/path, repository, branch, HEAD, start/end/outcome |
| `work_events` | Append-only milestones and transitions: actor/run, event type, summary/details, context snapshot, server sequence and idempotency key |
| `work_links` | Issue, PR, commit, build and evidence links; parent/dependency relations with indexed target IDs where appropriate |

Keep the current summary and next action in `work_items`; do not ask an LLM to reconstruct them from history on every read. Events preserve previous values and context. Text fields can contain Markdown; structured fields drive queries and automation. Store artifact references rather than large logs or credentials.

Start with ordinary indexed tables and filtered views. A partial index on nonterminal items plus indexes for repo, owner, last progress and link targets supports the common queries. Paginate history and offer `after_event_id` for incremental recovery. Validate dependency cycles when adding blocking edges.

### Correctness and ownership

1. A start/resume-for-work operation must acquire the needed claims and create/update the work record in one server transaction. Refactor transaction boundaries around the existing claim helper rather than calling a committed claims operation and hoping a second write succeeds.
2. Each mutation carries a client operation UUID for retry deduplication. The current row and its event commit together. Reusing a key with different input is rejected.
3. Revision checks and an active run/fencing token reject late writes from replaced sessions. Stable names such as `alan_aura_1` alone cannot distinguish two executions using the same identity.
4. Existing issue/source/path claims remain the mutual-exclusion authority. A ledger label is not permission to edit. Starting a second task under the same issue must respect its claim policy.
5. Handoff offers do not transfer ownership. Acceptance and the necessary claim changes are atomic; if claims conflict, acceptance fails without a partial transfer.
6. Use server commit order for events; preserve client occurrence time as additional metadata. A delayed offline event cannot silently revert current state.
7. Publish SSE notifications after commit. A milestone generates its concise Partyline message server-side with a unique event reference, avoiding a separate manual post and duplicate messages on retry. Readers can recover by querying the ledger if a notification is missed.

Authentication and attribution remain distinct: authenticate the caller through the existing app boundary; record the agent's self-declared identity as attribution. Do not treat knowledge of an agent ID as proof of ownership. Mutation authorization must enforce the authenticated operator's allowed scope, with auditable administrative reassignment.

## Simplification and automation

### One write, several useful results

A milestone updates current state, appends history, and supplies the coordination announcement. The operator should not maintain a handoff file, a chat post, and an issue comment for the same event. External GitHub updates remain opt-in and use explicit integration permissions.

### Portable core; optional hooks

CLI/REST is the common denominator for all vendors. Hooks and launchers auto-capture context, show active work at startup, and remind an agent to save unfinished progress. An unsupported harness can use the same workflow without a new integration.

Hook callbacks cannot reliably determine when a semantic task starts or finishes. Do not create a new work item on every prompt or mark it done on every Stop event. Do not call an LLM for every shell command. Agent instructions supply the semantic milestone; adapters supply identity/context and best-effort reminders.

### No manual archiving

On `done`/`cancelled`, an item disappears from active views immediately and remains searchable. Recently completed is a bounded time-window view. Reopening makes it active again. No moving rows to a second table, editing archive files, or scheduling an archive job is needed for v1.

Removing archive chores does not eliminate database capacity and backup requirements. Keep summaries, evidence links and transition history durable. Large attachments can have a separate documented retention policy later. Do not add LLM summarization or destructive expiry just to keep the active list short.

### Offline behavior

Reuse the existing outbox pattern where practical for unsent progress events. A bounded local retry queue is transport state, not a second handoff source of truth. Clearly report `queued locally` versus `committed to server`. Replay preserves operation IDs and handles revision conflicts explicitly. Claim-bearing starts fail closed when ownership cannot be acquired; an offline queue is not a substitute for mutual exclusion.

## Relationship to GitHub

GitHub owns issue/PR state. A future webhook or poller can append verified merge/reopen events and unblock waiting tasks. Those events include PR URL and commit identity. They do not automatically assert that a deployment or runtime test passed.

Use a minimal mapping rather than unrestricted bidirectional sync: issue state is authoritative in GitHub; execution status and handoff context are authoritative in the ledger. A GitHub issue closing may require reconciliation when linked work remains unfinished; it must not erase that work.

## Existing tools: build, adopt or integrate?

The following assessment uses public primary sources reviewed on 2026-10-30. It is not an installation or proof-of-concept result. Product capabilities and deployment support should be rechecked before adopting.

| Option | What it could supply | What still needs proving for this requirement |
|---|---|---|
| Extend Partyline | Shared work ledger beside existing identity, claims, transport and UI | We own implementation, authorization, migrations, support and adapter upkeep |
| Omnigent | Multi-harness orchestration, persistent sessions, multi-agent worktrees and review workflows | Durable business-work records spanning sessions; ingestion from independently launched agents; exact issue/workspace/identity mapping |
| Beads | Persistent agent task memory, dependency graph, claims and agent-facing CLI | Shared-server operational fit, required metadata and centralized fleet views; coexistence with Partyline claims |
| GitHub Issues + Projects | Existing issue/PR workflow, custom fields, status automation and automatic archive views | Agent/session identity, machine-local paths, milestone ingestion, and runtime claim enforcement need integration |

**Omnigent:** A genuine candidate if we want a shared agent execution environment as well as tracking. Polly documents multi-vendor delegation into separate harnesses/worktrees. Omnigent documents database-backed session history and has session/task status and GitHub-resource concepts in its API. These are meaningful overlaps. They do not establish that the proposed standalone, cross-session ledger for arbitrary external CLIs is available unchanged; that gap must be demonstrated, not assumed. Native Windows terminal-wrapper limitations are relevant to this fleet. See [Polly](https://omnigent.ai/docs/use/builtin-agents/polly), [database](https://omnigent.ai/docs/deploy/database), [API schema](https://raw.githubusercontent.com/omnigent-ai/omnigent/main/openapi.json), and [official repository/Windows limitations](https://github.com/omnigent-ai/omnigent).

**Beads:** The closest functional comparison for replacing Markdown handoffs. Its current documentation describes Dolt-backed persistent task memory, dependency-aware work, atomic claims and history. The default embedded mode is single-writer; a server mode supports multiple concurrent writers, and an HTTP backend is documented as experimental. Evaluate these choices against our shared-service requirement rather than assuming a per-repo install gives central fleet coordination. See the [official repository](https://github.com/gastownhall/beads).

**GitHub Projects:** Useful if reducing additional software is the priority and most work is issue-bound. Built-in automations can update status and automatically archive matching items while retaining field data. It still needs a runtime-context adapter and does not replace Partyline's filesystem/source leases. See [built-in automations](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations) and [automatic archiving](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/archiving-items-automatically).

Databricks also documents managed Omnigent as beta, with local/VM and cloud execution-host options. That makes it particularly relevant here, but availability in our workspace was not checked. See [Databricks overview](https://developers.databricks.com/docs/omnigent/overview).

**Decision rule:** If the fleet must keep its existing native CLI workflows, prefer a small Partyline ledger or a Beads integration. If moving execution into Omnigent is desirable, evaluate whether its durable session/task model can be the authority and Partyline can remain the coordination bridge. Avoid keeping two independently editable authoritative task stores.

### Short evaluation before implementation

Use the same acceptance scenario for Partyline's proposed design, Beads and Omnigent:

1. Agent A starts issue-linked work in a Windows checkout, records progress and exits unexpectedly.
2. Agent B, on another vendor and machine, finds the task by issue and gets the correct next action without opening a transcript.
3. B cannot take ownership while A still has a valid conflicting claim; an explicit takeover is recorded when allowed.
4. A later resumes and cannot overwrite B's newer state with a delayed update.
5. B completes the scoped acceptance; the item disappears from active lists but its evidence/history remain searchable.
6. The same works for an agent launched outside the orchestration platform, or that limitation is explicitly accepted.
7. No manual archive, Markdown maintenance, duplicate task entry, or default replay of full conversations is required.

Choose the existing tool if it meets these requirements with less ongoing operational work. Otherwise, use the narrow Partyline extension; do not build a new agent runner to solve this task-ledger need.

## MVP and migration

**Must have:** four-table ledger, lifecycle/event API with CLI parity, concise resume query, issue/repo/workspace filters, ownership concurrency controls, evidence/next-action fields, summary-first dashboard, filtered terminal items, and idempotent updates. Provide opt-in MCP tools over the same service semantics.

**Next:** GitHub event integration, richer dependency/ready queues, optional Omnigent run links, and operator reporting. Avoid mandatory billing, scheduling, agent spawning, full-transcript storage and autonomous reassignment in v1.

Migration should be one-way:

1. Add schema/API/client support without changing existing claim behavior or deleting files.
2. Provide an idempotent importer keyed by source file and entry fingerprint. Preserve original text and timestamp. Treat old `ENDED` rows as historical run-end evidence, not automatic proof of task completion; ambiguous mappings need reconciliation.
3. Offer a temporary `add.sh` compatibility adapter that forwards to the API. Do not keep appending to both file and database as the normal workflow. Optional generated exports are read-only snapshots.
4. Pilot one repository with two vendors, then a second machine. Change agent instructions to use work start/progress/leave/finish and concise resume context.
5. Retain original files as historical artifacts during migration. No automatic deletion or global rollout is part of this proposal.

Infrastructure and schema changes must use automated deployment. No manual UI provisioning steps. Validate a future implementation against the deployed app under the explicitly selected profile and remove disposable test records.

## Acceptance and success measures

- A task can be resumed after process death by another vendor with its last committed summary and next action intact.
- Simultaneous conflicting starts produce one successful owner and an explicit refusal; no partial claims or orphan active run.
- Retrying the same milestone creates one event and one coordination announcement.
- An old run or stale revision cannot overwrite a newer owner/state.
- Changing branch, path or machine preserves the task ID and earlier context.
- Terminal state leaves active views without maintenance; reopening restores it with history intact.
- A stopped session, expired claim, merged PR or offline update never silently proves acceptance.
- When disconnected, queued progress is visibly distinguished from server-committed progress.
- Startup context is bounded: proposed default of five relevant summaries, 200 words total, with details requested separately.
- Pilot targets, to be measured rather than assumed: zero manual archiving; no dual-written handoff files; at most one semantic command per milestone; a useful resume packet from one CLI request; no duplicate events in retry tests.

## Decisions to settle before implementation

1. **Workflow:** Must independently launched native CLIs remain first-class? Proposed default: yes.
2. **Authority:** Partyline ledger, Beads, or Omnigent as the single execution-work authority? Decide after the short evaluation.
3. **Scope:** Central fleet ledger with repository-filtered views, including work without a GitHub issue? Proposed default: yes.
4. **Completion:** Task-specific acceptance with evidence, rather than equating session end with done? Proposed default: yes.
5. **Ownership:** Explicit operator-authorized takeover with run fencing; no takeover based on silence alone? Proposed default: yes.

These are design defaults for discussion, not choices already approved by the operator.
