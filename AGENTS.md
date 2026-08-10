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

Multiple agents work these repos in parallel. There are two ways to reach the line;
**prefer the CLI + hooks path** and load MCP only when you specifically want typed tools.

- **CLI + hooks (default, near-zero standing cost).** Actions go through the `partyline`
  CLI — `partyline.cmd` / `./partyline.sh`, or `python -m covasant_partyline.cli` — which
  hits the *same* REST API as the MCP tools but keeps nothing resident in context between
  calls. Awareness comes from the lifecycle hooks in `.Codex/settings.json`: a
  `SessionStart` hook injects recent line traffic, and a `UserPromptSubmit` hook
  (`partyline-plugin/hooks/check_unread.py`) peeks for anything addressed to you before
  each turn (silent when the line is quiet). This covers most coordinated work.
- **MCP server (opt-in, for typed tools).** `Partyline is opt-in, not always-on` — it is
  deliberately *not* in the routine `.mcp.json`, because its tool schemas + `INSTRUCTIONS`
  are re-sent every turn: a per-turn token tax on every unrelated solo session (Anthropic
  measures comparable servers at ~700–2,000 tokens/tool — see `docs/Covasant Partyline Whitepaper.md`
  §3.8). Load it only for a genuine **epic-scale, multi-agent coordinated effort** where
  typed, discoverable tools earn their keep:

  ```bash
  Codex --mcp-config partyline.mcp.json    # add --strict-mcp-config for ONLY these servers
  ```

  Requires `pip install -e ./partyline-client` (editable — a stale bridge hangs the MCP
  connection) and a valid `dev-eastus2` Databricks profile. Edit the `--agent-id` /
  `PARTYLINE_AGENT_ID` in `partyline.mcp.json` per agent instance.

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
- **Staying aware (no real-time idle wake in Codex right now).** The
  `partyline-channel` push was the only mechanism that could interrupt an *idle* session,
  and it is currently **blocked by org policy**: `--dangerously-load-development-channels`
  is refused until an administrator sets `channelsEnabled: true` in managed settings, so
  inbound channel messages are silently dropped. The CLI `watch` background task can't
  substitute here either — Codex reaps background tasks at each turn boundary, so it
  dies within the turn and never catches a mention. Until the channel is unblocked, rely
  on the hooks plus on-demand checks:
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
