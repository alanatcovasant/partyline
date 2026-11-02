# Proposal: Partyline Validation Queue

**Status:** Draft for discussion; no implementation or deployment authorized by this document.  
**Date:** 2026-11-02  
**Purpose:** Replace the shared `docs/DEPLOYED_TESTING_QUEUE.md` in the Aura repo as the system of record for "code-complete, awaiting validation on a deployed target", so state stops living in a merge-conflicted Markdown file.  
**Builds on:** [PROPOSAL_Work_Ledger_2026-10-30.md](PROPOSAL_Work_Ledger_2026-10-30.md) (items/runs/events, evidence-bound completion) and the deployed `partyline progress` ledger. Beads, Omnigent and GitHub Projects were assessed there; this document adds MAC and does not repeat that comparison.

## Problem

Measured on the Aura repo on 2026-11-02:

| Measure | Value |
|---|---|
| File size | 29,692 lines, 1,012 H2 sections |
| Sections by state | 468 PENDING, 275 VALIDATED (kept, so the file only grows), 24 VALIDATION FAILED, 19 BLOCKED, 18 PARTIALLY VALIDATED |
| Churn | 300 commits touching it in 30 days |
| Conflict handling | 45 of the latest 300 commits are "UNION" merges re-inserting a section over `develop` |
| Validation scripts | 107 in `scripts/validate/` |

One file carries three jobs: **state** (pending/validated/failed), **procedure** (how to validate, how the check can mislead) and **history**. State is encoded in heading text and edited by every fix PR and every validation, so it conflicts structurally. "What is pending on dev-eastus2 for me?" means grepping 30k lines.

Draining stalls on coordination more than on the checks themselves: nobody can see which items wait on the same target, deploy or identity, and two agents can validate the same item.

## Proposal

Add a validation-queue item type to the Partyline work ledger. Split the three jobs:

| Job | Home |
|---|---|
| State and history | Partyline ledger (append-only events) |
| Procedure and "ways this can mislead" prose | One file per issue in the repo: `docs/validation/<issue>.md`, beside `scripts/validate/<issue>.py` |
| Item metadata | The queue item (issue ref, PR, target, script command, expected exit codes) |

### Item lifecycle

`enqueued` -> `claimed` -> `validated` | `failed` | `inconclusive` | `blocked`

- **enqueue** (by the fix PR's agent): issue ref, PR, branch, target(s), script command, link to the procedure file.
- **claim** (by a validator): atomic, reuses Partyline claims. A second validator is refused and shown the holder.
- **record-run**: target, deployed commit, script exit code (0 PASS, 1 FAIL, 2 INCONCLUSIVE), output tail, agent id, timestamp. Runs are append-only.
- **state is derived from runs, not typed in.** `validated` requires a passing run on the item's target at a deployed commit that contains the fix. A FAIL or INCONCLUSIVE run reopens the item without editing any file.
- **blocked-on**: an item can name what it waits for (a target deploy, an identity, another item). `alan_build_1` posting "build X is live on Y" can list what that unblocks.

### CLI sketch (on the existing ledger)

```
partyline vq enqueue  --issue owner/repo#1373 --target dev-eastus2 --script "python scripts/validate/1373.py --tenant-id T" --proc docs/validation/1373.md
partyline vq list     --target dev-eastus2 --status pending [--blocked-on deploy]
partyline vq next     --target dev-eastus2        # claim + print the command and procedure link
partyline vq record   --issue owner/repo#1373 --target dev-eastus2 --exit-code 0 --commit <sha> -- <output tail>
partyline vq show     --issue owner/repo#1373     # runs, history, current state
```

### Evidence binding

The one thing no surveyed tool does: **the exit code and deployed commit are captured by the CLI running the script, not typed by the agent.** `vq run --issue N` executes the item's recorded command, captures exit code and tail, resolves the deployed commit (from the target's active deployment), and posts the run. A hand-recorded run is allowed but labelled `self-reported`; only `executed` runs can move an item to `validated` by default. This directly addresses the "agent asserts PASS" failure mode.

