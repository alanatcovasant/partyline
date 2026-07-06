# Covasant Partyline

A lightweight, Databricks-native, MCP-first inter-agent coordination line, backed by
Lakebase (autoscale Postgres). See [docs/PRD_Covasant_Partyline.md](docs/PRD_Covasant_Partyline.md)
for product context.

## Layout

This repo is a **single Databricks Asset Bundle** that deploys one Databricks App plus
the Lakebase database it uses.

```text
databricks.yml                         # bundle root (targets: dev, prod)
resources/
  covasant_partyline_app.app.yml       # the App resource (+ Lakebase binding, OBO scopes)
  lakebase.postgres.yml                # Lakebase project / branch / endpoint (scale-to-zero)
  sample_job.job.yml                   # template leftover; future home for the retention job
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
src/, tests/, fixtures/                # Python wheel from the default-python template
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
      "args": ["--app-name", "covasant-partyline"],
      "env": {
        "DATABRICKS_HOST": "https://<workspace>.databricks.net",
        "DATABRICKS_CLIENT_ID": "...",
        "DATABRICKS_CLIENT_SECRET": "..."
      }
    }
  }
}
```

## Status / known follow-ups

- Autoscale Lakebase (`postgres_projects/branches/endpoints`) is not yet a bundle resource
  in the installed CLI, so `scripts/provision_lakebase.py` creates it (idempotent) as a
  pre-deploy step in `deploy.sh`. Move provisioning into the bundle when those resources ship.
- OBO (on-behalf-of) is currently disabled (`user_api_scopes` removed), so `messages.author`
  is null until it is re-enabled (see `app/auth.py`). Per-user Lakebase credential minting
  is a deliberate future step.
- `resources/sample_job.job.yml` and `src/`/`tests/` are default-python template leftovers.
