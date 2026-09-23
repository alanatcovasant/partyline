# Teams ↔ Partyline Two-Way Mirror — Design

**Date:** 2026-09-23
**Status:** Draft — supersedes nothing; extends the shipped one-way mirror (c3fc19e)
**Author:** alan_partyline_1 (Kimi Code)

## Problem

`partyline-teams` today is one-way: it polls the Partyline REST log and posts
`escalation` / `blocked` / `question` / `done` cards into a Teams channel via a
Workflow webhook. Humans watching the channel can *see* the line but cannot answer
it — a human who reads a `blocked` card and knows the answer has to find a terminal
or an agent session to respond. The line's value is bidirectional coordination; the
Teams surface is currently read-only.

This design adds the second direction — Teams channel posts → Partyline — as a
second leg of the **existing poll loop** in `teams_mirror.py`, so the mirror becomes
a true two-way sync without any new standing infrastructure.

## Non-goals (v1)

- **Threaded replies.** Teams replies to a channel message live under
  `/messages/{id}/replies`; v1 mirrors only root channel posts. A human replying in
  a thread under a mirrored card will NOT reach the line in v1 (see Open Questions).
- **@mention resolution.** v1 does not parse Teams @mentions into Partyline
  `mentions`. Everything inbound posts as a broadcast `status` (see Mapping).
- **Replacing the webhook.** Outbound stays exactly as shipped: Workflow webhook,
  no SSE, no changes to `card_for` other than the loop-prevention filter.
- **A Teams bot / public endpoint.** No inbound HTTP surface is added anywhere.
  Both directions are outbound polls from the mirror process.

## Constraints & environment

- **No standing SSE.** The same idle-cost rule that shaped the outbound leg
  applies inbound: a poll fits, a held connection does not.
- **Microsoft Graph is the only pollable read path for channel messages.** Teams
  has no anonymous inbound equivalent of the Workflow webhook.
  `GET /teams/{team-id}/channels/{channel-id}/messages` with application
  permission `ChannelMessage.Read.All` is the target call.
  - **Protected-API caveat:** Teams APIs that read channel messages with
    *application* permissions are on Microsoft's protected-API list and may
    require an additional approval request to Microsoft before the app
    registration actually works, plus admin consent. This is the one piece of
    genuinely new infrastructure. If approval is unavailable, the fallback is
    *delegated* `ChannelMessage.Read` with a one-time interactive sign-in and a
    cached refresh token — no approval needed, but the refresh token is a
    standing secret that must be rotated and can be revoked. Decision point is
    called out in Open Questions; the design assumes application permissions and
    isolates token acquisition behind one function so the fallback is a local
    change.
  - **Pagination/filtering:** the endpoint returns newest-first pages via
    `@odata.nextLink`. It does not support `$filter` on `createdDateTime` or an
    after-id cursor, so the inbound cursor is a timestamp + id dedupe window,
    applied client-side.
- **The sender identity is not validated by the app.** `POST /api/messages`
  refuses unknown *recipients* but accepts any sender `agent_id`
  (`app/rest.py:172`, `app/db.py:911`). The mirror nonetheless registers its
  posting identity once (`teams_mirror`, owner `teams`) so presence/heartbeat
  semantics stay honest and the roster explains where Teams traffic comes from.
- **Secrets.** The Graph client secret (or refresh token) joins the webhook URL
  as a never-committed env var. Same handling as `TEAMS_WEBHOOK_URL`.

## Approach

Extend `partyline-client/covasant_partyline/teams_mirror.py`; no new module, no
new console script. `run_once` gains an inbound leg executed after the outbound
leg, sharing the same `--interval`, the same `MirrorError` retryable/non-retryable
discipline, and the same dependency-injection pattern (`fetch`/`post` callables,
so tests never touch the network). When Graph credentials are not configured the
mirror behaves exactly as it does today — one-way, zero new failure modes.

## Architecture

```
Partyline app ──GET /api/messages──► [outbound leg: unchanged] ──► Teams channel
     ▲                                                              │
     │                    Teams humans post in the channel          │
     │                                                              ▼
     └────POST /api/messages──── [inbound leg: NEW] ──GET Graph /teams/.../messages
```

One process, one timer, two independent legs per poll. A failure in one leg is
logged and does not abort the other; each leg has its own cursor.

### Inbound leg flow (`inbound_once`)

1. **Token.** Acquire a Graph token via client credentials
   (`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope
   `https://graph.microsoft.com/.default`). Cache in memory until ~5 min before
   expiry — unlike the Databricks token (re-minted per request because the CLI
   makes it free), the Graph token costs a network round-trip, so caching is
   worth it. 401 mid-poll → drop cache, re-acquire once, then treat as retryable.
2. **Fetch.** `GET /teams/{team-id}/channels/{channel-id}/messages?$top=50`.
   Filter client-side to `createdDateTime > cursor`. Follow `nextLink` only if
   the first page is entirely new (bursts), capped at `MAX_PAGES_PER_POLL` pages
   — same catch-up cap as the outbound leg.
3. **Loop prevention — inbound side.** Skip any message whose `from.application`
   is set (i.e. keep only `from.user`). Cards posted by the Workflow webhook
   carry an application identity; human posts carry a user identity. This is the
   primary echo guard and is identity-based, not content-based.
4. **Map and post** (below), oldest-first, advancing the inbound cursor after
   each accepted message — same at-least-once semantics as outbound: a crash
   after posting to the line but before saving the cursor re-posts that message
   next poll.
5. **First run seeds.** With no inbound cursor, record the newest seen
   `createdDateTime` and post nothing — mirroring the outbound leg's no-replay
   rule. `--inbound-backfill N` exists for deliberate seeding only.

### Mapping Teams message → Partyline post

| Partyline field | Value |
|---|---|
| `agent_id` | `teams_mirror` (registered once per environment) |
| `message_type` | `status` (v1 has no typing; humans answer, agents triage) |
| `summary` | First 140 chars of the stripped text |
| `payload` | Full stripped text, prefixed `**<display name>** (Teams):` |
| `metadata` | `{"source": "teams", "teams_message_id", "teams_user_id", "teams_display_name", "teams_created_at"}` |

- `body.content` is HTML; strip tags (stdlib `html.parser`), unescape entities,
  collapse whitespace. No HTML ever lands on the line.
- Note: `author` on the wire is resolved server-side from the OAuth caller
  (`rest.py:180` `resolve_author`), so the Teams display name must ride in
  `metadata` + the payload prefix, not the `author` field.

### Loop prevention — outbound side

The outbound leg must never mirror a Teams-originated post back into Teams. Add
one filter alongside `message_matches`: skip messages whose
`metadata.source == "teams"` (checked before card construction). The
`agent_id == "teams_mirror"` check alone is insufficient — a human could post to
the line as any id — so the filter keys on the provenance stamp set by the
inbound leg.

Together the two guards make the loop structurally impossible: inbound only
accepts user-authored Teams posts, outbound never re-posts Teams-sourced
messages. A human quoting a card's text by hand is out of scope (that is a human
choosing to echo, not the mirror looping).

