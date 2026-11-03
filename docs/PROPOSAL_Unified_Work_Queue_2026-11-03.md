# Proposal: One Work Queue — agent work, handoff and a prioritized GitHub intake

**Status:** Draft for discussion; no implementation or deployment authorized by this document.  
**Date:** 2026-11-03  
**Extends:** [PROPOSAL_Work_Ledger_2026-10-30.md](PROPOSAL_Work_Ledger_2026-10-30.md) (items/runs/events, handoff offers, evidence-bound completion) and [PROPOSAL_Validation_Queue_2026-11-02.md](PROPOSAL_Validation_Queue_2026-11-02.md). Those define the item model; this document adds **ownership**, a **ranked unclaimed queue fed from GitHub Issues**, and the rule that makes dropped issues detectable.

## Position

An agent's work list, the handoff log and the validation queue are one thing: **work items with an owner, a state and an append-only history.** The owner is a **seat**: an agent in a specific folder, because that folder holds the agent's memory and working context. A handoff to a different seat is therefore the exception, and the default for an interrupted item is to resume in the seat that had it; a validation is an item of another kind; the "what should I do next?" list is the unowned items in rank order. Persistence, transfer and fleet visibility fall out of keeping that in Partyline rather than in files.

It is not only a naming change. The deployed `partyline progress` ledger covers persistence and history, but four things are missing (section 2), and the intake queue (section 3) is new.

## 0. GitHub first: use what GitHub has, store only what it lacks (decided 2026-11-03)

**GitHub is the source of every issue.** Where GitHub has a capability that does the job, we use it and do not rebuild it in Partyline. Partyline stores only **runtime state about agents that GitHub has no concept of**: who is working on something right now, in which seat, with what lease, with what evidence.

| Need | GitHub capability (verified in docs and changelog, 2026-11-03) | Partyline's role |
|---|---|---|
| The issue: title, state, labels, body, assignees | Issues | Read-through cache only. Never the source. |
| Intake: every new issue lands somewhere | Projects v2 **auto-add** and **auto-archive** built-in workflows | Audit that no tracked issue is outside the Project; do not rebuild intake |
| Severity, priority band, effort | Projects v2 **custom fields**: single-select (Severity, Priority) and number (Effort) | Read them. No severity/effort/band columns of our own |
| Order of the queue | Projects v2 **manual item order**, changeable by API (`updateProjectV2ItemPosition`) and by dragging in the UI | Read it. Agents move items through GitHub, not through a Partyline rank column |
| Epic / parent grouping | **Sub-issues** (parent/child, nested) | Read parent links for affinity and for "same epic" routing |
| Blocked by / blocking | **Issue dependencies** (generally available since 2025-08-21, REST API, `is:blocked` search) | `ready` means no open blocker, read from GitHub; do not model dependencies ourselves |
| Kind of work (including validation) | **Issue types** (org-level), sub-issues | A validation request can be a typed sub-issue of its fix (section 4) |
| Who has claimed an issue, visibly | Comments, labels, a Project text field, assignees | **Project the Partyline claim into GitHub** (section 2.2); accept claims made in GitHub. Assignees alone cannot name a seat |

Gaps GitHub does not fill, which remain Partyline's job (and are what the rest of this document designs):
- **An atomic claim with a lease and liveness.** Assigning is not a lease: it has no expiry, does not know the agent died, and cannot be raced safely by many agents calling `next`.
- **Seats, affinity and handoff** (offer/accept/takeover, orphan detection, the Fibonacci grace windows).
- **Run evidence.** An executed script's exit code, target and deployed commit are not an issue or comment concept; Partyline records them and links back.
- **Resume packets, `next_action`, the session-start digest, and escalations** delivered over the line to agents and humans.

