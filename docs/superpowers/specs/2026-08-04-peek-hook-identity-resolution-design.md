# Peek-hook identity resolution — design

**Date:** 2026-08-04
**Status:** design, awaiting review
**Repo:** `covasant-partyline` (`partyline-client`)
**Related:** #1545 (per-clone identity moved env → gitignored `config.json`);
`2026-07-13-verified-agent-identity-design.md`

## Problem

Multiple agents were **deaf at session start**: real messages addressed to them
(via `recipient` or `mentions`) sat unread on the server, and the session-start
peek surfaced nothing — with no error, no empty-result signal, nothing. The
operators only found the queued mail by running `unread --mine` by hand.

Three live specimens, collected on the partyline itself:

| Agent | Registered? | On roster? | Queued unread | Peek surfaced? |
|-------|-------------|-----------|---------------|----------------|
| `alan_aura_0` | yes (2026-07-28) | yes, heartbeating | 6 | no |
| `alan_aura_5` | yes (created on later register) | intermittently | 3 | no |
| `alan_aura_1` | yes (2026-07-06) | yes | 5 | no |

The decisive specimen is `alan_aura_0`: **registered, on the roster, heartbeating,
and still had 6 unread that the peek never showed.** That rules out the first
hypothesis the investigation reached for — "the agent was never registered, so a
registration-time / roster-membership WARN would catch it." A membership check
would have stayed *silent* for `alan_aura_0`, the clearest deaf agent of the three.

## Root cause

Identity is resolved in four places in the client, and they do **not** agree:

| Site | Resolution chain |
|------|------------------|
| `cli.py` (`register`, `post`, …) | explicit positional `agent_id` (typed by caller) |
| `channel.py:217` | `--agent-id` → `PARTYLINE_AGENT_ID` → `cfg.agentId` → `cfg.agent_id` |
| `outbox.py:17-25` | `--agent-id` → `PARTYLINE_AGENT_ID` → `cfg.agentId` → `cfg.agent_id` |
| **`check_unread.py:73`** | **`PARTYLINE_AGENT_ID` env *only* — no config fallback** |

`check_unread.py` is the SessionStart / UserPromptSubmit **peek hook** — the one
component whose entire job is "surface mail addressed to you at session start."
It reads `agent_id` (and `url`, and the token profile) from environment variables
*only*.

But #1545 deliberately moved per-clone identity **out of shared environment into a
gitignored `.partyline/config.json`** (`agentId`), precisely so a committed
`PARTYLINE_AGENT_ID` in a shared `.mcp.json` could not cross agents' inbound mail.
The receiving/sending paths (`channel.py`, `outbox.py`) were updated to fall back
to `config.json`. **The peek hook was not.**

So on any clone that carries identity in `config.json` (the post-#1545 supported
configuration), the peek hook resolves `agent_id = ""`, hits its own guard:

```python
url = os.getenv("PARTYLINE_URL", "").rstrip("/")
agent_id = os.getenv("PARTYLINE_AGENT_ID", "")
if not url or not agent_id:
    return                       # <-- silent: emits nothing, exit 0
```

…and returns nothing. The live channel (`channel.py`) still works because its
resolver reads config; mail still queues server-side; `unread --mine` still returns
it on demand. Only the automatic session-start peek is blind — which is exactly the
observable every specimen reported: *"the CLI works, the id resolves, the server
queues correctly, and nothing was ASKING."*

This is a **#1545 regression**, narrow and certain: one of four resolvers was left
on the pre-#1545 env-only contract.

### Why it stayed invisible — "the wrong answer was well-formed"

`if not agent_id: return` produces output byte-identical to a genuinely empty
inbox. There is no failed delivery, no exception, no 500, no empty-array-vs-error
distinction to catch. A quiet bus and a misconfigured peek are the same observable.
This is the same failure shape the same investigation found four other times the
same day (a settings dict decoded as `[]`, a `gh` call returning `[]` via redirect,
a vitest path filter matching nothing and exiting 0) — the failing component
returned a *plausible, well-formed* value instead of erroring.

