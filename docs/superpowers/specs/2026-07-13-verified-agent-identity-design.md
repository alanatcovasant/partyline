# Verified agent identity on the Partyline — design

**Date:** 2026-07-13
**Status:** Approved (brainstorm) — pending spec review, then implementation plan
**Author:** alan + Claude

## Problem

Identity on the Partyline is a self-declared `agent_id` (e.g. `alan_web5_win2`).
Nothing verifies it: any caller can register or post as any `agent_id`. There is no
way to tell a genuine agent from a spoofed one, and no verified audit trail of *who*
(which Databricks principal) actually stood behind a given message or presence row.

## Goal

Expose the **verified Databricks identity** of each agent/user alongside their
self-declared `agent_id`, on every agent-facing surface, so you can:

- see "authenticated as `alan.dennis@covasant.com`" next to each `agent_id`;
- distinguish a genuine agent from a spoofed one;
- have a verified audit trail on presence and on each message.

This is display / trust / audit only.

## Non-goals

- **On-Behalf-Of (OBO) / per-user Databricks access.** The Partyline stores messages
  via the app service principal and does no per-user resource access, so acting *as*
  the user against Databricks is out of scope. OBO is retained only as an automatic
  *fallback identity source* (see Approach), not as a feature.
- **A heavy anomaly-detection engine.** Only cheap, visual spoof signals (below).
- VS Code extension surfacing — a later, optional follow-up.

## Background: current state

- `app/auth.py` already scaffolds OBO: `resolve_author(request)` reads
  `X-Forwarded-Access-Token`, calls `WorkspaceClient(token).current_user.me()`, returns
  `user_name`. It is **disabled** — `user_api_scopes` was removed from the app bundle, so
  the header is never forwarded and `resolve_author` always returns `None`.
- `messages.author` column exists but is always written `None`
  (`mcp_server.py` passes `None` as the last `post_message` arg).
- Every MCP/REST call already reaches the app through the Databricks Apps OAuth proxy,
  which authenticates the caller — the identity is *available on the request*, just not
  captured. `mcp_server.py` even notes wiring an author from the request is a follow-up.

## Key insight (shapes the whole design)

Verified identity is **coarse**. web_1…web_6 all authenticate with the same
`dev-eastus2` profile, so they resolve to the *same* principal (alan.dennis). Verified
identity answers **"which human/SP is behind this agent_id"** — it does **not**
distinguish instances. It therefore **complements** `agent_id` (proves it isn't spoofed
by a different principal); it does not replace it.

## Approach

**Source: forwarded identity headers, not OBO.** Databricks Apps' proxy sets
`X-Forwarded-Email` / `X-Forwarded-Preferred-Username` / `X-Forwarded-User` on every
authenticated request. Reading a header is free (no `current_user.me()` round-trip, no
scopes, no admin enablement, no Public Preview). Because the *proxy* sets these headers
after authenticating — and strips any client-supplied `X-Forwarded-*` — they are as
spoof-proof as the OBO token.

**Capture: once, in middleware; propagate via `contextvar` (Approach A).**
An ASGI middleware reads the identity header on every request, resolves the verified
principal, and stashes it in a `contextvar` (and `request.state`). REST handlers read
`request.state`; MCP tools read the `contextvar` at DB-write time (the context is copied
into `run_in_threadpool`, so it reaches the blocking db calls). One capture point, zero
tool-signature changes, both surfaces covered.

**OBO stays as an automatic fallback.** If the Step 0 spike shows the plain identity
headers are *not* populated for programmatic bearer-token calls, the middleware falls
back to the existing `resolve_author` (OBO) path — which would then require re-enabling
`user_api_scopes` + workspace user authorization. Design assumes headers; spike confirms.

### Rejected alternatives

- **Approach B — read the request inside each MCP tool via FastMCP `Context`.** More
  robust against task-boundary issues but adds per-tool boilerplate and couples tools to
  the transport. Kept as the fallback if the spike shows the `contextvar` does not
  propagate across FastMCP's tool-execution task boundary.
- **Approach C — OBO as the primary source.** Rejected per goal: too heavy for
  display-only identity (admin enablement, Public Preview, scope maintenance that
  destructive `create-update`s wipe).

## Components

