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

Multiple agents work these repos in parallel. Coordinate through the `partyline` MCP
server. **Partyline is opt-in, not always-on** — it is deliberately *not* in the routine
`.mcp.json`, because its tool schemas + `INSTRUCTIONS` are re-sent every turn and would tax
every unrelated solo session. Load it only for a genuine **epic-scale, multi-agent
coordinated effort**:

```bash
claude --mcp-config partyline.mcp.json      # add --strict-mcp-config for ONLY these servers
```

Requires `pip install -e ./partyline-client` (editable — a stale bridge hangs the MCP
connection) and a valid `dev-eastus2` Databricks profile. Edit the `--agent-id` /
`PARTYLINE_AGENT_ID` in `partyline.mcp.json` per agent instance.

- **Session start**: `register_agent` with agent_id `<owner>_<repo>_<instance>`
  (e.g. `alan_partyline_1`), owner, repo, branch. Then `get_unread(only_for_me=true)`
  and `ack_checkpoint` after processing.
- **Before editing shared surfaces** (app/db.py schema, resources/*.yml, deploy.sh,
  the retention job): `claim_resource(repo, resource_path, ttl_seconds=900)`. If
  refused, another agent holds it — post a `question` to the holder (as `recipient`)
  instead of editing. `release_resource` when done.
- **Broadcast as you work**: `status` when starting significant work, `done` on
  completion, `blocked` (with detail) when stuck. Address agents via `recipient` or
  `mentions` (`@name` in dashboard-composed messages becomes a mention).
- Everything is broadcast — targeting is filtering, not privacy. Identity on the line
  is your self-declared `agent_id`; keep it consistent for the whole session.
- **Wake-up (instead of polling)**: in Claude Code, rely on the `partyline-channel` push
  (loaded by `partyline.mcp.json`). It is a persistent MCP server that pushes messages
  addressed to you into the session in real time as `<channel source="partyline" ...>`
  events; reply with the `reply` tool. **Do not** use the CLI `watch` background task to
  wake yourself here — Claude Code reaps background tasks at each turn boundary, so it dies
  within the turn and never catches a mention. The CLI watcher below is only for a *human*
  terminal or a non-Claude-Code agent:

  ```bash
  TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
  PARTYLINE_URL=<app-url> PARTYLINE_TOKEN=$TOKEN \
    python -m covasant_partyline.cli watch --exit-on-mention <your_agent_id> --timeout 3600
  ```

  A `SessionStart` hook (see `.claude/settings.json`) also injects recent line
  traffic at session start, so you begin every session already oriented.
