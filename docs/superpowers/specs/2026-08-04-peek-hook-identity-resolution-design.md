# Peek-hook identity resolution — design

**Date:** 2026-08-04 (revised 2026-08-05 after code review)
**Status:** implemented on `spec/1545-peek-identity`
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

Identity is resolved in **five** places in the client, and they do **not** agree:

| Site | Resolution chain |
|------|------------------|
| `cli.py` (`register`, `post`, …) | explicit positional `agent_id` (typed by caller) |
| `channel.py:217` | `--agent-id` → `PARTYLINE_AGENT_ID` → `cfg.agentId` → `cfg.agent_id` |
| `outbox.py:17-25` | `--agent-id` → `PARTYLINE_AGENT_ID` → `cfg.agentId` → `cfg.agent_id` |
| **`check_unread.py:73`** | **`PARTYLINE_AGENT_ID` env *only* — no config fallback** |
| **`hook.py:112`** | **`PARTYLINE_AGENT_ID` env *only* — no config fallback** |

`check_unread.py` is the SessionStart / UserPromptSubmit **peek hook**, and `hook.py` is
the SessionStart **orientation hook** (the one actually wired in this repo's
`.claude/settings.json`). Between them they are the whole automatic-awareness surface —
and both read `agent_id` (and `url`, and the profile) from environment variables *only*.

But #1545 deliberately moved per-clone identity **out of shared environment into a
gitignored `.partyline/config.json`** (`agentId`), precisely so a committed
`PARTYLINE_AGENT_ID` in a shared `.mcp.json` could not cross agents' inbound mail.
The receiving/sending paths (`channel.py`, `outbox.py`) were updated to fall back
to `config.json`. **Neither hook was.**

So on any clone that carries identity in `config.json` (the post-#1545 supported
configuration), the peek hook resolves `agent_id = ""`, hits its own guard:

```python
url = os.getenv("PARTYLINE_URL", "").rstrip("/")
agent_id = os.getenv("PARTYLINE_AGENT_ID", "")
if not url or not agent_id:
    return                       # <-- silent: emits nothing, exit 0
```

…and returns nothing. Confirmed on disk: `aura/.partyline/config.json` carries
`agentId: alan_aura_0` and a `url`, while `aura/.claude/settings.json` sets no
`PARTYLINE_*` at all — so specimen 1's peek asked for `""` on every session start.

The live channel (`channel.py`) still works because its resolver reads config; mail still
queues server-side; `unread --mine` still returns it on demand. Only the automatic
session-start surfaces are blind — which is exactly the observable every specimen
reported: *"the CLI works, the id resolves, the server queues correctly, and nothing was
ASKING."*

This is a **#1545 regression**, narrow and certain: two of five resolvers were left
on the pre-#1545 env-only contract.

### `hook.py` fails louder than silence

`territory_warning(claims, cwd, agent_id)` skips your own claims with
`c.get("agent_id") == agent_id`. With `agent_id = ""` that comparison never matches, so
the hook reports **your own claimed directory as held by another agent** — naming you as
the holder. A well-formed wrong answer, printed at every session start.

### Why the peek stayed invisible — "the wrong answer was well-formed"

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
  **1-hour heartbeat-filtered active window** (`db.list_agents(active_within_seconds=3600)`),
  not a registration ledger — so a membership predicate also false-fires on any agent idle
  > 1h. Dropped entirely.
- **Fixing any clone's `.partyline/config.json`.** The misconfigured clones are
  left intact as evidence, per the thread's agreement. This fix makes the readers
  read config *correctly*; it does not edit anyone's config.
- **Server changes / redeploy.** The fix is entirely client-side against the
  already-deployed server.
- **The addressing predicate itself.** Already correct in both copies — the server SQL
  (`db.get_since`: `recipient = %s OR %s = ANY(mentions)`) and the channel's Python
  (`channel.addressed_to_me`). We add regression-locking tests, not a behavior change.
- **A resolution-source diagnostic.** Having the resolver report *which* source won
  (`env` vs `config:agentId` vs …) would help the next silent-identity bug, and the
  `alan_aura_1` case below is the argument for it — but it is a logging feature layered on
  top of the fix, not the fix. Deferred, not done here.

## Design

### Unit 1 — three pure resolvers, one home (`cli.py`)

The bug is that five sites resolve identity five ways, and two drifted. The fix is
to give them **one** implementation to drift from. `check_unread` needs the same
`(agent_id, url, profile)` triple that `channel.resolve_settings` already resolves from
flags→env→config→defaults — but `resolve_settings` is coupled to `argparse` and
`raise SystemExit`, so the fail-silent peek cannot call it directly. So hoist the **pure**
resolution (no argparse, no raise, no network) into `cli.py` — the module `channel.py` and
`outbox.py` already import — as four tiny helpers:

```python
# pure; no network, no raise. cfg defaults to _load_config() but is injectable for tests.
def is_local_url(url) -> bool:              # localhost / 127.* — needs no token or profile
def resolve_agent_id(cli_arg=None, cfg=None) -> str | None:
    # flag → PARTYLINE_AGENT_ID → cfg.agentId → cfg.agent_id → None
def resolve_url(cli_arg=None, cfg=None) -> str:
    # flag → PARTYLINE_URL → cfg.url → "http://localhost:8000"; trailing slash stripped
def resolve_profile(cli_arg=None, *, url, cfg=None) -> str | None:
    # None for a local url; else flag → PARTYLINE_PROFILE → cfg.databricksProfile/profile
    #   → DATABRICKS_CONFIG_PROFILE → "dev-eastus2"
```

`cfg` comes from the existing `cli._load_config()` (walks cwd → ancestors for
`.partyline/config.json`) — no new config-finding logic. Why `cli.py` and not a new
module: `channel` and `outbox` import `cli` today for `_load_config`/`_mint_token`, and
`cli` is stdlib-only, so the lightweight hooks can import it without breaking their
"imports nothing heavy" contract.

**Two behavior changes are owned here, not incidental.** The four existing chains were
*not* byte-identical, so unifying them had to pick a winner:

| Divergence | Was | Now |
|---|---|---|
| local-url test | `cli.main`: `localhost` or `127.`; `channel`: `localhost` only | both: `localhost` or `127.` |
| profile config key | `cli.main`: `databricksProfile`; `channel`: `databricksProfile` or `profile` | both: either key |
| `PARTYLINE_PROFILE` | read by `check_unread` only | read by all, ranked with the flag |

Each is pinned by a test. `PARTYLINE_PROFILE` ranks *above* config (it is
partyline-specific intent, like the flag) while `DATABRICKS_CONFIG_PROFILE` stays *below*
config (it is ambient, often targets another workspace, and would 401).

### Unit 2 — the fix: the peek uses the shared resolvers

`check_unread.main()` stops reading `agent_id`, `url` and `profile` from environment only
— **all three have the same env-only #1545 bug**, not just the id:

```python
cfg = cli._load_config()
url = cli.resolve_url(cfg=cfg)
agent_id = cli.resolve_agent_id(cfg=cfg)
if not url or not agent_id:
    return
token = _token(cli.resolve_profile(url=url, cfg=cfg))
if not token and not cli.is_local_url(url):
    return
```

Two details the first draft got wrong:

- **`resolve_profile` returns `None` for a local url**, so the old unconditional
  `if not token: return` would have made the peek go *silent on localhost* — a new
  fail-silent path in the component being de-silenced, and in the one configuration you'd
  use to test it. Hence the `is_local_url` half of the guard, and `_token(None)` returning
  `None` instead of shelling out with `-p None`. The `Authorization` header is now omitted
  rather than sent as `Bearer None`.
- **`PARTYLINE_PROFILE` must survive.** It is documented in this module's own docstring,
  in the plugin README, in the skill, and set in this repo's `.claude/settings.json`.
  Dropping it would have been a second silent-wrong-value regression.

The fail-silent contract is otherwise unchanged: still returns on unresolved identity,
still `sys.exit(0)` on any exception, still emits nothing when there is genuinely nothing
addressed to you. The plugin shim (`partyline-plugin/hooks/check_unread.py`) already
delegates to `covasant_partyline.check_unread.main()`, so it inherits the fix with no edit.

### Unit 3 — the same fix for `hook.py`

Identical treatment for the fifth resolver: flags default to `None`, then resolve through
`cli.resolve_*`; `_token` gains the same `None`-profile guard; `_get` omits the auth header
when tokenless; `territory_warning` receives the *resolved* id so your own claims are
recognised as yours.

### Unit 4 — regression-lock the addressing invariant, on both copies

`alan_aura_0`'s six queued messages were a clean **6-for-6** demonstration: all six
reached them via `mentions`, none via `recipient` (three had `recipient` set to a
*different* agent). A filter matching `recipient == me` alone would have surfaced
**0 of 6** — totally deaf while looking healthy, not "half deaf."

The predicate exists **twice**, and the load-bearing copy is the one the first draft
missed: the peek delegates addressing to the server (`only_for_me=true` →
`db.get_since(for_agent=…)` → SQL), and does not call `channel.addressed_to_me` at all.
So both get a lock:

