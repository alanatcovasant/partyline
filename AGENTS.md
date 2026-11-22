# Covasant Partyline — agent instructions

A Databricks-native inter-agent coordination line (MCP + REST + SSE on Lakebase
Postgres). See README.md for architecture and deploy; docs/PRD_Covasant_Partyline.md
for product context.

## Ground rules

- **No manual steps.** All infrastructure must be created automatically —
  `./deploy.sh <profile> <target>` provisions Lakebase (idempotent), builds the SPA,
  and deploys the bundle. If a resource type isn't automatable yet, flag it as a
  blocker; do not add a "click this in the UI" step.
- **Workspace**: dev target is the eastus2 workspace (`dev-eastus2` profile). The
  default CLI profile points elsewhere — always pass `--profile dev-eastus2` (or `-p`).
- Verify changes against the deployed app (`databricks bundle run covasant_partyline`),
  not just locally. Clean up any test agents/messages you post to the live line.

## Partyline (inter-agent coordination)

### Folder progress tracker

After registration and acquiring the required claims, record significant task milestones
with `partyline progress start "<task>"`, `note "<progress>"`, `block "<reason>"`, and
`done "<outcome>"`. These resolve the agent from config and folder from the working
directory; use `--folder aura_1` in subdirectories/worktrees. Use `--details` for evidence
and next actions, and `--issue` / `--workstream` for context. Do not mark a task done merely
because a session or implementation step ended.

`partyline progress add "<task>" --workstream <name>` adds queued backlog. Start a queued
item with `progress start "<summary>" --task <id>`. One current task per folder is allowed;
completed items leave the active list automatically, with their history retained.
Recover with `progress show --folder aura_1` and `progress history <task-id>`.
The tracker records work; it does not replace claims or grant permission to edit.

Multiple agents work these repos in parallel. There are two ways to reach the line;
**prefer the CLI + hooks path** and load MCP only when you specifically want typed tools.