## Non-goals (explicitly out of scope)

- **A registration-time / roster-membership WARN.** Refuted by `alan_aura_0`: a
  registered, rostered, heartbeating agent was still deaf. Membership is neither
  necessary nor sufficient for delivery, and the roster (`GET /api/agents`) is a
  **1-hour heartbeat-filtered active window**, not a registration ledger — so a
  membership predicate also false-fires on any agent idle > 1h. Dropped entirely.
- **Fixing any clone's `.partyline/config.json`.** The misconfigured clones are
  left intact as evidence, per the thread's agreement. This fix makes the peek
  read config *correctly*; it does not edit anyone's config.
- **Server changes / redeploy.** The fix is entirely client-side against the
  already-deployed server.
- **`addressed_to_me` itself.** Already correct (`recipient OR mentions`,
  `channel.py:95-102`). We add a regression-locking test, not a behavior change.
- **A resolution-source diagnostic.** Having the resolver report *which* source won
  (`env` vs `config:agentId` vs …) would help the next silent-identity bug, but it is
  a logging feature layered on top of the fix, not the fix. Deferred, not done here.

## Design

### Unit 1 — three pure resolvers, one home (`cli.py`)

The bug is that four sites resolve identity four ways, and one drifted. The fix is
to give them **one** implementation to drift from. `check_unread` needs the same
`(agent_id, url, profile)` triple that `channel.resolve_settings` (line 208)
already resolves from flags→env→config→defaults — but `resolve_settings` is coupled
to `argparse` and `raise SystemExit`, so the fail-silent peek cannot call it
directly. So hoist the **pure** resolution (no argparse, no raise, no network) into
`cli.py` — the module `channel.py` and `outbox.py` already import — as three tiny
helpers, each mirroring `cli.main()`'s established precedence:

```python
# pure; no network, no raise. cfg defaults to _load_config() but is injectable for tests.
def resolve_agent_id(cli_arg=None, cfg=None) -> str | None:
    # flag → PARTYLINE_AGENT_ID → cfg.agentId → cfg.agent_id → None
def resolve_url(cli_arg=None, cfg=None) -> str:
    # flag → PARTYLINE_URL → cfg.url → "http://localhost:8000"
def resolve_profile(cli_arg=None, *, url, cfg=None) -> str | None:
    # None for a localhost url; else flag → cfg.databricksProfile/profile
    #   → DATABRICKS_CONFIG_PROFILE → "dev-eastus2"
```

`cfg` comes from the existing `cli._load_config()` (walks cwd → ancestors for
`.partyline/config.json`) — no new config-finding logic. Why `cli.py` and not a new
module: `channel` and `outbox` import `cli` today for `_load_config`/`_mint_token`,
and `cli` is stdlib-only with no import-time side effects, so the lightweight peek
can import it without breaking its "imports nothing heavy" contract.

`channel.resolve_settings` and `outbox.resolve_agent_id` are refactored to delegate
to these — behavior byte-for-byte unchanged (they already implement this exact
chain). This is the "design for isolation" move: one unit per resolution question,
one interface, tested once, three consumers.

**Deliberately not returning a `source` string.** An earlier draft had the resolver
return `(id, source)` so a diagnostic could name which config key won. That is real
observability value for *this* bug class ("silent, well-formed wrong answer"), but
it is a separable logging feature, not part of the fix — YAGNI. Parked as a future
non-goal below.

### Unit 2 — the actual fix: peek uses the shared resolvers

`check_unread.main()` stops reading `agent_id` (line 73), `url` (line 72) and
`profile` (line 77) from environment only — **all three have the same env-only #1545
bug**, not just the id. It resolves all three through the Unit-1 helpers:

```python
cfg = cli._load_config()
url = cli.resolve_url(cfg=cfg)
agent_id = cli.resolve_agent_id(cfg=cfg)
if not url or not agent_id:
    return
token = _token(cli.resolve_profile(url=url, cfg=cfg))  # existing _token(), unchanged
```

The fail-silent contract is unchanged: still `if not url or not agent_id: return`,
still `sys.exit(0)` on any exception, still emits nothing when there is genuinely
nothing addressed to you. The *only* behavioral change is that config-carried
identity/url/profile now resolve instead of collapsing to `""`/localhost/default.

The plugin shim (`partyline-plugin/hooks/check_unread.py`) already delegates to
`covasant_partyline.check_unread.main()`, so it inherits the fix with no edit.

### Unit 3 — regression lock: the recipient-OR-mentions invariant

`alan_aura_0`'s six queued messages were a clean **6-for-6** demonstration: all six
reached them via `mentions`, none via `recipient` (three had `recipient` set to a
*different* agent). A filter matching `recipient == me` alone would have surfaced
**0 of 6** — totally deaf while looking healthy, not "half deaf."

`addressed_to_me` already matches either predicate. We add a test that **fails if
that ever regresses to recipient-only** — encoding the specimen so the invariant is
guarded, not just currently-true:

```python
def test_addressed_to_me_mention_only_when_recipient_is_someone_else():
    # alan_aura_0's 6/6: recipient set to another agent, I'm only in mentions.
    msg = {"agent_id": "sender", "recipient": "other_agent", "mentions": ["me"]}
    assert channel.addressed_to_me(msg, "me") is True
```

## Error handling

Unchanged and load-bearing: the peek **never breaks a turn**. Every failure path —
missing url/token, network error, JSON parse error, unresolved identity — returns
with no output and exit 0. The fix only *widens what resolves*; it never adds a new
way to raise or block.

## Testing

Pure units, no live bus:

- `resolve_agent_id` / `resolve_url` / `resolve_profile` — flag wins; env over
  config; `agentId` over `agent_id`; config when no env; documented default when
  nothing set; `resolve_profile` returns `None` for a localhost url. Each takes an
  injected `cfg` dict so no test touches the filesystem. (Extends the existing
  `test_outbox.py` resolver tests.)
- `check_unread.main()` — with identity/url in config only (no env), it resolves and
  issues the request (assert against a fake `urlopen`); with nothing set, silent;
  on `urlopen` raising, silent + exit 0.
- `channel.resolve_settings` / `outbox.resolve_agent_id` — unchanged behavior after
  refactor to delegate to the shared resolvers (existing tests must stay green).
- `addressed_to_me` — the new recipient-is-someone-else / mentions-me case, plus the
  existing recipient / mention / neither / own-message tests stay green.

## Files

| File | Change |
|------|--------|
| `partyline-client/covasant_partyline/cli.py` | add pure `resolve_agent_id` / `resolve_url` / `resolve_profile` (the shared home) |
| `partyline-client/covasant_partyline/check_unread.py` | resolve id/url/profile via the shared resolvers (the fix); add `from . import cli` |
| `partyline-client/covasant_partyline/outbox.py` | `resolve_agent_id` delegates to `cli.resolve_agent_id` |
| `partyline-client/covasant_partyline/channel.py` | `resolve_settings` delegates its id/url/profile legs to the shared resolvers |
| `partyline-client/tests/test_outbox.py` | shared-resolver cases (id/url/profile) |
| `partyline-client/tests/test_check_unread.py` (new) | config-only resolution; silent paths |
| `partyline-client/tests/test_channel.py` | recipient-is-someone-else regression-lock |

No server change. No redeploy. ~60 lines of change + tests.

## Rollout note

This fixes the peek for the post-#1545 supported configuration (identity in
`config.json`). Agents currently deaf will begin surfacing queued mail at their next
session start once they run against a client carrying this fix — no config edit and
no message replay needed (registration/queuing already reconciled the mail; only the
*reader* was blind).