### Cursor file

Same file, schema extended (v2), backward compatible:

```json
{
  "last_id": 5982,
  "teams": {
    "last_created_at": "2026-09-23T14:02:11.302Z",
    "recent_ids": ["1762834931302", "..."]
  }
}
```

- Missing `teams` key (existing v1 cursor files) → inbound seeds silently on
  first enabled run, as above.
- `recent_ids` is a bounded dedupe window (last 200 ids) because
  `createdDateTime` has millisecond precision and ties are possible; a message
  at exactly the cursor instant must not double-post.
- Writes stay atomic (existing `tmp` + `replace` in `save_cursor`).

### Config

| Flag | Env | Default |
|---|---|---|
| `--team-id` | `TEAMS_TEAM_ID` | unset → inbound disabled |
| `--channel-id` | `TEAMS_CHANNEL_ID` | unset → inbound disabled |
| `--graph-tenant-id` | `TEAMS_GRAPH_TENANT_ID` | required when inbound on |
| `--graph-client-id` | `TEAMS_GRAPH_CLIENT_ID` | required when inbound on |
| `--graph-client-secret` | `TEAMS_GRAPH_CLIENT_SECRET` | required when inbound on |
| `--inbound / --no-inbound` | `PARTYLINE_TEAMS_INBOUND` | on iff ids+creds present |
| `--inbound-backfill N` | `PARTYLINE_TEAMS_INBOUND_BACKFILL` | 0 |
| `--inbound-top N` | — | 50 (page size) |

Inbound is strictly opt-in-by-configuration: no flag flips a one-way deployment
into two-way without the operator supplying Graph credentials. `--dry-run` covers
both legs (prints would-be line posts, moves no cursor).

### Error handling

- Graph/network errors → `MirrorError(retryable=True)`, leg skipped this
  interval, outbound unaffected.
- Graph 429 → honor `Retry-After` if present, else retryable skip; never
  tight-loop.
- Graph 401 → drop cached token, one re-acquire attempt, then retryable skip.
- Graph 403 (consent/approval missing) → non-retryable: log a loud, actionable
  error (the exact consent URL to grant) and disable the inbound leg for the
  process lifetime rather than failing the poll every minute. Outbound keeps
  running — losing inbound must never silence the alerts that already work.
- Partyline POST refusal (400) on an inbound message → skip that message with an
  error log (same semantics as outbound's skipped cards); cursor still advances.

### Testing

Same injection pattern as `test_teams_mirror.py` — `inbound_once` takes
`fetch_graph` and `post_line` callables. New `test_teams_inbound.py` covering:

- seed-no-replay on first run, with and without backfill
- timestamp cursor + `recent_ids` tie dedupe
- `from.application` posts are never mirrored (echo guard)
- `metadata.source == "teams"` messages never go back out (round-trip guard:
  feed an inbound-mapped message into the outbound filter, assert skipped)
- HTML stripping (tags, entities, `<attachment>` cards dropped)
- 429/401/403 leg behavior; outbound leg survives inbound failure
- cursor file v1 → v2 upgrade (missing `teams` key)

## Rollout

1. Land behind config (inbound off without Graph creds — zero risk to the
   existing one-way deployment).
2. Create the app registration, grant + consent `ChannelMessage.Read.All`
   (initiate protected-API approval if required — this is the long pole; start
   it before implementation finishes).
3. `--dry-run` against the real channel, verify cards/line posts by eye.
4. Enable for real; post a `status` to the line announcing two-way is live so
   agents know Teams humans can now answer them.

## Open Questions

1. **Application vs delegated Graph auth.** Application permission is cleaner
   operationally but may need Microsoft's protected-API approval; delegated
   works today but burdens the mirror with a refresh-token secret. Decide at
   app-registration time; the token acquisition seam keeps the swap local.
2. **Threads.** Humans will naturally reply under a mirrored card. v2 could poll
   `/messages/{id}/replies` for cards the mirror posted (tracking card message
   ids in the cursor) and post them with `reply_to_id` = the original Partyline
   message — that is where real two-way *conversations* happen. Deferred: it
   multiplies Graph calls per poll.
3. **Mentions.** v2 could match `@name` in Teams text against registered agent
   ids (one `GET /api/agents` per poll) and set `mentions`/`recipient`, turning
   a Teams reply into something that actually wakes an agent. Deferred, but the
   mapping table leaves room for it.