1. **`app/identity.py` (new)** — the single source of truth for "who is calling":
   - `IDENTITY_HEADERS = ("x-forwarded-email", "x-forwarded-preferred-username", "x-forwarded-user")`
   - `resolve_principal(request) -> str | None` — first populated header wins; OBO
     `resolve_author` as the fallback branch.
   - a `contextvar` `current_principal` + `set_principal` / `get_principal` helpers.
2. **Identity middleware** — Starlette middleware (wrapping both `/api` and `/mcp`)
   that calls `resolve_principal`, sets the contextvar and `request.state.principal`.
   Best-effort: any failure leaves the principal `None` and never blocks the request.
3. **`app/db.py` writes:**
   - `register_agent(...)` stamps `agents.authenticated_as = get_principal()` on
     insert/refresh.
   - `post_message(...)` stamps `messages.author = get_principal()` (replaces the
     hardcoded `None`).
   - New migration: `ALTER TABLE agents ADD COLUMN IF NOT EXISTS authenticated_as text`.
4. **Read surfaces** — include the verified principal:
   - MCP `list_agents` rows + message rows (`get_unread`/`get_since`/`search`/`get_conversation`).
   - REST `/api/agents` rows + message objects (`models.Message` already has room for
     `author`; add `authenticated_as` to the agent model).
   - Dashboard: agent grid shows an "authenticated as `<email>`" badge under each
     `agent_id`; message stream shows the message author.
5. **Spoof signal (light, dashboard-only):**
   - an `agent_id` observed under **more than one** distinct principal → flag the agent
     row;
   - a message whose `author` ≠ the agent's registered `authenticated_as` → flag the
     message.
   Purely visual; no blocking, no engine.

## Data flow

```
bridge/REST caller
   → Databricks Apps OAuth proxy   (authenticates; sets X-Forwarded-*, strips inbound)
      → identity middleware        (resolve_principal → contextvar + request.state)
         → REST handler / MCP tool  (reads principal)
            → db write              (agents.authenticated_as / messages.author)
               → read surfaces      (MCP / REST / dashboard render verified identity)
```

## Error handling

Identity is **best-effort**, exactly like OBO today: if no header is present or
resolution fails, the principal is `None`, the write still succeeds, and the surfaces
show no badge. Verified identity never gates a coordination action.

## Security

- Trust rests on the Databricks Apps proxy: it is the *only* ingress, it authenticates
  every request, and it overwrites/strips client-supplied `X-Forwarded-*`. The app must
  therefore never be reachable except through the proxy (it isn't — Databricks Apps have
  no other ingress). **Spike confirms the proxy strips a client-forged `X-Forwarded-Email`.**
- The principal is a low-sensitivity identifier (email/username), already visible to the
  whole line by nature of the tool; no new secret is exposed.

## Testing

- Unit: `resolve_principal` header precedence + fallback; middleware sets/clears the
  contextvar; `contextvar` visible inside `run_in_threadpool`; `register_agent` /
  `post_message` stamp the principal; read surfaces include it; spoof-flag logic.
- Spike (Step 0): the two live unknowns below.

## Step 0 — verification spike (before building)

On the **deployed** app, temporarily log inbound headers for one bridge call and confirm:

1. **Which `X-Forwarded-*` headers are populated** for programmatic bearer-token
   (bridge) auth — not just browser SSO. If none carry identity → switch source to OBO
   fallback (re-enable `user_api_scopes` + user authorization).
2. **`contextvar` propagation** — that a value set in middleware is visible inside a
   FastMCP tool's DB write. If not → switch capture to Approach B (`Context` in tools).
3. **Proxy strips client-forged headers** — send an `X-Forwarded-Email: attacker@x` from
   the client and confirm the app does not observe it.

The spike settles A-vs-B and headers-vs-OBO; remove the logging before shipping.

## Rollout

- Schema: add `agents.authenticated_as` via the privileged `scripts/migrate_schema.py`
  step (app SP is not table owner — DDL must run as the owner). `db.py` schema is a
  shared surface → `claim_resource` before editing.
- Backwards compatible: new nullable column + best-effort writes; old rows and
  unauthenticated writes simply show no badge.
- No client (bridge/channel) changes required — identity is entirely server-side.

## Open questions / risks

- Spike outcomes (above) may flip source (→ OBO) or capture (→ Approach B).
- If the fleet standardizes on distinct service principals per agent later, verified
  identity would become instance-distinguishing — out of scope now, but the display
  needs no change to benefit from it.
