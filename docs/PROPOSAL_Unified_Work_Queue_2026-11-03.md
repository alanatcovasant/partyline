# Proposal: One Work Queue — agent work, handoff and a prioritized GitHub intake

**Status:** Draft for discussion; no implementation or deployment authorized by this document.  
**Date:** 2026-11-03  
**Extends:** [PROPOSAL_Work_Ledger_2026-10-30.md](PROPOSAL_Work_Ledger_2026-10-30.md) (items/runs/events, handoff offers, evidence-bound completion) and [PROPOSAL_Validation_Queue_2026-11-02.md](PROPOSAL_Validation_Queue_2026-11-02.md). Those define the item model; this document adds **ownership**, a **ranked unclaimed queue fed from GitHub Issues**, and the rule that makes dropped issues detectable.

## Position

An agent's work list, the handoff log and the validation queue are one thing: **work items with an owner, a state and an append-only history.** The owner is a **seat**: an agent in a specific folder, because that folder holds the agent's memory and working context. A handoff to a different seat is therefore the exception, and the default for an interrupted item is to resume in the seat that had it; a validation is an item of another kind; the "what should I do next?" list is the unowned items in rank order. Persistence, transfer and fleet visibility fall out of keeping that in Partyline rather than in files.

It is not only a naming change. The deployed `partyline progress` ledger covers persistence and history, but four things are missing (section 2), and the intake queue (section 3) is new.

## 1. What exists today, and what it can't do

| Need | Today | Gap |
|---|---|---|
| Persistent record of what an agent is doing | `progress_tasks` + `progress_updates` (folder-keyed, append-only history) | Folder-keying is right for resume (see 2.1) but the folder label is only the directory name, so two machines with an `aura_2` collide |
| Who owns it | `agent_id` column, overwritten by whoever updates last | No ownership; any agent can overwrite another's task |
| Handoff | Free-text `NEXT:` lines in `.handoff/handoff.txt`, mirrored by `add.sh` | No transfer event, no offer/accept, no structured next action |
| A queue of what to do | `queued` status per folder (backlog) | Per folder only; no global order, no priority, no atomic "take the next one" |
| Backlog from GitHub | none | Issues are tracked in people's heads, in `handoff.txt` and in the Aura testing-queue doc |

## 2. Ownership and handoff (the foundation)

Add to the item (the `work_items` shape from the work-ledger proposal; the existing `progress_tasks` migrate into it):

- `owner_agent_id` **and** `home` (the seat: agent id plus a workspace-qualified folder, see 2.1). The folder stays first-class; it is not demoted to a location attribute. `workdir` and `branch` remain attributes of the current run.
- `next_action` (structured, one line) required when leaving unfinished work, shown by `resume`.
- **Handoff events** instead of overwrites: `offer(from, to|anyone, note)` -> `accept(by)` | `decline` | `expire`. Acceptance is one transaction: owner changes, the item's claims move, the event is logged. A message to another agent stays a notification until accepted.
- **Fencing.** Updates carry the owner and revision they were made against. A non-owner update is refused (409) with the owner named; an explicit `take over` is allowed only when the owner is stale and records who and why.
- **Staleness is derived, never silent.** The line already labels agents `live|idle|stale`. An item whose owner has been stale beyond a threshold shows as `orphaned` and becomes claimable via takeover; nothing auto-completes, auto-cancels or auto-transfers (consistent with the work-ledger proposal).

### 2.1 Folder affinity: resume in the seat that has the memory

A folder carries state no ledger row can reproduce: the agent's memory files for that directory, the clone's `.partyline` identity, local notes and the working tree. Until an orchestrator (for example Omnigent) owns sessions, the cheapest continuity is to keep an issue in the same folder, and to let the same agent resume it after an interruption such as a token-window reset. So:

- **An interruption is not a release.** A session ending with unfinished work leaves the item owned by its seat in state `interrupted` (derived, not typed). That folder's next session finds it first via `resume`. This matches today's rule that a folder has one current task, which is the resume point.
- **Items remember their home.** The first seat to work an issue becomes its `home`, retained across interruptions. Follow-ups tend to return to the same place: review fixes, a reopened issue, the deployed validation of the fix, and other issues under the same epic or parent.
- **Affinity is a soft reservation, never a lock.** A homed item is reserved for its home seat for a grace period that **shrinks as priority rises**, following the Fibonacci series (table below). After that, or on an explicit release, it re-enters the global pool. Another seat can take it earlier only by an explicit takeover. Without the expiry, affinity recreates the dropped-issue problem: an item stuck behind a folder nobody opens.
**Grace period by priority band** (decided 2026-11-03: the more urgent the item, the less it may wait for its home seat):