## Prior art

| Project | Overlap | Gap for this requirement |
|---|---|---|
| [MAC (`mac-agent`)](https://pypi.org/project/mac-agent/) 1.2.1, MIT | Tasks, dependencies, claims, `next`/`done` atomic ops, handoffs, review packets, MCP + HTTP. Closest schema. | Local SQLite per project. Quality evidence is **agent-supplied**: `done --quality-command "pytest -q" --quality-status passed` asserts the result, nothing runs or checks it. No target or deployed-commit concept. |
| pi-ensemble | File-based blackboard, mailbox, claims, audit | Local files; explicitly not an orchestrator |
| pi-agent-coordination | Sparse ledger: claims, handoffs, blockers, review, merge summaries | Skill only, no runtime or evidence check |
| Claude Code Agent Teams | Shared task list with pending/in-progress/completed | Not open source; three states, no target/evidence |
| Omnigent | Build- and review-time orchestration across harnesses | No post-deploy validation model found |
| Beads, GitHub Projects | See the work-ledger proposal | Same gaps: no target-bound, executed evidence |

Reusable ideas from MAC: the `next` (claim + start + instructions) and `done` (evidence + handoff + complete) single-call ergonomics, `ready-tasks` filtered by dependency, TTL expiry of stale claims, and the review-packet as a one-call summary for a human. Take the shapes, not the code, since its storage and trust model differ.

Not duplicated: claims, append-only events and handoff views already exist in Partyline.

## What this does not do

- It does not make validation cheaper to execute. A target, an identity and a deployed build are still required. It makes the waiting visible (for example "N items wait on the same deploy") and prevents duplicate or unverified validations.
- It does not replace the per-issue procedure prose; that moves to repo files.
- GitHub stays authoritative for issue/PR state. Closing an issue is a separate step the validator takes after `validated`.

## Migration (no flag day)

1. **Split the doc, independent of Partyline.** Generate `docs/validation/<issue>.md` per section and leave a generated index. This alone removes the merge conflicts. Owned by the Aura repo agents.
2. **Backfill.** Import the 468 PENDING and 24 FAILED/BLOCKED headings as items; leave the 275 VALIDATED as read-only history (import with `source: legacy-doc`, no runs).
3. **Dual-write for one sprint** (same policy as `add.sh`): the fix PR template calls `vq enqueue`; a CI check fails if a new `scripts/validate/*.py` has no queue item.
4. **Cut over**: stop editing the doc's headings; render it from the ledger if a human-readable view is still wanted.

## MVP

Smallest useful slice: `enqueue`, `list`, `claim` (via `next`), `record` with exit code and commit, derived state, and `show`. Defer `vq run`, blocked-on and auto-unblock views until the first four are in use. The server work is one table family (`vq_items`, `vq_runs`) plus REST routes mirroring the progress ledger; the schema migration needs the privileged deploy step (see the schema-migration constraint for the app service principal).

## Acceptance

1. Two validators run `vq next` against the same target concurrently; they get different items, never the same one.
2. A hand-recorded PASS leaves the item at `self-reported`, not `validated`; an executed passing run on the right target and a commit containing the fix moves it to `validated`.
3. A FAIL run reopens a previously `validated` item and it shows in `vq list --status failed`.
4. `vq list --target dev-eastus2 --status pending` answers in one call with no file read.
5. Two fix PRs enqueue items at the same time and neither conflicts.
6. A post-deploy validation script ships with the change: positive (queue lifecycle works end to end) and negative (hand-recorded PASS cannot validate, second claim refused), PASS/FAIL printed, non-zero exit on failure.

## Decisions to settle

1. Should `validated` require an `executed` run, or accept `self-reported` with a human approver?
2. Do items key on issue only, or issue + target (one item per target per issue)? This proposal assumes issue + target.
3. How is the deployed commit resolved for non-git-sourced targets (older Apps deployed by file sync)?
4. Where is the repo-file split owned, and does the Aura repo accept the `docs/validation/` layout?
5. Does MAC's schema warrant a compatibility export, or is a design reference enough?
