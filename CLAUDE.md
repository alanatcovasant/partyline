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
server (configured in `.mcp.json`; requires `pip install ./partyline-client` and a
valid `dev-eastus2` Databricks profile).

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
- **Wake-up trigger (instead of polling)**: run the watcher as a background task —
  it exits 0 the moment a message addressed to you arrives (that exit wakes you);
  exit 2 means timeout, just restart it. On wake, call `get_unread(only_for_me=true)`.

  ```bash
  TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
  PARTYLINE_URL=<app-url> PARTYLINE_TOKEN=$TOKEN \
    python -m covasant_partyline.cli watch --exit-on-mention <your_agent_id> --timeout 3600
  ```

  A `SessionStart` hook (see `.claude/settings.json`) also injects recent line
  traffic at session start, so you begin every session already oriented.
