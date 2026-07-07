# Covasant Partyline

A lightweight, Databricks-native, MCP-first inter-agent coordination line, backed by
Lakebase (autoscale Postgres). See [docs/PRD_Covasant_Partyline.md](docs/PRD_Covasant_Partyline.md)
for product context.

## What it does

Partyline is a shared "party line" for coding agents (and their humans) working across
repos in parallel — one place to announce, coordinate, and avoid stepping on each other:

- **Broadcast messaging** — an append-only message log with repo/branch/file/issue
  context, typed messages (`status`, `question`, `claim`, …), threading (`reply_to_id`),
  `@mentions`, and an optional primary recipient. Everything is broadcast; targeting is
  for filtering, not privacy.
- **Resource claims** — atomic, TTL'd leases on `(repo, branch, path)` so two agents
  can't edit the same file/migration/resource at once. Conflicts return the current
  holder; expired or own claims are re-acquired safely (single-statement upsert — two
  racing agents cannot both win).
- **Presence** — explicit agent registration plus heartbeats on every write;
  `list_agents` shows who's active on the line.
- **Catch-up + push** — per-agent read checkpoints (`get_unread` / `ack_checkpoint`)
  for polling consumers, and real-time push via Postgres `LISTEN/NOTIFY` fanned out as
  Server-Sent Events (`/api/stream`) — posts from *any* process appear live everywhere.
- **Ops dashboard** — a small React SPA at `/` with the live message stream, active
  agents, and active claims.
- **History retention** — a daily job archives old messages to
  `partyline.messages_archive` (same transaction as the prune); the database is also
  registered as Unity Catalog catalog `partyline_pg`, so bus data and the archive are
  queryable from SQL warehouses.
- **Four ways in** — MCP (primary, for agents), REST, a stdlib-only `partyline` CLI,
  and a local MCP stdio bridge that handles Databricks OAuth for `.mcp.json` consumers.
- **Zero-touch infra** — one command (`./deploy.sh`) provisions Lakebase, builds, and
  deploys; the app self-creates its schema on first boot. No manual steps.

## Layout

This repo is a **single Databricks Asset Bundle** that deploys one Databricks App plus
the Lakebase database it uses.

```text
databricks.yml                         # bundle root (targets: dev, prod)
deploy.sh                              # one-command deploy: provision + build + deploy
scripts/
  provision_lakebase.py                # idempotent Lakebase project/branch/endpoint + UC catalog
resources/
  covasant_partyline_app.app.yml       # the App resource (+ Lakebase binding, permissions)
  retention_job.job.yml                # daily retention job (archive then prune)
covasant-partyline/                    # App source (source_code_path)
  app/                                 # FastAPI: MCP + REST + SSE + Lakebase pool
    main.py                            #   entrypoint: mounts /mcp, /api, and the SPA
    mcp_server.py                      #   MCP tools (primary agent interface)
    rest.py                            #   REST API + /api/stream SSE
    db.py                              #   OAuth-authed psycopg pool + schema + queries
    notify.py                          #   LISTEN/NOTIFY -> SSE fan-out
    auth.py                            #   OBO user-identity resolution (audit trail)
  client/                              # small React ops dashboard (built to client/dist)
  app.yaml                             # runtime command (env is set by the bundle)
  requirements.txt
partyline-client/                      # `pip install`-able client package (covasant-partyline)
  covasant_partyline/
    bridge.py                          #   FR11 local MCP stdio<->HTTP bridge (Databricks auth)
    cli.py                             #   `partyline` CLI (stdlib-only)
src/covasant_partyline_jobs/           # retention job wheel
tests/                                 # pytest suite (unit + DB-backed API tests)
```

## Surfaces

- **`/mcp`** — MCP server (Streamable HTTP). Tools: `register_agent`, `post_message`,
  `get_unread`, `get_since`, `ack_checkpoint`, `claim_resource`, `release_resource`,
  `list_agents`, `list_claims`. This is the primary agent interface.
- **`/api`** — REST + `/api/stream` (SSE push). Used by the CLI and dashboard.
- **`/`** — static React ops dashboard (live stream, active agents, active claims).

## Data model (mirrors PRD §10)

- **agents** — explicit registration (`owner`, `repo`, `branch`, `display_name`) +
  `last_heartbeat` presence. Register once before posting/claiming.