Consequences for the sections below: severity, effort, band, order, parent and blocker data are **read from GitHub** (section 3.1); moves are performed **in GitHub** (section 3.2); and phase 2 no longer builds rank or triage tables (section 5). References: [Projects fields and the GraphQL API](https://docs.github.com/en/graphql/reference/projects), [issue dependencies GA](https://github.blog/changelog/2025-08-21-dependencies-on-issues/), [issue dependencies REST](https://docs.github.com/en/rest/issues/issue-dependencies). Issue types and sub-issue limits were confirmed only from secondary sources; verify against docs.github.com before building.

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

### 2.2 A seat claims a GitHub issue, visibly in GitHub (decided 2026-11-03)

A claim that exists only in Partyline is invisible to a person reading the issue, and to anyone working without Partyline. So **claiming an issue is an explicit act that leaves marks in GitHub**, and a claim made in GitHub is honored by Partyline.

**Authority.** Partyline holds the lease and decides races (it is atomic, with a TTL and liveness); GitHub cannot arbitrate between two near-simultaneous writers. The marks in GitHub are a **projection of the Partyline claim**, written only by the seat that won it. The reverse path (2.2.3) makes GitHub-side claims binding on Partyline.

#### 2.2.1 The marks

All are written together when the claim succeeds (`partyline claim-issue`, and `work next`, which claims):

| Mark | Content | Why this one |
|---|---|---|
| **Claim comment** (one per issue, edited in place) | A readable line plus a hidden machine marker: `<!-- partyline-claim v1 seat="alan_aura_2" folder="aura_2" since="2026-11-03T10:15Z" -->` and text such as "Claimed by `alan_aura_2` (aura_2), branch `fix/4836`, since 10:15Z. Next: re-run the validator." | Visible in the issue timeline for people who live in issues; the marker lets tools and other agents verify the claimant without scraping prose |
| **Label `partyline:claimed`** (one static label, never one per seat) | Present while the issue is claimed | Searchable (`label:partyline:claimed`), filterable in Projects, usable by GitHub automations. A label per seat would sprawl across every agent id and clone |
| **Project field `Claimed by`** (text) | The seat id | Shows in board and table views at a glance, and is sortable and filterable there |
| **Assignee** (optional) | The operator's GitHub account | Notifications and "assigned to me" views. It does not identify a seat, because every seat usually acts as the same GitHub user |

#### 2.2.2 Keeping GitHub in step

- **Claim:** all four marks. **Release / done / lease expiry:** the comment is edited to "released" with the reason, the label and the field are cleared.
- **Milestones:** the comment's "latest / next" line is edited, at most once per few minutes. Heartbeats are **not** written to GitHub (too noisy, and rate-limited); liveness stays in Partyline.
- **Handoff accepted or takeover:** the claim comment is edited to the new seat, and a **new short comment** records the change ("handed off from A to B: note", or "taken over from stale A"), so the ownership trail is in the timeline even though edits of the claim comment are not.
- **Owner stale (orphaned):** the comment gains "owner stale since ...; claimable" while the label stays until the claim is released or taken over, so readers see an issue that is nominally claimed but not being worked.
- **Issue closed upstream while claimed:** the claim is released with the reason "issue closed", and the seat is told over the line.

#### 2.2.3 Claims made in GitHub (inbound)

Someone not using Partyline, or a person, can claim an issue in GitHub by any of: applying `partyline:claimed`, commenting `/claim` (optionally `/claim seat=<agent id>`), or setting an assignee **other than the account the seats act as**. (Every seat usually acts as one GitHub account, and issues are often assigned to it as ordinary practice, so an assignee equal to that account is not a claim; counting it would block all of the fleet's own work. Found when the first read-only check hit an issue assigned to the operator.) At the next sync such an issue is a **foreign claim**:

- It is **not pullable** by `work next`, so a Partyline seat cannot pick up work someone already took.
- If it names a registered seat, Partyline **adopts** it as that seat's claim. Otherwise it is recorded against the GitHub login and shown in the audit as an external claim.
- **A human removing the label (or the claim comment) is a release**, consistent with a human's change always winning (3.2): Partyline releases the claim and tells the seat over the line.

#### 2.2.4 Conflicts and failure

- Partyline claim and GitHub marks disagree (different seat in the comment): Partyline's recorded claim is authoritative among Partyline seats; it rewrites the marks and logs a `claim_mismatch` event.
- Two seats race: exactly one wins the Partyline lease, and only the winner writes marks.
- **A failed GitHub write never blocks the claim.** The claim stands and is flagged `unprojected` in listings and the audit; the next sync retries. `work next` shows the flag so the agent knows others cannot yet see the claim.
- Comments are advisory: only the recorded lease governs pulls. A spoofed `/claim` can block an issue from `work next` but is visible in the audit with its author; commands are honored only from repository collaborators.

#### 2.2.5 Who writes

- **MVP: from the client, with the agent's own `gh` authentication.** No server secret; the comment and label appear under the operator's account with the seat id in the body, and the Project field is set the same way. This works today for any seat that has `gh`.
- **Later: from the server with a GitHub App** (bot identity, retries, the audit's `unprojected` repair). This is the same credential decision as the sync (decision 4), needing write permission for issues and the Project.

## 3. The unclaimed queue, populated from GitHub Issues

### 3.0 Vocabulary: severity, story points, priority, cluster (decided 2026-11-03)

Four different things, kept as four different fields:

| Term | Answers | Values | Set | Lives in |
|---|---|---|---|---|
| **Severity** | How bad is the problem if left alone? Intrinsic impact. | S1..S4 | At triage; rarely changes | Project single-select `Severity` |
| **Story points** | How big is the work? | Fibonacci 1, 2, 3, 5, 8, 13 | At triage | Project number `Story points` (called "effort" elsewhere in this document) |
| **Priority** | When should it be worked? Urgency, the thing the queue sorts by. | P0..P3 (P4 icebox) | **Proposed from severity**, then adjustable with a reason (blocks a release, deadline, blocks another issue) | Project single-select `Priority` |
| **Cluster** | Which issues travel together in one branch and one PR? | A name per cluster, within one repository | Proposed by agents, reviewed by a human | Project field `Cluster` |

Severity and priority are deliberately separate: a cosmetic S4 that blocks a release is P0, and an S1 that already has a workaround may not be. The earlier rule that the starting band comes from severity remains the **default proposal**, not a constraint.

#### 3.0.1 Clusters: the unit of work

To minimize branches and PRs, similar or related issues are worked as a unit: grouped around **the files involved or the functionality**. The cluster, not the single issue, is therefore the unit that is queued, claimed, handed off and (with its fixes) validated. Rules:

- **Every issue is in exactly one cluster; the default is a cluster of one.** There is then one model, not two: the queue is a queue of clusters, and an unclustered issue is simply a singleton.
- **One cluster = one branch and one PR** in one repository, whose description closes every member (GitHub links a PR to several issues natively). A cluster never spans repositories.
- **Today a cluster exists only as a PR that closes several issues** (decided: that is the current practice), so the design adds the cluster **before** the PR and then follows the PR:
  - Before a PR exists, the cluster is a name in the Project field `Cluster` (a branch-style slug chosen by the proposing agent, for example `ingest-retry-fixes`) so the group can be queued, claimed and seen.
  - Once a PR exists, **the PR's closing references are the truth**. At sync, issues a PR closes become one cluster named for the PR, and the `Cluster` field is rewritten to match. If the PR's references differ from the planned cluster, the PR wins (it is what is actually being worked) and the difference is logged as an event.
  - GitHub's own PR-to-issue links therefore carry the cluster through merge and close; no second record is needed for finished work.
- **Formation.** Agents propose membership with a reason (shared files, same functionality, same component), as they propose ordering: it takes effect at once and a human reviews it without blocking anything. Signals: files touched by the issue's earlier fixes or linked PRs, the area labels, and an agent's own read of the code. Only **triaged** issues can join a cluster.
- **Cluster priority = the highest priority among its members** (decided). A P3 that rides along with a P1 is worked at P1; that is the point of clustering, and the cost (a low item pulled forward) is visible in the audit.
- **Cluster points = the sum of its members' points, capped at 13** (decided: the top of the scale), so a cluster stays reviewable as one PR; a cluster over the cap is split. There is no separate issue-count cap.
- **Pull, claim and affinity act on the cluster.** `work next` hands out a cluster; the claim covers every member issue and writes its marks to **each** member (so someone reading any one of them sees the claim, 2.2); the home seat, the affinity grace and handoff all apply to the cluster, using its priority.
- **Validation follows the cluster.** All members deploy together, so their validation requests share the cluster's priority band minus one and are taken by one validator as a batch for the same target and commit, instead of one claim per issue.
- **Membership changes are events** (join, leave, split, merge) with actor and reason. Closing every member closes the cluster. A cluster with a claimed member is not dissolved by an agent without the claim holder's agreement.
- **Dependencies** (GitHub "blocked by") between members are ignored inside the cluster and between clusters they order it: a cluster is `ready` only when nothing outside it blocks any member.

**Why a Project field, not a sub-issue parent or a label.** GitHub allows an issue only **one** parent, and the parent is already used for epics (and, in this design, for a fix's validation sub-issues), so using it for clusters would force a choice between an epic and a cluster. A label per cluster would sprawl and is repository-scoped. A Project field is visible in board and table views, filterable and groupable, works across the organization's Project, and does not compete with either.

### 3.1 Model

A **queue** is an ordered set of unowned work items. Named queues: one intake queue per scope (repo or area, for example `aura`, `partyline`), and per-target validation queues (`validation:dev-eastus2`).

**Triage gate** (decided 2026-11-03): an issue is available for the queue only once it has a **severity** and an **effort estimate** (story points). It then belongs to a cluster, by default its own (3.0.1). Until then it is `untriaged`: visible in intake and audited, but not pullable by `work next`. Once both are set it is `triaged`, and enters the queue.

- **Severity** `S1..S4` (S1 critical: broken or blocking; S2 major; S3 minor; S4 cosmetic or nice-to-have). **Effort** in Fibonacci points `1, 2, 3, 5, 8, 13` (anything larger needs splitting before it is triaged). The scales are proposed; see decision 12.
- **Who triages:** anyone. A human sets them in GitHub (labels such as `severity:S2`, `effort:5`, read at sync; GitHub wins when present), or an agent estimates them with `partyline queue triage --issue owner/repo#N --severity S2 --effort 5 --why "..."`, which sets the Project fields. Agent estimates carry a `triage_source: agent` marker (a comment or a field) so a human can confirm or correct them.

On triage the server **proposes a position**: band from severity (S1 -> P0, S2 -> P1, S3 -> P2, S4 -> P3; P4 icebox only by explicit parking), then within the band **smaller effort first, then older first** (cheap work clears before large work). The proposal is effective immediately; no one has to approve it for it to be workable.

**Where this lives (section 0):** Severity, Priority band and Effort are **Project fields**, and the order is the **Project's item order**. Partyline's queue is a read-through mirror (`source_ref` = `owner/repo#N`, plus the field values, position, parent and blockers at last sync), used for fast `work next`; it has no `severity`, `effort`, `band` or `rank` columns of its own. The only queue state Partyline adds is what GitHub lacks: `order_state` (`proposed` | `confirmed` | `pinned`, derived as described in 3.2), the current claim, seat and affinity. Agent estimates (`triage_source: agent`) are written to the Project fields through the GitHub App, so GitHub stays the record; if write access is not available the estimate is held in Partyline as an unapplied proposal and shown as such.

**Interim, implemented (phase 2, slice 1).** Until the Project fields exist, the labels the fleet already uses carry the same data, read at sync: `P0`..`P3` (band), `sp-N` or `effort:N` (story points, Fibonacci only), `severity:S1`..`S4`, and `cluster:<name>` (the cluster, 3.0.1). Aura already has `P0`..`P3`, `sp-1`..`sp-13` and `cluster:*` labels, so nothing new has to be created there. An explicit `P` label outranks a band derived from severity, and **an issue is triaged when it has a band (from `P` or severity) and points**; severity is optional because the fleet's labels do not carry it. Order is band, then fewer points, then older. The mirror table `work_items` holds only what GitHub said at the last sync. **A claim is a live row in the existing `claims` table keyed `issue:owner/repo#N`, never a column of the mirror**, so there is one claim authority: a direct `claim-issue` takes an item out of the queue with no extra step, and `work next` claims through the same all-or-nothing `claim_targets`. Reads are a join, so state is derived: `ready | untriaged | claimed | external | closed`. Only an assignee other than the acting `gh` account counts as an external claim (2.2); a `partyline:claimed` label with no live claim is reported by the audit but does not block, because a lease that expired leaves its label behind and would otherwise stay stuck.

Order is **Priority band first, then the Project's item order within the band, then age**. Changes made through Partyline are events (actor, before, after, reason). A change made directly in GitHub is seen at the next sync as a position or field difference with no matching Partyline event, which is how a human decision is recognized (3.2).

### 3.2 Who proposes the order, who decides (non-blocking)

**Agents propose; a human decides; nothing waits for the decision.**

- **Proposals take effect at once.** Any agent can propose a band or position for a triaged item (`partyline queue move --issue owner/repo#N --band P1 --before owner/repo#123 --why "blocks #4700"`). Partyline performs it in GitHub (the Priority field and the item position, via the Projects API) and records the actor and reason. It is `order_state: proposed`. Work is never held for approval.
- **A human's change is recognized without any extra step.** If a sync finds a Priority or position that differs from Partyline's last recorded state and no Partyline event explains it, a person changed it in the GitHub UI: the item becomes `pinned`. Humans keep their normal tool, and agents never have to ask.
- **A human reviews on their own schedule.** `partyline queue review` (and a dashboard view) lists items whose order is still `proposed`, oldest first. The human confirms them in bulk (`confirmed`) or sets a band or rank (`pinned`). Review age appears in the audit; an unreviewed proposal never expires, never blocks, and never changes the order by itself.
- **A human's decision outranks agents without freezing them.** A `pinned` item's order does not change when an agent proposes otherwise: Partyline does not write the move to GitHub; it keeps it as a pending proposal beside the item (visible in the review list) and the pinned order stays in force until the human acts. This is the only case where an agent's proposal does not take effect, and it exists because a person has already decided.
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

What is mirrored: from **issues** the ref, title, state, labels, assignees, parent, blockers, updated_at and URL; from the **Project** the Severity, Priority and Effort field values and the item order. Issue bodies and comments stay in GitHub. The credential is a GitHub App with read access to issues and the organization Project; write access (Project fields and position, assignee) is only needed for agent moves and the optional assignee mirror, and can be a second, later grant.

### 3.6 GitHub Projects as the order store (decided)

Decided 2026-11-03 (section 0): the order and the triage fields live in a GitHub Project. This reverses the earlier recommendation to keep an authoritative rank in Partyline. What the Project cannot do (an atomic claim with a lease, seat affinity, run evidence) stays in Partyline, which is why `work next` reads the Project's order but takes the claim itself.

Costs accepted: moves go through the Projects GraphQL API; reads are eventually consistent with the sync interval (so `work next` may briefly use a slightly stale order, and the claim is still safe because it is atomic in Partyline); and one Project (or a small fixed set) must be named as the source per scope. Open point: GitHub Projects v2 is organization-owned, so the Project and the GitHub App must live in the `covasant` org.

## 4. Folding in the validation queue and the handoff log

- **Validation items** are `kind=validation` items with `target`, `script` and executed-run evidence (the prototype's rules unchanged: only an executed passing run validates). They live in the **same priority queue** as tasks (a per-target view is a filter, not a separate queue) and are pulled with the same `next`.
- **Validation enters the queue after deploy, one band higher than its issue** (decided 2026-11-03). The bias is deliberate: clearing validations is worth more than starting new work, and the existing validation backlog (468 pending sections in the Aura testing-queue doc) is the problem this is meant to break.
  - **A validation request is a GitHub item** (decided direction, see decision 14): a sub-issue of the fix issue with the issue type `Validation`, created by the deploy step through the GitHub App, with its Project Priority set one band above the parent. It is then visible, orderable and countable in the same Project as everything else, and "validated" is closing that issue after Partyline's executed run passes. Partyline keeps what GitHub has no place for: the executed run (exit code, target, commit, validator seat, `independent`) and the claim.
  - **Before deploy** the item exists but is `waiting` on its deploy (created when the fix PR is enqueued). It is not pullable, so nobody tries to validate a build that is not running.
  - **A deploy event** for a target (posted by whoever deploys, for example the build agent: "build X is live on target T", carrying the commit) moves every waiting item for that target whose fix is in that commit to `ready`.
  - **Band = the issue's band minus one, floored at P0** (P1 -> P0, P2 -> P1, P3 -> P2). A P0 fix's validation stays P0.
  - **Within a band, validations sort before tasks**, then smaller effort, then age. This is the "get it out of the way" bias in the order itself, not a separate lane.
  - **The independence grace** (below) then governs who may take it: the builder's seat is deprioritized for the band's Fibonacci window.
  - **Eligibility includes the target.** `work next` hands a validation only to a seat that can reach the target (a registered capability such as "has a `dev-eastus2` profile"), so an agent is not handed work it cannot run. A build agent that holds a deploy lock on the target (#3648 in the Aura notes) can exclude itself the same way.
  - **Starvation guard.** Because validations outrank tasks at equal band, a flood could starve feature work. The audit reports the queue's validation/task mix and the oldest waiting task per band; a cap (for example, at most N consecutive validations before the next task) is a decision, not a default.
  - **Backlog migration.** The legacy pending validations are imported with the band derived from their issue's labels, or P2 (so a P1 validation) if unknown, and their real age, never all at P0, which would bury the genuinely urgent ones. The `vq_*` prototype tables should share this model's ownership, claim and event mechanics; whether they share physical tables is an implementation choice, but agents must see **one** list: `partyline work list --mine` shows tasks and validations together.
- **Who validates a fix** (decided 2026-11-03): the builder's seat may validate its own fix, but another seat is preferred. This is the mirror image of folder affinity: for validation items the builder's seat is the one that is *deprioritized*.
  - A validation item records `built_by` (the seat that owned the fix item; the prototype's `enqueued_by` is the closest field today).
  - `work next` on a validation queue skips items built by the caller's seat while the **independence grace** runs. It reuses the same Fibonacci table as affinity (P0 3h, P1 5h, P2 8h, P3 13h, measured from when the item became ready), so there is one timing concept to learn. Once it lapses, the builder's seat may take it rather than letting the item wait.
  - Each run records the validator seat, and the item shows `independent: true|false`. A builder-validated item is fully `validated` (it still needs an executed passing run); the flag makes it findable, so an operator can spot-check or re-queue those items for another seat.
  - Independence is at **seat** level (a different agent in a different folder). It does not guarantee a different model or vendor; that would need `harness`/`model` on the seat and is left as a later refinement.
- **`.handoff/handoff.txt`** stops being a source of truth once session start prints the agent's queue and `resume` packet. `add.sh` dual-write is then retired: milestones are written once, to the ledger.
- **Session start** (hook) prints: your owned items with next actions, offers addressed to you, then the top of your scope's queue and any `untriaged` count. Agents begin from the queue rather than from a file they must locate.

## 5. Phasing

1. **Ownership + handoff on the existing ledger**: `next_action`, offer/accept/decline/takeover, opt-in fencing, orphan labeling. Partyline-only runtime state, unaffected by section 0. Prerequisite for everything else.
2. **Read-through queue + `work next` + visible claims** (no rank or triage tables): a GitHub App with read access, the sync (client-pushed first, server-side scheduled second), a mirror of Project fields, order, parents and blockers, `work next` with the claim in Partyline, **the client-side claim projection into GitHub (2.2: comment, label, Project field) and the foreign-claim detection**, agent scope, seat affinity, and `queue audit`.
3. **Agent moves and triage written to GitHub** (Project field and position mutations, the optional assignee mirror), human-change detection to set `pinned`, the alarms for untriaged and orphaned items, and the dashboard views.
4. **Validation as typed sub-issues**: created at deploy, Priority one band higher, claimed and evidenced in Partyline; session-start queue digest; retire the `add.sh` dual-write.

The `vq_*` prototype and the handoff work stay valid: they hold exactly the runtime state GitHub lacks.

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
10. Claiming an issue through Partyline leaves, in GitHub, the claim comment, the `partyline:claimed` label and the `Claimed by` field naming the seat; releasing removes the label and field and edits the comment; a handoff edits the seat and adds a trail comment.
11. An issue claimed in GitHub by someone not using Partyline (label, `/claim` or assignee) is not returned by `work next` and appears in `queue audit` as an external claim; removing the label releases it and the seat is notified.
13. Two triaged issues in one repository that share files are proposed as one cluster; the cluster's priority is its highest member's and its points are the sum; `work next` hands it out as one item, the claim marks appear on every member, and a PR that closes all members closes the cluster. A cluster over the cap, spanning two repositories, or containing an untriaged issue is refused.
12. If the GitHub write fails, the Partyline claim still stands, is flagged `unprojected`, and a later sync projects it. Two seats claiming the same issue concurrently produce exactly one set of marks.
9. A validation item is `waiting` and not pullable until a deploy event for its target covers its fix; then it is `ready` at the issue's band minus one (P1 -> P0, P0 stays P0) and sorts ahead of tasks in that band.
8. A validation item built by seat A is not handed to A by `work next` while another seat can take it; after the band's independence grace it is. A run by A is recorded `independent: false` and still validates; a run by B is `independent: true`; a hand-typed PASS by either still cannot validate.

## 7. Decisions to settle

1. ~~Authority over order~~ **Decided:** agents propose, a human decides, and nothing blocks on the decision (3.2). Still open: who the reviewer is (you, or a nominated triager) and the review cadence the audit should nag at.
2. **Priority bands:** how many, with what meaning? (Proposed: P0 drop everything, P1 this sprint, P2 next, P3 backlog.)
3. ~~Order store~~ **Decided:** the GitHub Project (section 0, 3.6). Still open: which Project(s), and who owns the Priority/Severity/Effort field definitions.
4. ~~Sync credential~~ **Decided 2026-11-03:** a new read-only GitHub App for Partyline (issues and the org Project), with a write grant added later only for agent moves and server-side claim marks. Still open: who provisions the App's secret into the bundle so it stays automated (a blocker to flag if it cannot be).
5. **Tracked repos:** a configured list, or every repo an agent registers against? Which labels exclude an issue from intake (for example `question`, `discussion`)?
6. **Queue scope per agent:** declared at register time, or derived from `repo` and workdirs?
7. **Untriaged threshold and escalation target** (who is paged, how often).
8. **Migration of existing `progress_tasks`:** convert in place (folder becomes the seat's workspace-qualified folder) or leave read-only history and start fresh at phase 1.
9. ~~Affinity grace period~~ **Decided:** by band, Fibonacci: P0 3h, P1 5h, P2 8h, P3 13h, P4 21h (see 2.1). Still open: whether P4 should exist and whether wall-clock hours are acceptable.
10. ~~Who validates a fix~~ **Decided:** builder allowed, another seat preferred, via the independence grace in section 4. Still open: whether a builder-validated P0/P1 should be automatically re-queued for an independent spot-check, or only flagged.
16. ~~Clusters~~ **Decided 2026-11-03:** clusters are today a PR that closes several issues, so the PR's references are the truth once a PR exists and the `Cluster` Project field covers the planning stage before it; priority is the highest member's; the cap is 13 story points with no count cap. Still open: the naming format for a pre-PR cluster, whether a low member may be left out of a high cluster at a reviewer's discretion, and who may merge or split a cluster while it is claimed.
15. **Claim marks in GitHub:** **Decided 2026-11-03:** the first version writes them from the client with the agent's own `gh` login. Still open: the label name (`partyline:claimed`), the Project field name (`Claimed by`), the comment marker format, and whether the optional assignee mirror is wanted. Is a human's removal of the label always a release? How often may the comment be edited (the proposal says at most once every few minutes)?
14. ~~Validation requests as GitHub sub-issues~~ **Decided 2026-11-03: yes**, typed sub-issues of the fix. Still open: the issue type name, how the legacy backlog of about 468 pending validations is brought in (all at once would flood the Project; a staged import by age or band is safer), and the write grant (`issues:write`) the deploy step needs.
13. **Validation boost details:** is one band always right, or should it depend on the issue (a security fix to P0 regardless)? What is the starvation cap, if any? Which seat capabilities gate a target (profile, identity, deploy-lock)?
12. **Severity and effort scales and labels:** are S1..S4 and Fibonacci points 1..13 right, and what GitHub label names carry them (`severity:S2`, `effort:5`)? Should an issue with effort above 13 be refused at triage until split? Should the system-proposed band come only from severity, or also weigh age and what the issue blocks?
11. **Seat key:** is `(agent_id, workspace-qualified folder)` right, given that some folders are shared by several agent ids over time (for example the Aura main clone)?
