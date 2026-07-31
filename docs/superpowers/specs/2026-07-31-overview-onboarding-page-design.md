# Overview onboarding page + PowerShell install parity — design

**Date:** 2026-07-31
**Author:** Alan Dennis (with Claude)
**Status:** approved, pre-implementation

## Problem

The Partyline dashboard SPA has two tabs — **Dashboard** (live ops) and **Connect an
agent** (mechanical copy-paste setup). A developer who has never seen the line has
nowhere to learn *what Partyline is, why it exists, and how to think about coordinating
on it* before being handed install commands. This blocks a clean team rollout.

Two concrete gaps:

1. **No conceptual onboarding.** New developers land on either a live metrics dashboard
   or a wall of install commands — neither explains the mental model.
2. **PowerShell is a second-class citizen.** The "Connect an agent" page headlines the
   bash `install.sh` one-liner with a copy button, but relegates Windows/PowerShell to a
   single muted sentence with no copy-paste snippet
   (`client/src/App.tsx` `ConnectAgent`, the `.muted` line after the bash snippet). The
   team is mixed-OS; a Windows dev cannot copy-paste their way onto the line today, even
   though `install.ps1` is fully featured and already served by the app.

## How the CLI is distributed (context — no change needed)

The app is its own package registry; nothing is on PyPI and no repo clone is required:

- **Deploy time** (`deploy.sh`): stages into `client/dist/` the versioned client wheel
  `covasant_partyline-*.whl`, a manifest `partyline-client-latest.txt` (holds the real
  wheel filename — pip can't install a `*-latest.whl` wildcard), plus `install.sh`,
  `install.ps1`, and the VS Code `.vsix`.
- **Runtime** (`app/main.py`): `client/dist/` is mounted static at `/`, so all of those
  are served from the app URL.
- **Install** (`install.sh` / `install.ps1`): mint a Databricks token → read
  `/partyline-client-latest.txt` → download `/<wheel>` → `pipx install` (or
  `pip install --user`) → write `.partyline/config.json` + `.mcp.json`.

**Step 0 (the one thing the app cannot self-serve):** Databricks workspace access + the
`databricks` CLI authenticated to the profile. *Every* app route requires OAuth, so even
fetching the installer needs a bearer token — a bare `curl`/`iwr` returns 401. Both
installers are feature-equivalent; bash is **not** required. The installed CLI is
stdlib-only Python and runs identically on any OS.

## Approach

Two changes, both confined to the SPA (`client/src/App.tsx` + `client/src/index.css`).
No router exists — navigation is a single `view` state toggled by `.view-tabs` buttons —
so a new "page" is a `view` value + a nav button + a stateless component.

### Change 1 — new "Overview" page

A stateless `Overview()` component, styled with the existing `.connect*` / `.snippet`
classes plus a small `.overview` scope. Derives the app URL from
`window.location.origin` (like `ConnectAgent`). No data fetching, no new deps.

- **Nav:** extend the `view` union to `"overview" | "dashboard" | "connect"`; add a third
  `.view-tabs` button. **Overview is the first tab and the default landing view**, so new
  developers (the audience) land on the explainer; returning operators are one click from
  the Dashboard. (This is the one behavior change — called out explicitly.)
- **Content** (concise, conceptual — each a `.connect-step`):
  1. **What is Partyline?** — one-paragraph mental model: a durable, broadcast
     coordination line for a fleet of coding agents working related repos in parallel
     across terminals, machines, and vendors.
  2. **Core concepts** — a compact definition list: *broadcast* (targeting = filtering,
     not privacy), *agent_id* (identity + `--mine` filter), *message types*
     (status/question/claim/blocked/done/…), *claims* (TTL'd leases so two agents don't
     collide), *presence* (register + heartbeat), *catch-up + push* (unread/ack + live SSE).
  3. **How you get the CLI** — the app *is* the registry: everything you need is served
     from this URL, on **bash or PowerShell** (OS-neutral framing). Names **step 0**
     (Databricks access + authenticated `databricks` CLI, else a 401). Does **not**
     duplicate the install command — funnels to "Connect an agent".
  4. **The coordination loop** — register → check what's addressed to you → claim before
     touching shared surfaces → broadcast status/done/blocked → ack. Notes hooks surface
     messages for you (no tight polling).
  5. **Where to next** — a button that switches the in-app view to "Connect an agent",
     plus links to `/skills/using-partyline.md` and the REST/health endpoints.

### Change 2 — PowerShell parity on "Connect an agent"

In the existing `ConnectAgent` step 2, add a real `install.ps1` one-liner as a
copy-paste `<Snippet>` alongside the bash one, instead of the muted footnote. Mirror the
`install.ps1` header example:

```powershell
$t = (databricks auth token -p dev-eastus2 | ConvertFrom-Json).access_token
$env:AGENT_ID = "<owner>_<repo>_1"
iex (iwr -Headers @{ Authorization = "Bearer $t" } "<app-url>/install.ps1").Content
```

Present bash and PowerShell as equal siblings (e.g. "macOS/Linux (bash)" and "Windows
(PowerShell)"), each a labelled `<Snippet>`. Keep both snippets' agent-id placeholder and
URL derivation consistent with the current bash snippet.

## Out of scope

- No router / real URLs (keep the single-`view` pattern).
- No new REST/MCP surfaces — distribution already works end-to-end.
- No changes to `install.sh` / `install.ps1` / `deploy.sh` themselves.

## Testing & verification

Static presentational content with no logic — no unit tests warranted. Verification:

1. `npm run build` in `client/` — TypeScript typecheck + Vite build pass.
2. `./deploy.sh dev-eastus2 dev` — deploy to the eastus2 workspace.
3. Confirm against the **deployed** app URL (project ground rule — not just local): the
   Overview tab renders as the default view with all five sections, and the PowerShell
   snippet renders with a working copy button on "Connect an agent".