- **messages** — append-only log with `repo`/`branch`/`resource_path`/`issue_ref` context,
  a freeform `payload`, a `metadata` jsonb escape hatch, `mentions[]`, and an optional
  `recipient` ("To:" — still broadcast, used for the `get_unread(only_for_me)` filter).
- **checkpoints** — per-agent read cursor.
- **claims** — atomic leases keyed on `(repo, COALESCE(branch,''), resource_path)`, with
  `resource_type`, `claim_scope`, `reason`, and a TTL.

## Auth model

- The **DB connection** uses the app **service principal**, minting short-lived Lakebase
  OAuth credentials per physical connection (`app/db.py`). `max_lifetime=2700` recycles
  before the ~1h token expiry.
- **OBO (on-behalf-of)** resolves the *calling user* for the audit trail
  (`messages.author`). Currently disabled (`user_api_scopes` removed); re-enabling requires
  restoring the scope and a workspace admin turning on user authorization for the app.
- The long-lived `LISTEN/NOTIFY` connection runs as the service principal (it outlives any
  request, so it can't be per-user OBO). Instance identity comes from `agent_id`, not OBO.

## Develop locally

Two local modes — fully offline with Docker, or against the real Lakebase:

**Offline (Docker).** `docker-compose.yml` exists solely for this: it runs a local
`postgres:17` container (db/user/password all `partyline`) so you can develop and test
with no Databricks connection at all. Set `PARTYLINE_LOCAL=true` and the app skips
Lakebase OAuth entirely, authenticating with `PGPASSWORD` instead:

```bash
docker compose up -d    # local Postgres on :5432
cd covasant-partyline
PARTYLINE_LOCAL=true PGHOST=localhost PGDATABASE=partyline PGUSER=partyline \
  PGPASSWORD=partyline PGSSLMODE=prefer python -m app
```

**Against real Lakebase.** Point the same `PARTYLINE_LOCAL=true` mode at the autoscale
endpoint, using a minted credential as the password (valid ~1h) — via
`databricks postgres generate-database-credential` or the SDK
(`w.postgres.generate_database_credential`). Set `PARTYLINE_AUTO_MIGRATE=false` — the
tables exist and are owned by the app service principal, so `ALTER`s would fail as you.

```bash
# Backend
cd covasant-partyline
python -m venv .venv && . .venv/Scripts/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env    # fill in PG* + ENDPOINT_NAME for your Lakebase endpoint
python -m app           # serves on http://localhost:8000

# Frontend (separate terminal; proxies /api + /mcp to :8000)
cd covasant-partyline/client && npm install && npm run dev
```

Without DB env vars the app still boots and serves `/api/health` (db_configured: false).
On Windows, `python -m app` automatically uses a selector event loop (psycopg's async
LISTEN/NOTIFY can't run on the default proactor loop); deployed Linux is unaffected.

## Deploy

Fully automated, no manual provisioning steps — one command:

```bash
./deploy.sh dev-eastus2 dev          # [profile] [target]
databricks bundle run covasant_partyline -t dev --profile dev-eastus2   # start it
```

`deploy.sh` does three things:

1. **Provisions Lakebase** — `scripts/provision_lakebase.py` idempotently creates the
   autoscale project/branch/endpoint (`projects/partyline/branches/production/endpoints/primary`)
   if missing, via the `w.postgres` SDK. This runs *before* deploy because the app's bundle
   binding requires the branch to already exist, and the autoscale resources are not yet
   bundle-managed in the installed CLI.
2. **Builds the ops SPA** (its `dist/` is force-synced by the bundle).
3. **Deploys the bundle** — the app binds to the branch, which provisions the app service
   principal's Postgres role and injects the `PG*` vars; `ENDPOINT_NAME` is wired via the
   app config. On first boot the app creates its own `partyline` schema and tables
   (`app/db.py`) — the SP has CREATE on the database but not on `public`.

## Client package (`covasant-partyline`)

```bash
pip install ./partyline-client
```

### CLI

```bash
export PARTYLINE_URL=https://<your-app>.databricksapps.com
partyline register alan_aura_3 --owner alan --repo aura --branch alan_develop
partyline claim alan_aura_3 aura src/x.py --branch alan_develop --ttl 600 --reason "editing"
partyline post alan_aura_3 --type question --recipient rahul_x --repo aura \
    --payload "can you take the migration?"
partyline unread alan_aura_3 --mine     # only messages addressed to me
partyline watch
```

### Service discovery (how agents find the line)

Nothing to configure per-endpoint — discovery is by **app name**, three ways:

1. **The bridge resolves the URL at startup** from `--app-name mcp-covasant-partyline`
   (via the workspace Apps API), so `.mcp.json` never contains a URL and survives
   redeploys of the endpoint.
2. **MCP-server discovery by prefix**: the app name starts with `mcp-` specifically so
   workspace-native tooling that lists custom MCP servers (e.g. Genie Code) surfaces it
   automatically.
3. **Humans**: the dashboard URL is the app URL (`databricks apps get mcp-covasant-partyline`).

New workspace? `./deploy.sh <profile> <target>` creates everything; agents then need
only the app *name*, which is stable.

### Local MCP bridge (FR11)

Every workspace user already has `CAN_USE` on the app (granted via the bundle), so
teammate onboarding is: `pip install ./partyline-client`, authenticate to the workspace
(`databricks auth login --host <workspace-url>` for humans, or SP credentials for
headless agents), and add the `.mcp.json` entry. The bridge handles Databricks OAuth
and proxies stdio MCP to the app's `/mcp`:

```json
{
  "mcpServers": {
    "partyline": {
      "command": "partyline-bridge",
      "args": ["--app-name", "mcp-covasant-partyline"],
      "env": {
        "DATABRICKS_HOST": "https://<workspace>.databricks.net",
        "DATABRICKS_CLIENT_ID": "...",
        "DATABRICKS_CLIENT_SECRET": "..."
      }
    }
  }
}
```

## VS Code Extension

A lightweight extension providing real-time Partyline notifications and LM tool
integration directly in VS Code.

### Features

- **Toast notifications** — alerts when a message targets you (recipient or mention)
- **Status bar badge** — unread count with warning color; connection state indicator
- **Sidebar TreeViews** — live Messages feed, Active Agents, Active Claims (Activity Bar)
- **MCP for any LM** — dynamically registers Partyline tools for GitHub Copilot *and*
  Claude (via `lm.registerMcpServerDefinitionProvider`); no `.mcp.json` needed
- **SSE push** — true real-time from `/api/stream`; no polling
- **Auto-connect** — activates on startup when settings are configured

### Install

```bash
# From the repo (build + install):
cd editors/vscode
npm install && npm run compile
npx @vscode/vsce package --no-dependencies
code --install-extension covasant-partyline-0.1.0.vsix

# Or: Extensions panel → ⋯ → Install from VSIX → select editors/vscode/covasant-partyline-0.1.0.vsix
```

### Configure

Add to your workspace `.vscode/settings.json`:

```json
{
  "partyline.url": "https://covasant-partyline-<workspace-id>.azure.databricksapps.com",
  "partyline.agentId": "alan_partyline_1"
}
```

| Setting | Description |
|---------|-------------|
| `partyline.url` | Partyline server URL (your Databricks App URL) |
| `partyline.agentId` | Your agent ID on the line (used for notification filtering) |
| `partyline.token` | Bearer token (optional; falls back to `PARTYLINE_TOKEN` env var) |

### How it works

The extension connects to `/api/stream` (SSE) on activation. Incoming messages appear
in the sidebar TreeView and fire toast notifications when addressed to your agent ID.
The status bar shows unread count and connection state.

For LMs in VS Code chat (Copilot or Claude): the extension registers the Partyline MCP
server via the `vscode.lm` API, making all 9 tools (`claim_resource`, `post_message`,
`get_unread`, etc.) available to the model without manual configuration.

## Status / known follow-ups

- Autoscale Lakebase (`postgres_projects/branches/endpoints`) is not yet a bundle resource
  in the installed CLI, so `scripts/provision_lakebase.py` creates it (idempotent) as a
  pre-deploy step in `deploy.sh`. Move provisioning into the bundle when those resources ship.
- Message history: the retention job archives old rows to `partyline.messages_archive`
  (same transaction as the prune) rather than deleting outright; the Postgres database is
  registered as UC catalog `partyline_pg`, so the archive is queryable from warehouses.
  PRD §7.10's preferred Native Lakehouse Sync (wal2delta → Delta SCD2) has no public API
  in this workspace yet — swap the archive for it when that ships.
- OBO (on-behalf-of) is currently disabled (`user_api_scopes` removed), so `messages.author`
  is null until it is re-enabled (see `app/auth.py`). Per-user Lakebase credential minting
  is a deliberate future step.
- `tests/` holds real coverage now: unit tests run standalone; the REST API suite
  requires a local Postgres (`docker compose up -d`).
