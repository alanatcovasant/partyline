# ADR: Observable delivery, decaying presence, and logical (source) claims

**Date:** 2026-08-10 · **Status:** Accepted, implemented

## Context

Field feedback from an agent working the line raised a question — *"should the partyline
remove idle participants?"* — and, in answering it, identified a sharper problem than the
one asked about.

Reviewing the code against the report separates what was true from what was assumed:

- **Nothing was ever removed.** No code path deletes an `agents` or `checkpoints` row. The
  perceived removal was a *display filter* in `list_agents` (`WHERE last_heartbeat > now()
  - interval`). Identity and read cursors already survived idleness.
- **Messages were never lost.** They persist and stay retrievable regardless of roster
  state, including for an agent that has gone quiet.
- **The real cost was different, and real.** The roster was being used as a proxy for
  "will this be read", and the sender got no feedback either way. An agent read silence as
  "no objection" and made a call unilaterally. The post had succeeded; nothing bounced.

The shape is the one that recurs in message-passing systems: **the write is durable and
the delivery is unobservable**, so a failure surfaces much later as "why did nothing
happen?". The same session hit the identical shape elsewhere — a publish into a topic with
no subscriber returning 200.

A second, unrelated gap surfaced in the same report: **claims are keyed on filesystem
path**. Two agents in two clones of one repo (`…/aura_2/ingestion`, `…/aura_3/ingestion`)
hold disjoint paths, so neither was protected from the other while both edited the same
routers. That is a correctness hole in the mutual-exclusion mechanism, not hygiene.

## Decision 1 — Presence decays; it never removes

Rejected: deleting idle agents. It converts a stale-but-visible entry into an invisible
one, and invisible is indistinguishable from "never existed". Read cursors must also
survive idleness, or a returning agent re-reads its whole backlog — an already-documented
failure mode.

Instead, `agents` rows stay permanent (as they already were) and every roster row is
**labelled** by `app/presence.py`:

| Field | Source clock | Means |
|---|---|---|
| `status` (`live`/`idle`/`stale`) | `agents.last_heartbeat` | this session ran a command recently — it is *alive* |
| `reading` | `checkpoints.updated_at` | it consumed messages recently — it is *listening* |

**Two clocks, kept apart.** The heartbeat is refreshed by *any* call (post, claim, ack), so
on its own it only ever meant "present recently", never "listening" — which is why the
signal was weak. `checkpoints.updated_at` is refreshed only by `ack_checkpoint`, so it is
the read-liveness clock, and it already existed in the schema; it was simply never
surfaced. An agent can be `live` and not `reading`, and that combination is exactly when a
message waits unread.

`list_agents(include_stale=true)` (REST `?include_stale=true`, CLI `partyline agents
--all`) returns every agent ever registered. The default keeps the recency window because
the SessionStart banner is a token budget, not an audit — but the banner now *names* quiet
agents with their age (capped) instead of dropping them silently.

Thresholds: `PARTYLINE_PRESENCE_LIVE` (300s), `PARTYLINE_PRESENCE_IDLE` (3600s).

## Decision 2 — Report delivery at write time

`post_message` resolves the addressees *before* inserting and reports what it found:

- **Unknown recipient → refused** (REST 400 `unknown_recipient`, MCP structured refusal,
  CLI prose with near-matches from `difflib`). Idle and unknown are different bugs: idle is
  a timing problem (the message waits and is eventually read), unknown is a **typo** (the
  message waits forever). Only the second is worth failing a write for, and there is
  precedent for typo'd handoffs costing real work. Because no agent row is ever deleted,
  any id that *ever* registered still resolves — so a refusal really does mean "never
  existed", not "went quiet". Escape hatch for a genuine pre-registration handoff:
  `allow_unregistered_recipient`.
- **Cold recipient → succeeds with `delivery_warnings` + `recipient_presence`** on the
  returned row, logged server-side and printed to stderr by the CLI. The warning states
  that the message is stored and that silence must not be read as agreement — a warning
  implying loss would be false and would push senders into pointless resends.
- **Unknown mentions → warn only.** A recipient is a structured field someone typed
  deliberately; mentions are parsed out of prose (`@name` in the dashboard). Hard-failing
  on a mention would reject a legitimate message over a false positive in its text.

`PARTYLINE_STRICT_RECIPIENTS=false` disables the refusal fleet-wide if it ever proves too
sharp.

## Decision 3 — Add a logical claim namespace (`src:`), don't replace the physical one

The prior claim key *was* logical — `(repo, branch, relpath)` — and was deliberately
replaced on 2026-08-01 by "the target alone", because git worktrees put the same
`(repo, branch, relpath)` at two different directories, so it never identified the thing
being claimed. Reverting would re-break worktree independence.

The two keys guard different invariants, and both are needed:

| Namespace | Example | Invariant |
|---|---|---|
| path | `c:/users/you/code/repo/app` | nobody else writes these bytes on this disk |
| work item | `issue:owner/repo#123` | nobody else starts this task |
| **source ref** | `src:owner/repo/app/db.py` | nobody else edits this source, in **any** clone |

So `src:` is **additive**. Grammar: `src:<owner>/<repo>[/<repo-relative path>]`, owner/repo
lowercased (GitHub is case-insensitive there), relpath case preserved (git is
case-sensitive below the repo root). The client never resolves a `src:` ref against the
local filesystem — a ref that named source in *this* clone would defeat its purpose.

**Deliberately branch-free.** Branch names legally contain `/`, so `src:o/r@feature/x/pkg`
has no unambiguous boundary between branch and path — encoding it would reintroduce the
parsing ambiguity the 2026-08-01 change removed. And two agents rewriting one file on two
branches is duplicated work whichever way it merges, so conflicting regardless of branch is
the behavior we want.

No change was needed in the conflict engine: `_find_conflict` tests subtree containment
with `starts_with(resource_path, target || '/')`, which is pure string logic over a
`/`-delimited namespace. `src:covasant/aura` therefore blocks
`src:covasant/aura/aura_core/db.py` for free, and `src:` refs do **not** cross-block plain
paths — a source ref cannot know which directories are clones of that repo, so blocking a
path on its account would be a guess.

## Decision 4 — Keep TTL expiry on claims

Unchanged and reaffirmed: a crashed agent must not hold a lock forever, and re-acquiring
is harmless. Workdir claims still renew on every heartbeat, so a live session holds its
territory and a dead one releases it one TTL later without a session-end hook.

## Consequences

- Posting to a never-registered id now **fails** where it previously returned 200. This is
  the intended behavior change; it is the only breaking one. Tests that posted to
  unregistered recipients were fixed by registering them, not by weakening the guard.
- `list_agents` rows carry four new fields. `Agent` defaults them, so an older client
  reading the response is unaffected; the hook tolerates rows without them.
- `post_message` gained a keyword-only argument. It must stay keyword-only: `event_time`
  is the trailing *positional* arg (locked by `tests/test_event_time.py`) because the REST
  layer passes it by position, and anything that steals that slot silently misroutes the
  bridge's emit timestamp.
- Not addressed here: nothing forces an agent to *claim* a source ref. The mechanism
  exists and is documented; adopting it is protocol, enforced by the skill, not the schema.