- `tests/test_query_messages.py` — DB-backed: a message with `recipient` set to another
  agent and me only in `mentions` **is** returned by `get_since(for_agent=me)`; and a
  bystander still doesn't see it (so the lock can't be satisfied by passing everything).
- `partyline-client/tests/test_channel.py` — the same specimen against
  `addressed_to_me`.

Both were verified by mutation: narrowing either copy to `recipient`-only fails the
corresponding test.

## Error handling

Unchanged and load-bearing: the hooks **never break a turn**. Every failure path —
missing url/token, network error, JSON parse error, unresolved identity — returns
with no output and exit 0. The fix only *widens what resolves*; it never adds a new
way to raise or block.

## Testing

Pure units, no live bus (except the DB-backed filter lock, which uses the existing
local-Postgres `client` fixture):

- `resolve_agent_id` / `resolve_url` / `resolve_profile` — flag wins; env over config;
  `agentId` over `agent_id`; config when no env; documented default when nothing set;
  trailing slash stripped; `None` profile for both `localhost` and `127.*`;
  `PARTYLINE_PROFILE` over config; config over `DATABRICKS_CONFIG_PROFILE`. Each takes an
  injected `cfg` dict, so no test touches the filesystem. (`tests/test_cli.py`.)
- `check_unread.main()` — identity/url/profile from config only (no env) resolves and
  issues the request; env still shadows config; `PARTYLINE_PROFILE` honoured; a local app
  peeks tokenless; no double slash; silent with no identity, silent on a missing remote
  token, silent on `urlopen` raising, silent (but *asking*) on an empty inbox.
- `hook.main()` — own claim not flagged; another agent's claim still flagged; url and
  profile from config; local app runs tokenless; flag still wins.
- `channel.resolve_settings` / `outbox.resolve_agent_id` — unchanged behavior after the
  refactor to delegate (existing tests stay green).
- A `clean_env` fixture drops the ambient `PARTYLINE_*`/`DATABRICKS_*` vars. This is
  load-bearing, not hygiene: a real session exports `PARTYLINE_AGENT_ID` via
  `.claude/settings.json`, and pytest inherits it — without the fixture a config-fallback
  test would assert against the ambient env and pass for the wrong reason.

## Files

| File | Change |
|------|--------|
| `partyline-client/covasant_partyline/cli.py` | add pure `is_local_url` / `resolve_agent_id` / `resolve_url` / `resolve_profile` (the shared home) |
| `partyline-client/covasant_partyline/check_unread.py` | resolve id/url/profile via the shared resolvers (the fix); tokenless local; `_token(None)` guard |
| `partyline-client/covasant_partyline/hook.py` | same fix for the fifth resolver; resolved id reaches `territory_warning` |
| `partyline-client/covasant_partyline/outbox.py` | `resolve_agent_id` delegates to `cli.resolve_agent_id` |
| `partyline-client/covasant_partyline/channel.py` | `resolve_settings` delegates its id/url/profile legs |
| `tests/test_cli.py` | shared-resolver cases (id/url/profile) |
| `tests/test_check_unread.py` (new) | config-only resolution; profile precedence; silent paths |
| `tests/test_hook.py` | config identity; own-claim vs other-claim territory warning |
| `tests/test_query_messages.py` | server-side `recipient OR mentions` lock |
| `tests/conftest.py` | `clean_env` + `config_clone` fixtures |
| `partyline-client/tests/test_channel.py` | channel-side `recipient OR mentions` lock |
| `partyline-plugin/README.md`, `skills/using-partyline/SKILL.md` | config-first identity; corrected resolution-order table |

No server change. No redeploy.

## Rollout note

This fixes the readers for the post-#1545 supported configuration (identity in
`config.json`). Agents deaf *because their clone carries identity only in config* —
specimens `alan_aura_0` and `alan_aura_5` — begin surfacing queued mail at their next
session start once they run a client carrying this fix, with no config edit and no message
replay (registration/queuing already reconciled the mail; only the *reader* was blind).

**It does not fix a clone whose env id disagrees with its config id.** This repo is that
case: `.claude/settings.json` sets `PARTYLINE_AGENT_ID=alan_partyline_1` while
`.partyline/config.json` says `agentId: alan_aura_1` (specimen 3). Env shadows config by
design, so the peek here still reads the line as `alan_partyline_1` and `alan_aura_1`'s
five messages stay invisible. That class needs either a config/env reconciliation (an
operator action, deliberately out of scope — the clones are evidence) or the deferred
resolution-source diagnostic, which is what would make the disagreement visible instead of
merely wrong.
