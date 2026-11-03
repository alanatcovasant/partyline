# Proposal: One Work Queue — agent work, handoff and a prioritized GitHub intake

**Status:** Draft for discussion; no implementation or deployment authorized by this document.  
**Date:** 2026-11-03  
**Extends:** [PROPOSAL_Work_Ledger_2026-10-30.md](PROPOSAL_Work_Ledger_2026-10-30.md) (items/runs/events, handoff offers, evidence-bound completion) and [PROPOSAL_Validation_Queue_2026-11-02.md](PROPOSAL_Validation_Queue_2026-11-02.md). Those define the item model; this document adds **ownership**, a **ranked unclaimed queue fed from GitHub Issues**, and the rule that makes dropped issues detectable.

## Position

An agent's work list, the handoff log and the validation queue are one thing: **work items with an owner, a state and an append-only history.** A handoff is an owner change on an item; a validation is an item of another kind; the "what should I do next?" list is the unowned items in rank order. Persistence, transfer and fleet visibility fall out of keeping that in Partyline rather than in files.

It is not only a naming change. The deployed `partyline progress` ledger covers persistence and history, but four things are missing (section 2), and the intake queue (section 3) is new.

## 1. What exists today, and what it can't do

| Need | Today | Gap |
|---|---|---|
| Persistent record of what an agent is doing | `progress_tasks` + `progress_updates` (folder-keyed, append-only history) | Keyed by **folder**, so work cannot move between clones or machines |
| Who owns it | `agent_id` column, overwritten by whoever updates last | No ownership; any agent can overwrite another's task |
| Handoff | Free-text `NEXT:` lines in `.handoff/handoff.txt`, mirrored by `add.sh` | No transfer event, no offer/accept, no structured next action |
| A queue of what to do | `queued` status per folder (backlog) | Per folder only; no global order, no priority, no atomic "take the next one" |
| Backlog from GitHub | none | Issues are tracked in people's heads, in `handoff.txt` and in the Aura testing-queue doc |

## 2. Ownership and handoff (the foundation)

Add to the item (the `work_items` shape from the work-ledger proposal; the existing `progress_tasks` migrate into it):

- `owner_agent_id`, separate from `folder`/location. `folder`, `workdir`, `branch` become **location** attributes of the current run, not the identity of the item.
- `next_action` (structured, one line) required when leaving unfinished work, shown by `resume`.
- **Handoff events** instead of overwrites: `offer(from, to|anyone, note)` -> `accept(by)` | `decline` | `expire`. Acceptance is one transaction: owner changes, the item's claims move, the event is logged. A message to another agent stays a notification until accepted.
- **Fencing.** Updates carry the owner and revision they were made against. A non-owner update is refused (409) with the owner named; an explicit `take over` is allowed only when the owner is stale and records who and why.
- **Staleness is derived, never silent.** The line already labels agents `live|idle|stale`. An item whose owner has been stale beyond a threshold shows as `orphaned` and becomes claimable via takeover; nothing auto-completes, auto-cancels or auto-transfers (consistent with the work-ledger proposal).

## 3. The unclaimed queue, populated from GitHub Issues

### 3.1 Model

A **queue** is an ordered set of unowned work items. Named queues: one intake queue per scope (repo or area, for example `aura`, `partyline`), and per-target validation queues (`validation:dev-eastus2`).

An item enters the queue as `kind=task`, `status=ready`, with a `rank`. Fields added: `queue`, `priority_band` (P0..P3 or a smaller agreed set), `rank` (orderable key allowing insertion between two items without renumbering), `source` (`github` | `agent` | `operator`), `source_ref` (`owner/repo#N`), `enqueued_by`, `ranked_by`/`ranked_at`.

Order is **band first, then rank within the band, then age**. Every rank change is an event (actor, before, after, reason), so reordering is auditable and a bad reorder is revertable.

### 3.2 Who can put things in and reorder

- **Any agent** may enqueue an item with a priority band and a one-line reason (`partyline queue add --issue owner/repo#N --band P1 --before W-123 --why "blocks #4700"`), or move an existing one.
- **The operator** (or a nominated triager) has authority: a band change by an operator is marked `operator-set`; agents may not move an operator-set item without a reason, and the item shows the override. This keeps "the order we want" a human decision without making agents ask permission to add work.
- Concurrent reorders resolve by server commit order; both are in the event log. There is no merge step, which is the point.

### 3.3 Pulling work

`partyline work next [--queue aura]` atomically claims the top eligible item (the same `FOR UPDATE SKIP LOCKED` pattern proven in the validation-queue prototype), creates the run and returns the resume packet. Eligibility filters, applied server-side so an agent cannot be handed out-of-scope work:

- **Agent scope.** An agent registers the repos it works in. `next` defaults to that scope. A Partyline-repo agent is never handed an Aura item; crossing scope needs an explicit `--queue`.
- Not `blocked`/`waiting`, dependencies done, and no conflicting claim on the source/path (existing claims stay the mutual-exclusion authority).
- Skipped items stay in place; they are not reordered by being skipped.

Direct claim by name remains possible (`claim-issue`), but it takes the item *out of the queue* instead of creating a second, untracked record.

### 3.4 The anti-drop rule

Issues are missed when they are in no list at all. The design makes that state **detectable and alarming** rather than hoping for discipline:

> **Invariant:** every open issue in a tracked repo is in exactly one place: an open queue entry (`untriaged` | `ready`), an owned in-progress item, or an explicit `parked` entry with a reason.

Mechanics:

1. **Sync.** A reconciler lists open issues for each tracked repo (and, optionally, those matching labels) and upserts them by `source_ref`. A **new** issue enters a top `untriaged` band, ahead of ranked work, so it cannot sit unseen below a long queue. It stays there until someone sets a band (triage) or parks it.
2. **GitHub is authoritative for issue state.** An issue closed in GitHub closes the entry; if an agent owned it, the item flips to `needs attention: closed upstream` instead of vanishing. A reopened issue reopens the entry.
3. **Coverage report** (`partyline queue audit`, also on the dashboard): GitHub-open issues with no entry (should be zero after a sync), entries whose issue is closed, orphaned items, `untriaged` older than a threshold, and owned items with no progress for N days.
4. **Alarm.** Untriaged-too-long and orphaned items post a single `escalation` message to the operator/triager (deduplicated by item and threshold, not per sync), which the existing hooks and the channel already deliver. A silent queue is then evidence there is nothing wrong, not evidence nobody is looking.
5. **Parking is explicit.** "Won't do now" needs a reason and an optional revisit date; parked items stay visible in the audit so they cannot be forgotten.

### 3.5 Where the sync runs

This is the main open design question; both options need the "no manual steps" rule honored:

| Option | How | Trade-off |
|---|---|---|
| **A. Client-pushed** | `partyline queue sync --repo owner/repo` uses the caller's `gh` auth and POSTs issue metadata. Run by the session-start hook and by any agent. | No GitHub secret on the server; works today. But a sync happens only when someone runs it: a quiet fleet could miss issues, which is the failure we are fixing. |
| **B. Server-side scheduled** | A Databricks job (the bundle already ships a retention job) or the app polls the GitHub API with a GitHub App installation token. Webhook delivery can replace polling later. | Guarantees freshness. Needs a GitHub credential stored as an automated, bundle-provisioned secret. A GitHub App for git deploys already exists on the Aura side (PR #4787, `covasant/auraa-app-release`); reusing it versus a separate read-only app is a decision, and automating the secret is a hard requirement, or it is a blocker to flag. |

Recommendation: ship **A** first so the queue and ordering can be used immediately, then **B** to make the guarantee real. The invariant is only enforceable once B exists; until then the audit report says when the last sync ran.

Only issue **metadata** is mirrored (ref, title, labels, state, assignee, updated_at, URL). Issue bodies and comments stay in GitHub.

### 3.6 GitHub Projects as the ordering store (alternative)

GitHub Projects already has ordered boards. Keeping the order there lets humans reorder in the GitHub UI and avoids a second source of ranking. The cost: ordering through GraphQL per agent action, no atomic claim, and no connection to Partyline agent scope or liveness. Recommendation: Partyline holds the authoritative rank (agents write it fast and claim atomically); optionally mirror band/rank to a Project field later for human visibility. Settle this before building (decision 3).

## 4. Folding in the validation queue and the handoff log

- **Validation items** are `kind=validation` items with `target`, `script` and executed-run evidence (the prototype's rules unchanged: only an executed passing run validates). They live in per-target queues and are pulled with the same `next`. The `vq_*` prototype tables should share this model's ownership, claim and event mechanics; whether they share physical tables is an implementation choice, but agents must see **one** list: `partyline work list --mine` shows tasks and validations together.
- **`.handoff/handoff.txt`** stops being a source of truth once session start prints the agent's queue and `resume` packet. `add.sh` dual-write is then retired: milestones are written once, to the ledger.
- **Session start** (hook) prints: your owned items with next actions, offers addressed to you, then the top of your scope's queue and any `untriaged` count. Agents begin from the queue rather than from a file they must locate.

## 5. Phasing

1. **Ownership + handoff on the existing ledger**: `owner_agent_id`, `next_action`, offer/accept/decline, fencing, orphan labeling. Prerequisite for everything else.
2. **Queue + ranking + `work next`**: bands, rank, agent scope, atomic claim, rank events. Client-pushed `queue sync` (option A) and `queue audit`.
3. **Server-side sync (B)** and the alarm for untriaged/orphaned items; dashboard queue and audit views.
4. **Fold in validation items**, session-start queue digest, retire the `add.sh` dual-write.

Each phase ships a validation script with positive and negative cases (exits non-zero on failure), per repository practice.

## 6. Acceptance

1. Agent A offers an item to anyone; agent B accepts; A's later update to it is refused naming B, and the item's claims moved with it atomically.
2. A stale owner's item shows `orphaned`; a takeover records who and why; it never silently changes hands.
3. Two agents run `work next` concurrently on a queue and get different items; an agent whose scope is `partyline` is never handed an `aura` item.
4. After a sync, a newly opened GitHub issue appears at the top as `untriaged`; closing it upstream closes the entry; an issue deliberately omitted from the queue appears in `queue audit`.
5. An untriaged issue older than the threshold produces exactly one escalation, not one per sync.
6. A rank change by an agent is reversible from the event log; an agent move of an operator-set item without a reason is refused.
7. `work list --mine` shows owned tasks and validations together, and no manual file is consulted to resume.

## 7. Decisions to settle

1. **Authority over order:** operator-only reordering, or any agent with the operator override described above?
2. **Priority bands:** how many, with what meaning? (Proposed: P0 drop everything, P1 this sprint, P2 next, P3 backlog.)
3. **Order store:** Partyline rank (recommended) or a GitHub Project as source of truth?
4. **Sync credential:** reuse the Aura GitHub App, or create a read-only app for Partyline? Who provisions the secret so it stays automated?
5. **Tracked repos:** a configured list, or every repo an agent registers against? Which labels exclude an issue from intake (for example `question`, `discussion`)?
6. **Queue scope per agent:** declared at register time, or derived from `repo` and workdirs?
7. **Untriaged threshold and escalation target** (who is paged, how often).
8. **Migration of existing `progress_tasks`:** convert in place (folder becomes location) or leave read-only history and start fresh at phase 1.