| Band | Meaning | Grace (hours since the home seat's last activity on the item) |
|---|---|---|
| P0 | drop everything | 3 |
| P1 | this sprint | 5 |
| P2 | next | 8 |
| P3 | backlog | 13 |
| P4 | icebox (optional fifth band) | 21 |

The series continues (34, 55, ...) if more bands are ever added. Rules around it:

- **The clock starts at the home seat's last activity on the item** (a progress note, a claim refresh or the session that interrupted), not at enqueue.
- **Re-banding re-evaluates immediately.** Raising an item to P0 that has been idle 4h releases it at once; lowering it extends the reservation only from the last activity, so priority changes cannot be used to hold an item hostage.
- **A stale home agent shortens it.** If the home seat's agent is `stale` on the line (not merely idle), the reservation ends at the next sweep instead of waiting out the grace. The grace is the upper bound.
- **Release is announced.** A reservation that lapses for P0 or P1 posts one message naming the item and the former home, so a human can see it moved; lower bands show up only in the audit.
- **Wall-clock hours, not working hours,** for simplicity. A P3 reserved at the end of the day therefore lapses roughly overnight; that is acceptable for backlog work, and a business-hours clock can be added later if it proves too aggressive.
- Untriaged items have no home and no grace.

- **Moving seats is allowed and visible.** The resume packet for a new seat states that the item's home is elsewhere and that the memory there did not move, so the new agent leans on the item's history and structured notes (objective, decisions, next action) instead of assuming context. This is why `next_action` and decision notes are required fields, not nice-to-haves.
- **Seat identity must be unambiguous.** Today the folder is the directory name. Qualify it with a machine/workspace id (as the work-ledger proposal already requires for claims) so `aura_2` on two machines is two seats. The agent id convention is already per clone, so the pair `(agent_id, workspace folder)` is stable enough to be the seat key.
- **Orchestrator-ready.** `home` is stored as an opaque seat reference. If Omnigent or another runner later owns sessions and worktrees, the seat becomes that session/worktree, and the queue logic does not change.

## 3. The unclaimed queue, populated from GitHub Issues

### 3.1 Model

A **queue** is an ordered set of unowned work items. Named queues: one intake queue per scope (repo or area, for example `aura`, `partyline`), and per-target validation queues (`validation:dev-eastus2`).

**Triage gate** (decided 2026-11-03): an issue is available for the queue only once it has a **severity** and an **effort estimate**. Until then it is `untriaged`: visible in intake and audited, but not pullable by `work next`. Once both are set it is `triaged`, and enters the queue.

- **Severity** `S1..S4` (S1 critical: broken or blocking; S2 major; S3 minor; S4 cosmetic or nice-to-have). **Effort** in Fibonacci points `1, 2, 3, 5, 8, 13` (anything larger needs splitting before it is triaged). The scales are proposed; see decision 12.
- **Who triages:** anyone. A human sets them in GitHub (labels such as `severity:S2`, `effort:5`, read at sync; GitHub wins when present), or an agent estimates them with `partyline queue triage --issue owner/repo#N --severity S2 --effort 5 --why "..."`. Agent estimates are recorded `triage_source: agent` and show as such, so a human can confirm or correct them. Agent estimates stay local to Partyline unless a later phase writes them back to GitHub (that needs a write credential, which is not assumed).

On triage the server **proposes a position**: band from severity (S1 -> P0, S2 -> P1, S3 -> P2, S4 -> P3; P4 icebox only by explicit parking), then within the band **smaller effort first, then older first** (cheap work clears before large work). The proposal is effective immediately; no one has to approve it for it to be workable.

Fields added: `queue`, `severity`, `effort`, `triage_source` (`github` | `agent` | `human`), `priority_band`, `rank` (orderable key allowing insertion between two items without renumbering), `order_state` (`proposed` | `confirmed` | `pinned`), `source` (`github` | `agent` | `operator`), `source_ref` (`owner/repo#N`), `enqueued_by`, `ranked_by`/`ranked_at`.

Order is **band first, then rank within the band, then age**. Every change to severity, effort, band or rank is an event (actor, before, after, reason), so reordering is auditable and a bad reorder is revertable.

### 3.2 Who proposes the order, who decides (non-blocking)

**Agents propose; a human decides; nothing waits for the decision.**

- **Proposals take effect at once.** Any agent can propose a band or position for a triaged item (`partyline queue move --issue owner/repo#N --band P1 --before W-123 --why "blocks #4700"`). It applies immediately as `order_state: proposed`, with the reason recorded. Work is never held for approval.
- **A human reviews on their own schedule.** `partyline queue review` (and a dashboard view) lists items whose order is still `proposed`, oldest first. The human confirms them in bulk (`confirmed`) or sets a band or rank (`pinned`). Review age appears in the audit; an unreviewed proposal never expires, never blocks, and never changes the order by itself.
- **A human's decision outranks agents without freezing them.** A `pinned` item's order does not change when an agent proposes otherwise: the proposal is stored as a pending proposal beside it (visible in the review list) and the pinned order stays in force until the human acts. This is the only case where an agent's proposal does not take effect, and it exists because a person has already decided.
- **No agent needs permission to add work.** Moving a triaged item in the queue is an agent action; only changes to a `pinned` item need a human.
- Concurrent proposals resolve by server commit order; both are in the event log. There is no merge step, which is the point.

### 3.3 Pulling work

`partyline work next [--queue aura]` atomically claims one item (the same `FOR UPDATE SKIP LOCKED` pattern proven in the validation-queue prototype), creates the run and returns the resume packet. It picks in this order, so affinity is honored without starving the pool:

1. **Your own interrupted or active item** in this seat: resume, do not start something new.
2. **Items reserved to this seat** (home = this seat, inside the grace period), in rank order.
3. **The global pool**, in band/rank order, **skipping items reserved to another seat** until their grace period lapses.

Eligibility filters, applied server-side so an agent cannot be handed out-of-scope work:

- **Agent scope.** An agent registers the repos it works in. `next` defaults to that scope. A Partyline-repo agent is never handed an Aura item; crossing scope needs an explicit `--queue`.
- `triaged` (severity and effort set), not `blocked`/`waiting`, dependencies done, and no conflicting claim on the source/path (existing claims stay the mutual-exclusion authority).
- Skipped items stay in place; they are not reordered by being skipped. The audit lists items waiting only on another seat's reservation, and how long, so a reserved item cannot hide.

Direct claim by name remains possible (`claim-issue`), but it takes the item *out of the queue* instead of creating a second, untracked record.

### 3.4 The anti-drop rule

Issues are missed when they are in no list at all. The design makes that state **detectable and alarming** rather than hoping for discipline:

> **Invariant:** every open issue in a tracked repo is in exactly one place: an open queue entry (`untriaged` | `ready`), an owned in-progress item, or an explicit `parked` entry with a reason.

Mechanics:

1. **Sync.** A reconciler lists open issues for each tracked repo (and, optionally, those matching labels) and upserts them by `source_ref`. A **new** issue enters the `untriaged` intake list, shown ahead of the ranked queue so it cannot sit unseen below a long one. It is not pullable. It stays there until someone triages it (severity and effort, see 3.1) or parks it; on triage it joins the queue at its proposed position.
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
- **Who validates a fix** (decided 2026-11-03): the builder's seat may validate its own fix, but another seat is preferred. This is the mirror image of folder affinity: for validation items the builder's seat is the one that is *deprioritized*.
  - A validation item records `built_by` (the seat that owned the fix item; the prototype's `enqueued_by` is the closest field today).
  - `work next` on a validation queue skips items built by the caller's seat while the **independence grace** runs. It reuses the same Fibonacci table as affinity (P0 3h, P1 5h, P2 8h, P3 13h, measured from when the item became ready), so there is one timing concept to learn. Once it lapses, the builder's seat may take it rather than letting the item wait.
  - Each run records the validator seat, and the item shows `independent: true|false`. A builder-validated item is fully `validated` (it still needs an executed passing run); the flag makes it findable, so an operator can spot-check or re-queue those items for another seat.
  - Independence is at **seat** level (a different agent in a different folder). It does not guarantee a different model or vendor; that would need `harness`/`model` on the seat and is left as a later refinement.
- **`.handoff/handoff.txt`** stops being a source of truth once session start prints the agent's queue and `resume` packet. `add.sh` dual-write is then retired: milestones are written once, to the ledger.
- **Session start** (hook) prints: your owned items with next actions, offers addressed to you, then the top of your scope's queue and any `untriaged` count. Agents begin from the queue rather than from a file they must locate.

## 5. Phasing

1. **Ownership + handoff on the existing ledger**: `owner_agent_id`, `next_action`, offer/accept/decline, fencing, orphan labeling. Prerequisite for everything else.
2. **Queue + ranking + `work next`**: bands, rank, agent scope, atomic claim, rank events. Client-pushed `queue sync` (option A) and `queue audit`.
3. **Server-side sync (B)** and the alarm for untriaged/orphaned items; dashboard queue and audit views.
4. **Fold in validation items**, session-start queue digest, retire the `add.sh` dual-write.

Each phase ships a validation script with positive and negative cases (exits non-zero on failure), per repository practice.

## 6. Acceptance

0. A session ends mid-item (simulated token-window reset); the same seat's next `work next` returns that item before anything else, with its next action; a different seat's `work next` skips it during that band's grace (3h for P0 ... 13h for P3) and takes it afterward, and its packet says the home memory did not move. Raising the band of an idle item past its new grace releases it immediately; a stale home agent releases it before the grace ends.
1. Agent A offers an item to anyone; agent B accepts; A's later update to it is refused naming B, and the item's claims moved with it atomically.
2. A stale owner's item shows `orphaned`; a takeover records who and why; it never silently changes hands.
3. Two agents run `work next` concurrently on a queue and get different items; an agent whose scope is `partyline` is never handed an `aura` item.
4. After a sync, a newly opened GitHub issue appears in intake as `untriaged` and `work next` never returns it; setting severity and effort (by label or `queue triage`) puts it in the queue at the proposed band and rank with no human action; closing it upstream closes the entry; an issue deliberately omitted from the queue appears in `queue audit`.
5. An untriaged issue older than the threshold produces exactly one escalation, not one per sync.
6. An agent's move takes effect immediately as `proposed`, with its reason, and appears in `queue review`; it is reversible from the event log. After a human pins an item, an agent's move of it is stored as a pending proposal and the pinned order is unchanged; an unreviewed proposal never blocks `work next`.
7. `work list --mine` shows owned tasks and validations together, and no manual file is consulted to resume.
8. A validation item built by seat A is not handed to A by `work next` while another seat can take it; after the band's independence grace it is. A run by A is recorded `independent: false` and still validates; a run by B is `independent: true`; a hand-typed PASS by either still cannot validate.

## 7. Decisions to settle

1. ~~Authority over order~~ **Decided:** agents propose, a human decides, and nothing blocks on the decision (3.2). Still open: who the reviewer is (you, or a nominated triager) and the review cadence the audit should nag at.
2. **Priority bands:** how many, with what meaning? (Proposed: P0 drop everything, P1 this sprint, P2 next, P3 backlog.)
3. **Order store:** Partyline rank (recommended) or a GitHub Project as source of truth?
4. **Sync credential:** reuse the Aura GitHub App, or create a read-only app for Partyline? Who provisions the secret so it stays automated?
5. **Tracked repos:** a configured list, or every repo an agent registers against? Which labels exclude an issue from intake (for example `question`, `discussion`)?
6. **Queue scope per agent:** declared at register time, or derived from `repo` and workdirs?
7. **Untriaged threshold and escalation target** (who is paged, how often).
8. **Migration of existing `progress_tasks`:** convert in place (folder becomes the seat's workspace-qualified folder) or leave read-only history and start fresh at phase 1.
9. ~~Affinity grace period~~ **Decided:** by band, Fibonacci: P0 3h, P1 5h, P2 8h, P3 13h, P4 21h (see 2.1). Still open: whether P4 should exist and whether wall-clock hours are acceptable.
10. ~~Who validates a fix~~ **Decided:** builder allowed, another seat preferred, via the independence grace in section 4. Still open: whether a builder-validated P0/P1 should be automatically re-queued for an independent spot-check, or only flagged.
12. **Severity and effort scales and labels:** are S1..S4 and Fibonacci points 1..13 right, and what GitHub label names carry them (`severity:S2`, `effort:5`)? Should an issue with effort above 13 be refused at triage until split? Should the system-proposed band come only from severity, or also weigh age and what the issue blocks?
11. **Seat key:** is `(agent_id, workspace-qualified folder)` right, given that some folders are shared by several agent ids over time (for example the Aura main clone)?