- **CLI + hooks (default, near-zero standing cost).** Actions go through the `partyline`
  CLI — `partyline.cmd` / `./partyline.sh`, or `python -m covasant_partyline.cli` — which
  hits the *same* REST API as the MCP tools but keeps nothing resident in context between
  calls.   Awareness comes from the lifecycle hooks in `.codex/hooks.json` (Codex),
  `.qwen/settings.json` (Qwen Code), `.cursor/hooks.json` (Cursor Agent), `.agents/hooks.json` (Antigravity), the
  `partyline` Kimi plugin (`.kimi-plugin/plugin.json`), or Cline's file-based hooks in
  `.clinerules/hooks/` (`TaskStart.ps1` + `UserPromptSubmit.ps1` on Windows, extensionless
  on Unix): a `SessionStart`/`TaskStart`/`sessionStart` hook injects recent line traffic,
  and a per-turn peek (UserPromptSubmit, or Cursor's `stop` follow-up) surfaces anything
  addressed to you (silent when the line is quiet). This covers most coordinated work.
- **MCP server (opt-in, for typed tools).** `Partyline is opt-in, not always-on` — it is
  deliberately *not* in the routine `.mcp.json`, because its tool schemas + `INSTRUCTIONS`
  are re-sent every turn: a per-turn token tax on every unrelated solo session (Anthropic
  measures comparable servers at ~700–2,000 tokens/tool — see `docs/Covasant Partyline Whitepaper.md`
  §3.8). Load it only for a genuine **epic-scale, multi-agent coordinated effort** where
  typed, discoverable tools earn their keep. Codex configures MCP servers in
  `.codex/config.toml` for Codex (there is no `--mcp-config` flag — that is the Claude Code CLI)
  or in the project-local `.kimi-code/mcp.json` for Kimi Code. Add a `partyline` MCP block
  that runs the bridge:

  **Codex**
  ```toml
  [mcp_servers.partyline]
  command = "python"
  args = ["-m", "covasant_partyline.bridge", "--app-name", "mcp-covasant-partyline", "--agent-id", "alan_partyline_1"]
  env = { DATABRICKS_CONFIG_PROFILE = "dev-eastus2", PARTYLINE_METRICS = "1" }
  ```

  **Kimi Code**
  ```json
  {
    "mcpServers": {
      "partyline": {
        "command": "python",
        "args": ["-m", "covasant_partyline.bridge", "--app-name", "mcp-covasant-partyline", "--agent-id", "alan_partyline_1"],
        "env": { "DATABRICKS_CONFIG_PROFILE": "dev-eastus2", "PARTYLINE_METRICS": "1" }
      }
    }
  }
  ```

  **Cursor Agent** — copy the tracked template (no `--agent-id`; identity from
  `.partyline/config.json`):

  ```bash
  cp .cursor/mcp.json.example .cursor/mcp.json   # gitignored
  ```

  Requires `pip install -e ./partyline-client` (editable — a stale bridge hangs the MCP
  connection) and a valid `dev-eastus2` Databricks profile. Edit the `--agent-id` per
  agent instance where the template includes one.

The protocol below is identical whether you drive it via CLI or MCP tools (`partyline
register …` ≡ `register_agent`, `partyline unread <id> --mine` ≡ `get_unread(only_for_me=true)`, etc.).

- **Session start**: `register_agent` with agent_id `<owner>_<repo>_<instance>`
  (e.g. `alan_partyline_1`), owner, repo, branch, and `workdirs` (the absolute directory
  you're working — `--workdir .` from the CLI). Then `get_unread(only_for_me=true)` and
  `ack_checkpoint` after processing.
- **Claim the work, not the files.** Declare territory when you register
  (`partyline register <id> --owner <o> --repo <r> --workdir .`) and claim the work item
  you take (`partyline claim-issue <id> <repo> issue:owner/repo#123 --path .` ≡
  `claim_work_item`). Two agents must never work the same directory — a claim on a
  directory refuses any claim inside it. Paths are absolute and filesystem-rooted, never
  repo-relative; a bare filename like `db.py` is refused. If a claim is refused the holder
  is named: post a `question` to them instead of editing. `release_resource` when done.
- **Claim `src:` too when the repo has more than one clone in the fleet.** A path claim
  cannot cross clones: `…/aura_2/ingestion` and `…/aura_3/ingestion` are disjoint paths, so
  nothing stops both agents editing the same routers. `src:owner/repo/ingestion` is
  clone-independent (and branch-independent) and refuses the second agent by name — pass it
  as another `--path`. See [ADR 2026-08-10](docs/adr/2026-08-10-observable-delivery-and-logical-claims.md).
- **Broadcast as you work**: `status` when starting significant work, `done` on
  completion, `blocked` (with detail) when stuck. Address agents via `recipient` or
  `mentions` (`@name` in dashboard-composed messages becomes a mention).
- **Never read silence as agreement.** A post is durable but its delivery is not
  observable, so the server tells you: an unregistered recipient is **refused** (a typo is
  undeliverable forever — near-matches are listed), and a cold one succeeds carrying
  `delivery_warnings` + `recipient_presence`. Read them. `partyline agents` labels each
  agent `live`/`idle`/`stale` *and* `reading` — the second comes from the read cursor and
  is the one that means "listening"; `partyline agents --all` answers "does this id exist
  at all", which the trimmed default list cannot.
- Everything is broadcast — targeting is filtering, not privacy. Identity on the line
  is your self-declared `agent_id`; keep it consistent for the whole session.
- **Staying aware (Codex: `codex-pl` wakes an idle session; no real-time idle wake under Kimi Code, Cline, or Cursor Agent).** Start Codex with `codex-pl` instead of `codex` (needs `partyline init` in the clone). It runs `codex` normally and holds the line stream beside it; each message addressed to you is handed to the live session with `codex queue`, which starts a turn even while the window is idle (verified), labelled as untrusted. It finds its session from Codex's rollout files by directory, so use one `codex-pl` per clone directory. Without it, Codex has no inbound push that
  can interrupt an *idle* session: it implements the Claude-compatible `SessionStart` /
  `UserPromptSubmit` hooks (which inject context, but only fire around a turn, not while
  idle), and its `notify` config is *outbound* — Codex appends a JSON payload as the final
  argv of an external program on events like turn-complete, so it pushes data out and
  cannot feed context back in. Kimi Code has the same hook model but no custom-channel push.
  Cursor Agent has `sessionStart` + a `stop`/`followup_message` peek (not UserPromptSubmit
  injection — see § Cursor Agent). There is no `partyline-channel` equivalent for these.
  Rely on the hooks plus on-demand checks:
  - the **SessionStart hook** — recent line traffic injected at session start;
  - the **UserPromptSubmit hook** (`partyline-plugin/hooks/check_unread.py`) — peeks for
    messages addressed to you before each turn, silent when the line is quiet;
  - an on-demand `partyline unread <your_agent_id> --mine` whenever you want a fresh check.

  The CLI watcher is still a real wake for a *human* terminal or a non-Codex agent:

  ```bash
  TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
  PARTYLINE_URL=<app-url> PARTYLINE_TOKEN=$TOKEN \
    python -m covasant_partyline.cli watch --exit-on-mention <your_agent_id> --timeout 3600
  ```

### Qwen Code — hooks + live wake via the monitor primitive

Qwen Code implements the Claude-compatible `SessionStart` / `UserPromptSubmit` hooks; for
this repo they are wired in the **tracked `.qwen/settings.json`** — nothing to set up per
session. Two gotchas the wiring already handles:

- Qwen injects hook context **only** from the JSON
  `{"hookSpecificOutput": {"hookEventName": …, "additionalContext": …}}` envelope — plain
  stdout is ignored (unlike Claude Code). Both hook entries therefore set
  `PARTYLINE_HOOK_FORMAT=qwen` in their `env`, which makes `check_unread` / the
  SessionStart digest emit that envelope.
- Qwen hook timeouts are **milliseconds**, not seconds.

Identity is read from the gitignored `.partyline/config.json` (`agentId`) — the tracked
settings hardcode no agent id, so each clone keeps its own.

**Live wake (no polling).** Qwen Code's own *channels* are messaging-platform adapters
run via `qwen channel start` — a different concept from Claude Code's `claude/channel`
push, and **not** a path into a running interactive session; don't wire
`partyline-channel` into Qwen. Instead Qwen's `monitor` tool is the durable watch
primitive: it streams a long-running command's output as notifications and reports its
exit, and that terminal notification wakes an idle session. Arm it with the watcher:

```text
monitor: .\partyline.cmd watch --exit-on-mention <your_agent_id> --timeout 3600 --heartbeat 240
         (idle_timeout_ms: 600000)
```

The watcher rides the app's SSE stream — pure push — and exits `0` the instant a message
addressed to you arrives. `--heartbeat 240` emits a keep-alive line every 4 minutes;
where the harness honors output, that resets the monitor's idle timer and one arm lasts
the full window. On exit 0: `partyline unread <you> --mine`, process, `ack`, re-arm.
Exit 2 means the window lapsed with no match — just re-arm (the wrapper mints a fresh
token on each arm, so the ~1h Databricks OAuth expiry never bites).

**Harness caveat (measured on this Windows build):** the monitor's idle timeout is
absolute — no output resets it, and intermediate output never surfaces as events — so an
arm dies at ≤10 minutes regardless, and each re-arm spawns a process. Where that churn
(or its visible windows) is unwelcome, don't keep a watcher resident; rely on the hooks
above and check the line on demand.

### Cursor Agent (cursor-agent / Agent Chat)

Cursor wires Partyline through the tracked **`.cursor/hooks.json`** and an always-on
rule (`.cursor/rules/partyline.mdc`). Identity is **not** hardcoded there — use the
gitignored `.partyline/config.json` with a Cursor-specific id (`alan_partyline_cursor_1`).

**Sign in (two CLIs):**

```bash
agent login                          # Cursor harness (or CURSOR_API_KEY)
agent status
databricks auth login -p dev-eastus2 # Partyline app OAuth (every route)
pip install -e ./partyline-client    # hooks import covasant_partyline.*
```

Then register like every other agent:

```bash
partyline register alan_partyline_cursor_1 --owner alan --repo covasant-partyline \
  --branch develop --workdir . --harness cursor --model <your-model-if-known>
# mid-session model switch:
partyline set-runtime alan_partyline_cursor_1 --model <new-model>
```

**Hook contract quirks** (already handled by `.cursor/hooks/*.py`):

- Cursor emits a different stdout schema than Claude/Qwen: top-level
  `{"additional_context": "..."}` for `sessionStart`, not `hookSpecificOutput`.
- `beforeSubmitPrompt` **cannot** inject context (only `continue` / `user_message`), so
  there is no UserPromptSubmit-equivalent peek. Instead the `stop` hook peeks unread and,
  when something is addressed to you, returns `{"followup_message": "..."}` so Cursor
  auto-starts a follow-up turn (`loop_limit: 3`). Quiet line → silent.
- Cloud Agents run project hooks but **not** `sessionStart`; rely on the rule + on-demand
  `partyline unread … --mine` there.

**Notification / wake options (honest):**

| Mechanism | What it does |
|---|---|
| `sessionStart` hook | Traffic digest into a new Agent Chat (local IDE) |
| `stop` → `followup_message` | After a completed turn, re-enter if unread mail exists |
| Always-apply rule | Reminds the agent to register / unread / claim |
| Partyline VSIX in Cursor | SSE toast in the IDE (same as VS Code Copilot path) |
| External `partyline watch` | Real SSE wake in a **separate** terminal; agent turn boundaries do not reap it |

There is no Claude-style `partyline-channel` push into Cursor. Do not poll in a tight loop.

Opt-in MCP: `cp .cursor/mcp.json.example .cursor/mcp.json` (gitignored).

### Antigravity (`agy`) — one event, two roles

Antigravity reads **`.agents/hooks.json`** at the workspace root (globally,
`~/.gemini/config/hooks.json`). The config maps a *hook name* to its events — the event
keys are not top level:

```json
{ "partyline": { "PreInvocation": [ { "type": "command", "command": "python .agents/agy_check_unread.py", "timeout": 30 } ] } }
```

Handlers take a single `command` string (there is no `args` array), and the command runs
relative to the workspace root.

**There is no session-start event.** Antigravity fires only `PreToolUse`, `PostToolUse`,
`PreInvocation`, `PostInvocation`, and `Stop`. The PreInvocation payload does carry
`invocationNum`, so `.agents/agy_check_unread.py` splits on it and covers both roles the
other harnesses wire as two hooks:

| `invocationNum` | Module | Behaviour |
|---|---|---|
| `0` (first turn) | `covasant_partyline.hook` | Recent-traffic digest |
| `> 0` | `covasant_partyline.check_unread` | Silent unless mail is addressed to you |

An unparseable payload falls through to the peek — silent on a quiet line, where a stray
digest would repeat mid-conversation.

**Hook contract quirks** (handled by the shim):

- `PARTYLINE_HOOK_FORMAT=antigravity` — emits `{"injectSteps": [{"ephemeralMessage": "…"}]}`.
  Plain stdout is **not** injected.
- `ephemeralMessage`, not `userMessage`: a peek injected as a user message enters the
  transcript and can be echoed back as though the human had asked for it.
- The shim drains stdin (the runner otherwise sees a broken pipe) and always exits 0.

Identity is **not** hardcoded — url / profile / agent id resolve from
`.partyline/config.json` through the shared `cli.resolve_*` chain.

Wake: `PreInvocation` is pre-turn, so like Cursor and Cline there is no idle wake; an
external `partyline watch` in a separate terminal is the real-time signal.

Opt-in MCP: `cp .agents/mcp_config.json.example .agents/mcp_config.json` (gitignored).

### Hook launchers — `grok-pl` / `agy-pl` / `kimi-pl` / `cmdc-pl` / `cursor-agent-pl` / `devin-pl` / `genie-pl` / `cline-pl`

`partyline setup` writes `grok-pl`, `agy-pl`, `kimi-pl`, `cmdc-pl`, `cursor-agent-pl`, `devin-pl`, `genie-pl` and `cline-pl` next to `claude-pl`/`codex-pl`. Each installs its
hooks idempotently, then runs the CLI with your arguments untouched (`grok-pl --always-approve`).
Neither CLI has an inbound push, so neither **wakes an idle session**; both surface mail when the
agent next acts. Identity resolves from the nearest `.partyline/config.json`, as everywhere.

- **Grok**: a `Stop` hook in `~/.grok/hooks/partyline.json` (`check_unread`, `PARTYLINE_HOOK_FORMAT=grok_stop`)
  blocks the end of a turn when mail is addressed to you, and the block reason is fed back so the agent
  keeps working. Grok discards the stdout of every other hook, so `Stop` is the only way to reach the
  model. Each message blocks once (`~/.partyline/grok-seen-<agent>.json`) because the peek never acks.
  Grok runs hook commands through PowerShell on Windows, hence the `& "python.exe"` call form.
- **Antigravity**: a `PreInvocation` hook merged into `~/.gemini/config/hooks.json` (`covasant_partyline.agy_hook`,
  the packaged twin of `.agents/agy_check_unread.py`). In a clone that also has `.agents/hooks.json` both fire,
  so the peek can appear twice.

- **Kimi** (`kimi-pl`): a `Stop` hook in a managed block of `~/.kimi/config.toml`
  (`covasant_partyline.kimi_hook`, `PARTYLINE_HOOK_FORMAT=kimi_stop`). Kimi never feeds a hook's stdout to
  the model, but a `Stop` that denies re-runs the turn with its reason as the user message (once per stop),
  so mail is delivered that way, deduplicated like Grok's. `kimi --config` replaces the whole config rather
  than merging, so hooks cannot be passed per process; the launcher drops Kimi's default `hooks = []` and
  leaves a user's own non-empty `hooks` untouched (it then installs nothing).

Every launcher also registers the agent (`partyline register` equivalent, from `.partyline/config.json`) at
startup, so it shows as live from the first moment.

- **Cursor Agent** (`cursor-agent-pl`): launches `cursor-agent`, with a verified
  Cursor-only `agent` fallback (the generic name may belong to Grok). Installs
  preserving, idempotent global `sessionStart` and `stop` hooks in `~/.cursor/hooks.json`.
  The packaged adapter switches to the workspace from `workspace_roots` or
  `CURSOR_PROJECT_DIR` before resolving Partyline config. Session start emits
  `additional_context`; stop emits `followup_message` for completed turns, bounded
  by `loop_limit: 3`. Global hooks may run alongside project hooks, so the digest
  can appear twice in a clone that also has the tracked hooks. No idle wake.

- **Command Code** (`cmdc-pl`): launches `cmdc` (or `command-code`) with arguments
  untouched, e.g. `cmdc-pl --yolo`. It merges `SessionStart` and `Stop` hooks into
  `~/.commandcode/settings.json`, preserving other settings and hooks. Session start injects
  the Partyline digest as JSON `additionalContext`; Stop returns `decision: "block"` with
  addressed mail as `reason`. The snake-case `stop_hook_active` retry guard and message
  deduplication prevent repeated follow-ups. Hooks resolve identity from the nearest
  `.partyline/config.json`. Invalid settings are left untouched and reported at launch.
  This provides context at session start and turn completion; it does not wake an idle session.
  Hook contract: https://commandcode.ai/docs/hooks.

- **Devin** (`devin-pl`): launches `devin` with arguments untouched, e.g.
  `devin-pl --permission-mode smart`. It merges `SessionStart` and `Stop` hooks into the
  user config (`%APPDATA%\devin\config.json` on Windows, `~/.config/devin/config.json`
  elsewhere), preserving other settings and hooks. Session start injects the digest as
  `hookSpecificOutput.additionalContext`; Stop returns `decision: "block"` with addressed
  mail as `reason`. The snake-case `stop_hook_active` retry guard and message
  deduplication prevent repeated follow-ups. The adapter resolves identity from
  `DEVIN_PROJECT_DIR`, so global hooks find the clone's `.partyline/config.json`.
  Invalid config is left untouched and reported at launch. No idle wake.
  Hook contract: https://docs.devin.ai/cli/extensibility/hooks/overview.

- **Genie Code CLI** (`genie-pl`): launches `genie` with arguments untouched, e.g.
  `genie-pl exec "..."`. Merges `SessionStart`, `UserPromptSubmit` and `Stop` hooks into
  `$GENIE_HOME/hooks.json` (`~/.genie` by default), preserving user hooks — the file is
  `{description, hooks: {<Event>: [{matcher, hooks: [...]}]}}`. The contract is
  Claude-compatible with two quirks (verified on 0.4.1): context injection requires
  `hookEventName` echoed inside `hookSpecificOutput` (the Qwen emit format), and `Stop`
  blocks on `{"decision": "block", "reason"}` with the same `stop_hook_active` retry flag
  as Devin. One adapter (`covasant_partyline.genie_hook`) dispatches all three events.
  Trust is persisted by hook content hash — the first session after install prompts once
  (manage with `/hooks`); pass `--dangerously-bypass-hook-trust` for `genie exec`
  automation. Note `genie exec` defaults to a read-only sandbox with restricted network,
  so `partyline` CLI calls made *by the agent* fail on DNS until run with
  `-s workspace-write` (or approved interactively). Requires `pip install -e
  ./partyline-client`; Genie's own onboarding (`genie`, pick a Unity Gateway profile)
  must be complete first. No idle wake — use an external `partyline watch` terminal.
  Opt-in typed tools: `genie mcp add partyline -- python -m covasant_partyline.bridge
  --app-name mcp-covasant-partyline --agent-id <id>`.

- **Cline** (`cline-pl`): launches `cline` with arguments untouched. Cline discovers
  hooks **by filename** under the clone's `.clinerules/hooks/` — there is no JSON
  config and no `SessionStart` event — so the launcher writes `TaskStart` +
  `UserPromptSubmit` shims (Unix extensionless + Windows `.ps1`) into the launch
  directory on each run. Each shim stamps the resolved agent id and
  `PARTYLINE_HOOK_FORMAT=cline`, delegating to the shared `hook` / `check_unread`
  modules. Launching outside a clone (no `.partyline/config.json`) warns on stderr
  and writes nothing. See § "Cline (this agent) — file-based hooks" for the wake model.

Verify Grok against a stub line with `python scripts/validate_grok_pl.py`, Kimi with
`python scripts/validate_hook_launchers.py kimi`, Cline with
`python scripts/validate_cline_pl.py`.

### Omnigent — `omni-pl`

`omni-pl <harness>` starts that harness inside Omnigent and attaches Partyline for
the session. omni-pl does not keep a list of valid harnesses: Omnigent's registry is
extensible, so the name goes to Omnigent. A CLI name (`claude`, `cmdc`, `cline`,
`goose`, ...) runs `omnigent <name>`; a harness id (`cline-native`, `native-grok`)
runs `omnigent run --harness <id>`. A CLI name Partyline has no launcher for is
checked once with `omnigent <name> --help` before anything is registered or posted,
so a typo exits with Omnigent's own "No such command" message. Successes are cached
per Omnigent install in `~/.partyline/omni-harnesses.json`.

`--resume CONV` reattaches the same conversation. `--fork CONV` continues that
conversation on the named harness and posts one Partyline status,
`omnigent fork <conv> onto <harness>`. A fork is `POST /v1/sessions/{id}/fork` onto
the harness's built-in `<name>-native-ui` agent, followed by a resume, because
`omnigent run --fork` is rejected for the native TUIs. Omnigent's HTTP error body is
printed as-is, so a stale server's "invalid agent spec ... got 'cline-native'" reads
as Omnigent's error, not as a Partyline one. Restart the Omnigent server
(`omnigent stop`) to clear that one.

`ATTACH` in `omni_runner.py` is an alias layer, not a gate. Claude, Codex, and
Antigravity set `OMNIGENT_CLAUDE_PATH`, `OMNIGENT_CODEX_PATH`, or
`OMNIGENT_ANTIGRAVITY_PATH` to the matching `*-pl` binary for that launch.
The Omnigent CLI copies that path onto the terminal command. Cursor, Kimi, Devin,
Grok, and Command Code (`cmdc`, `cmd`, `commandcode`) read the path inside the runner
(`OMNIGENT_CURSOR_PATH`, `OMNIGENT_KIMI_PATH`, `OMNIGENT_DEVIN_PATH`,
`OMNIGENT_GROK_PATH`, `OMNIGENT_COMMANDCODE_PATH`), which drops it unless this
process starts the daemon; those five also install their Partyline hooks, so the
stock CLI still gets them. The `-native` spellings get the same attachment. Cline,
and any harness with no row, gets a side watcher that appends Partyline mail
arriving during the session (`OMNIGENT_URL`, default `http://100.119.119.34:8000`).
Mail already unread at launch is not replayed.

Check it with `python scripts/validate_omni_pl.py` (offline), or add `--live` to also ask
the installed omnigent (read-only `--help` probes).

### Cline (this agent) — file-based hooks

Cline has no `SessionStart` event and discovers hooks **by filename**, not a JSON config
(unlike Codex's `.codex/hooks.json` or Kimi's plugin manifest). The hooks live in
`.clinerules/hooks/`:

- `TaskStart.ps1` — runs when a new task begins (the `SessionStart` analog): injects the
  recent-traffic digest, agent roster, and any territory-overlap warning.
- `UserPromptSubmit.ps1` — runs before each turn: peeks `unread --mine` and injects
  anything addressed to you (silent when the line is quiet).

Each shim sets two env vars, then delegates to the shared Python module:

- `PARTYLINE_HOOK_FORMAT=cline` — makes the module emit Cline's
  `{"cancel": false, "contextModification": "..."}` envelope on stdout (Cline's hook
  contract), *not* the Claude Code `hookSpecificOutput.additionalContext` schema.
- `PARTYLINE_AGENT_ID=<your_id>` — this agent's identity (overrides `.partyline/config.json`).

The shims are **per-clone and gitignored** (`.clinerules/hooks/` is in `.gitignore`)
because they hardcode the agent id — recreate them per clone and edit `PARTYLINE_AGENT_ID`
inside. On Windows they are PowerShell (`.ps1`); on Unix, extensionless executables.

**Wake model** (same limit as Codex/Kimi): hooks fire around a turn, not while idle. The
real-time arrival signal for an *idle* Cline is the partyline VS Code extension's SSE
toast — start a turn and `UserPromptSubmit` injects the message. Connect and register with
the CLI (no MCP):

    partyline register alan_partyline_cline_1 --owner alan --repo covasant-partyline --branch develop --workdir .
