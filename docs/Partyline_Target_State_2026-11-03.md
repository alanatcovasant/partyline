# Partyline: from a message line to the fleet's work system

**Status:** Direction document. It describes where Partyline is going and why; it is not a specification. Detail lives in the three proposals linked at the end.  
**Date:** 2026-11-03  
**Audience:** Anyone who works with, or alongside, the agent fleet.

## In one paragraph

Partyline began as a shared line where coding agents announce status, ask questions and avoid editing the same files. It is becoming the **system that keeps the fleet's work from falling through the cracks**: every GitHub issue is accounted for, the next thing to do is always known and in the order we want, an interrupted piece of work resumes where its context lives, a handoff is an explicit event rather than a text file, and a fix is not "done" until a deployed validation has actually passed. **GitHub stays the source of every issue.** Partyline holds only what GitHub has no concept of: which agent is working on something right now, in which seat, and the evidence of what it ran.

## The problems this answers

| Today | Consequence |
|---|---|
| Work is tracked in people's heads, in per-folder `handoff.txt` files and in a 29,000-line testing-queue document | Issues are missed; state conflicts in merges (45 of the last 300 edits to that document were conflict merges) |
| Validation after a deploy is a backlog (468 pending sections in the Aura queue document) | Fixes sit unproven; the backlog keeps growing because nothing prioritizes clearing it |
| A claim to work an issue exists only on the Partyline line | People and tools working in GitHub cannot see it and collide with it |
| An agent that runs out of context window loses its thread; the next agent starts cold | Repeated discovery, and work handed to a different folder loses that folder's memory |
| "Done" is whatever an agent says | A typed "PASS" is indistinguishable from a validation that ran |

## Principles

1. **GitHub first.** GitHub is the source of all issues. Where GitHub has a capability (Projects fields and ordering, sub-issues, issue dependencies, issue types, auto-add) we use it and do not rebuild it.
2. **Partyline stores only what GitHub cannot.** Live ownership with a lease and liveness, seats and affinity, handoff, run evidence, and the packets that let an agent resume.
3. **Agents propose, humans decide, nothing waits.** Agents order and estimate work and the change takes effect at once; a person reviews on their own schedule and their decision, once made, wins. No one is blocked on approval.
4. **Evidence, not assertion.** An item is validated only by a run the tooling executed, recorded with its target, commit and exit code. A hand-typed result is recorded but cannot validate.
5. **Resume where the memory is.** A folder holds an agent's memory and working context, so interrupted work stays with its seat by default. Moving it is allowed, visible, and carries a warning that the memory did not move.
6. **Nothing changes hands silently.** An absent owner's work is shown as orphaned, never reassigned automatically. A takeover needs a reason and records who and why.
7. **Missed work must be detectable.** Every open issue is in exactly one place (queue, owned item, or parked with a reason), and an audit reports anything that is not.

## The picture

```
                GitHub (the source of every issue)
   Issues  ·  Project fields (severity, priority, effort)  ·  item order
   sub-issues (epics)  ·  dependencies (blocked by)  ·  issue types
                         ▲                    │
          marks the claim│                    │reads issues, fields, order
        (comment, label, │                    ▼
          Project field) │        ┌───────────────────────────────┐
                         └────────│            PARTYLINE          │
                                  │  seats · atomic claims+leases │
                                  │  priority pull  (work next)   │
                                  │  handoff · orphan detection   │
                                  │  validation evidence          │
                                  │  resume packets · escalations │
                                  └───────────────┬───────────────┘
                                                  │ CLI · hooks · channel
                              ┌───────────────────┼───────────────────┐
                           Agent seat A        Agent seat B       Humans
                        (agent + folder)    (agent + folder)   (review, decide)
```

## How a piece of work flows

1. **An issue is opened in GitHub.** GitHub's auto-add puts it in the Project. Until it has a severity and an effort estimate it is `untriaged`: visible and audited, but not pullable.
2. **Triage and clustering.** Anyone, human or agent, sets severity (S1..S4) and story points (Fibonacci). The queue proposes a priority (from severity) and groups related issues into a **cluster**: issues around the same files or functionality that will be worked as one branch and one PR (today a cluster exists only as a PR that closes several issues; Partyline adds it before the PR and then follows the PR). An issue on its own is a cluster of one. Smaller work goes first within a priority.
3. **Ordering.** Agents can move items with a reason and it applies immediately as a proposal. A person reviews the proposals when they can, confirming or pinning. A change made directly in GitHub is recognized as a human decision.
4. **Pull.** An agent runs `work next`. It resumes its own interrupted cluster first, then clusters reserved to its seat, then the top of the shared queue in its scope. The claim is atomic and covers every issue in the cluster.
5. **The claim is visible in GitHub,** on every issue in the cluster: a comment naming the seat, a `partyline:claimed` label and a `Claimed by` Project field appear. A claim made in GitHub by someone not using Partyline is honored too.
6. **Work, with a trail.** The agent records milestones and a `next_action`. If its context runs out, the same seat resumes; if it must move, the owner offers it and another seat accepts into its own folder. A stale owner's item shows as orphaned and can be taken over with a reason.
7. **Deploy.** A deploy event for a target moves the waiting validation requests whose fixes it contains into the queue.
8. **Validation, ahead of everything at its level.** A validation is a typed sub-issue of its fix, one priority band above its issue (P1 becomes P0). It is pulled like any other item, preferring a seat other than the builder, and passes only on an executed script run with a recorded exit code.
9. **Done.** The validation issue closes on a passing executed run; the evidence stays in Partyline, linked from GitHub.

## Key concepts

| Concept | Meaning |
|---|---|
| **Seat** | An agent in a specific, machine-qualified folder. The unit that owns work, because it carries the memory. |
| **Work item** | A cluster of one or more GitHub issues, plus Partyline's runtime state: owner, claim, next action, history. The cluster is what is queued, claimed, handed off and validated. |
| **Cluster** | Issues worked together as one branch and PR, grouped by shared files or functionality, within one repository. Its priority is its highest member's; its points are the sum, capped. |
| **Severity, story points, priority** | Three separate fields: how bad (S1..S4), how big (Fibonacci points) and when (P0..P3). Priority is proposed from severity but can differ, for example a minor issue that blocks a release. |
| **Claim** | An atomic lease with a TTL on an item, decided by Partyline and projected into GitHub. |
| **Priority band** | P0 to P3 (and an optional P4 icebox), set from severity, with Priority as a Project field. |
| **Affinity grace** | How long an item stays reserved for its home seat before the pool may take it. The more urgent the item, the shorter: P0 3h, P1 5h, P2 8h, P3 13h, P4 21h (Fibonacci). |
| **Validation request** | A typed sub-issue created at deploy, one band above its issue, waiting until a deploy covers its fix. |
| **Evidence** | An executed run: target, deployed commit, exit code (0 pass, 1 fail, 2 inconclusive), validator seat, and whether it was independent. |
| **Orphaned** | An unfinished item whose owner has gone stale. Shown, never reassigned automatically. |

## What it replaces, and what stays

**Replaced:** the per-folder `handoff.txt` as a source of truth; the shared Deployed Testing Queue document as the place state lives (the per-issue procedure prose moves to one file per issue beside its validation script); ad-hoc "who is on this?" questions.

**Unchanged:** the line itself (status, questions, claims and messages between agents), the CLI-first model with lifecycle hooks, the push channel, identity by agent id, and the rule that Partyline never deletes history except for reserved test data.

## Where it stands (2026-11-03)

| Capability | State |
|---|---|
| Folder progress ledger: current task, backlog, append-only history | Deployed (earlier) |
| **Validation queue prototype**: items per target, executed-vs-typed evidence, atomic claims, `partyline vq`, test-target purge | Built and deployed to `dev-eastus2`; live check passes |
| **Ownership handoff (phase 1)**: offer/accept/decline/takeover, `next_action`, owner status and orphan flag, opt-in fencing, test-folder purge | Built and deployed to `dev-eastus2`; live check passes (17/17) |
| Seats with machine-qualified folders, affinity and the Fibonacci grace | Designed |
| GitHub-backed queue: read the Project's fields and order, `work next`, scope, audit of untriaged and missing issues | Designed (needs a read-only GitHub App) |
| Visible claims in GitHub; GitHub-side claims honored | Designed (first version writes from the client with the agent's `gh` login) |
| Agent moves and triage written to GitHub; human-change detection; alarms; dashboard views | Designed |
| Validation as typed sub-issues created at deploy; session-start queue digest; retirement of `handoff.txt` dual-write | Designed |

## Roadmap

1. **Ownership and handoff on the existing ledger.** Done; deployed.
2. **A GitHub-backed queue and visible claims.** The sync and `work next`, with the claim projected into GitHub.
3. **Agent moves, triage and alarms written to GitHub,** human-change detection, dashboard.
4. **Validation as sub-issues,** the deploy event, the independence preference, retiring the old files.

Each step ships a validation script with positive and negative cases that exits non-zero on failure.

## What Partyline is not

- **Not an issue tracker.** GitHub is. Partyline mirrors read-only and writes only claim marks and the changes agents make on a human's behalf.
- **Not an agent runner or orchestrator.** It does not launch or supervise sessions. If a runner such as Omnigent later owns sessions and worktrees, the seat maps onto them and the queue logic is unchanged.
- **Not a project-management suite.** No estimates beyond severity and effort, no time sheets, no transcripts.

## How we will know it works

- **Nothing missed:** the audit reports zero open issues in tracked repositories outside the Project or a parked entry, and any untriaged issue older than its threshold has produced exactly one escalation.
- **Validation backlog shrinks:** the count and oldest age of pending validations fall, and the share validated by a seat other than the builder is visible.
- **Resume is cheap:** after an interruption, the same seat is working again from its `next_action` without opening a transcript.
- **Claims are visible:** no issue is worked without a claim mark in GitHub, and no two seats hold the same issue.
- **No silent transfers:** every change of owner has an event with an actor and a reason.

## Decisions taken, and what is still open

**Taken:** GitHub is the source of issues and of order, severity and effort; agents propose and a human decides without blocking; triage (severity and story points) gates entry to the queue; the cluster is the unit of work, with the highest member's priority and a cap of 13 points; affinity grace by band on the Fibonacci series; validation prefers another seat but allows the builder; validation requests are typed GitHub sub-issues one band above their issue; the first claim marks are written from the client; the server sync uses a new read-only GitHub App.

**Open:** who reviews proposed ordering and how often; the label, field and issue-type names; how the legacy validation backlog is imported without flooding the Project; the seat key where several agents share a folder; whether a starvation cap is needed when validations outrank tasks; how the GitHub App's secret is provisioned automatically.

## Where to read more

- [PROPOSAL_Unified_Work_Queue_2026-11-03.md](PROPOSAL_Unified_Work_Queue_2026-11-03.md): ownership, seats, the GitHub-backed queue, visible claims, validation routing, phasing, acceptance and the decision list.
- [PROPOSAL_Validation_Queue_2026-11-02.md](PROPOSAL_Validation_Queue_2026-11-02.md): the validation queue and its evidence rule, with prior-art comparison.
- [PROPOSAL_Work_Ledger_2026-10-30.md](PROPOSAL_Work_Ledger_2026-10-30.md): the original work-ledger case, tool comparison (Beads, Omnigent, GitHub Projects) and correctness requirements.
- [PRD_Covasant_Partyline.md](PRD_Covasant_Partyline.md) and the whitepaper: the product as it stands today.
