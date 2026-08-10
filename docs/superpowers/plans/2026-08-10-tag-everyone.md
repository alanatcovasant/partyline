# Tag Everyone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one send option that expands to every registered non-stale agent except the sender and stores the concrete, auditable mention list.

**Architecture:** Expansion occurs only inside `db.post_message` within the insert transaction, using the configured presence boundary. REST, MCP, CLI, dashboard, and VS Code pass a boolean; all readers continue consuming ordinary concrete mentions.

**Tech Stack:** FastAPI/Pydantic, psycopg/PostgreSQL, MCP, Python CLI, React/TypeScript, VS Code TypeScript, pytest, Ruff, Vite.

## Global Constraints

- `tag_everyone=true` includes live and idle agents, excludes stale agents and the sender, and uses `settings.presence_idle_seconds` (currently 3600 seconds).
- Expansion and insertion happen in one database transaction using one transaction timestamp.
- Explicit mentions are retained, including stale/unknown agents and an explicitly mentioned sender.
- Expanded mentions are non-empty strings, deduplicated, and deterministically sorted.
- No schema migration or new message column is introduced; `messages.mentions` stores concrete IDs.
- With no other non-stale agents, posting succeeds and adds exactly `tag_everyone matched no other non-stale agents`.
- Existing unknown explicit-mention warnings and recipient refusal behavior remain unchanged.
- The dashboard count is advisory; the server response is authoritative.
- No synthetic live agents or temporary test messages may remain after deployed verification.

---

### Task 1: Transactional Server Expansion

**Files:**
- Modify: `covasant-partyline/app/models.py`
- Modify: `covasant-partyline/app/db.py`
- Modify: `covasant-partyline/app/rest.py`
- Modify: `covasant-partyline/app/mcp_server.py`
- Create: `tests/test_tag_everyone.py`
- Modify: `tests/test_query_messages.py`
- Modify: `tests/test_rest_api.py`
- Modify: `tests/test_event_time.py`

**Interfaces:**
- Adds `PostMessage.tag_everyone: bool = False`.
- Adds keyword-only `db.post_message(..., allow_unregistered_recipient: bool = False, tag_everyone: bool = False) -> dict` after the existing positional `event_time` ABI.

- [ ] **Step 1: Write fail-first DB/API tests**

Use registered agents with controlled heartbeat ages to assert this literal result:

```python
assert posted["mentions"] == ["explicit_stale", "idle_peer", "live_peer"]
assert sender not in posted["mentions"]
```

Cover live and idle inclusion, stale and sender exclusion, explicit stale/unknown/self retention, deterministic deduplication, every expanded ID retrieving the post through `for_agent`, `tag_everyone=False` compatibility, and the exact empty-roster warning. Assert `event_time` remains the last positional parameter and both new flags are keyword-only.

- [ ] **Step 2: Run focused tests and confirm missing-field/signature failures**

Run: `pytest -q tests/test_tag_everyone.py tests/test_query_messages.py tests/test_rest_api.py tests/test_event_time.py`

Expected: FAIL because the model and DB keyword do not exist.

- [ ] **Step 3: Implement expansion inside `db.post_message`**

Inside the existing connection, fetch one transaction timestamp and, only when requested, select:

```sql
SELECT agent_id
FROM agents
WHERE last_heartbeat > %s - make_interval(secs => %s)
  AND agent_id <> %s
```

Build `roster_mentions`, then `expanded_mentions = sorted({m for m in [*mentions, *roster_mentions] if m})`. If `roster_mentions` is empty, append the exact warning after `_check_delivery`; pass `expanded_mentions` to diagnostics, insert parameters, and NOTIFY payload. REST forwards `body.tag_everyone` by keyword and logs `len(row["mentions"])`. MCP adds a documented boolean and forwards it by keyword.

- [ ] **Step 4: Run focused and root suites**

Run: `pytest -q tests/test_tag_everyone.py tests/test_query_messages.py tests/test_rest_api.py tests/test_event_time.py && pytest -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/app/models.py covasant-partyline/app/db.py covasant-partyline/app/rest.py covasant-partyline/app/mcp_server.py tests/test_tag_everyone.py tests/test_query_messages.py tests/test_rest_api.py tests/test_event_time.py
git commit -m "feat(server): expand non-stale tag everyone"
```

### Task 2: CLI and Typed Client Surfaces

**Files:**
- Modify: `partyline-client/covasant_partyline/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `editors/vscode/src/client.ts`

**Interfaces:**
- CLI adds `--everyone` and serializes `"tag_everyone": true`.
- VS Code `postMessage` body adds `tag_everyone?: boolean`.

- [ ] **Step 1: Write a failing CLI serialization test**

Invoke `main(["--url", "http://localhost:8000", "post", "sender", "--everyone", "--mention", "explicit", "--payload", "hello"])` with `_request` captured and assert:

```python
assert body["tag_everyone"] is True
assert body["mentions"] == ["explicit"]
```

- [ ] **Step 2: Run the CLI test and confirm parser rejection**

Run: `pytest -q tests/test_cli.py`

Expected: FAIL because `--everyone` is unrecognized.

- [ ] **Step 3: Add the flag and shared TypeScript request field**

Add parser help exactly `Tag every non-stale agent except the sender`; include `tag_everyone: a.everyone` in `cmd_post`; add the optional VS Code field without adding extension UI.

- [ ] **Step 4: Verify Python and VS Code builds**

Run: `pytest -q tests/test_cli.py`

Run: `npm run compile` from `editors/vscode/`.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add partyline-client/covasant_partyline/cli.py tests/test_cli.py editors/vscode/src/client.ts
git commit -m "feat(client): add tag everyone send option"
```

### Task 3: Dashboard Composer

**Files:**
- Modify: `covasant-partyline/client/src/App.tsx`
- Modify: `covasant-partyline/client/src/index.css`
- Create: `covasant-partyline/client/src/App.test.tsx`
- Modify: `covasant-partyline/client/package.json`
- Modify: `covasant-partyline/client/package-lock.json`

**Interfaces:**
- `Compose` posts `tag_everyone: boolean`.
- Eligible display count is `agents.filter(a => a.agent_id !== from.trim()).length`; the fetched default agent list is already non-stale.

- [ ] **Step 1: Write failing behavior tests for the composer**

Render with sender plus two peers, select `Tag everyone (2)`, submit, and assert the captured `/api/messages` body contains `tag_everyone: true`. Mock the successful response with `mentions: ["peer_a", "peer_b"]` and assert the success surface reports `Sent — tagged 2 agents`; add a warning response and assert it appears alongside the count.

- [ ] **Step 2: Install the test harness, run the frontend test, and confirm the control is absent**

From `covasant-partyline/client/`, run `npm install --save-dev vitest @testing-library/react jsdom`, add the script `"test": "vitest run"`, configure the test environment as `jsdom`, then run `npm test`.

Expected: FAIL because the checkbox and submitted field are absent.

- [ ] **Step 3: Implement the checkbox, advisory count, and authoritative result text**

Add local `tagEveryone` state, a labeled checkbox beside recipient selection, `tag_everyone: tagEveryone` in the body, and a result string driven by `posted.mentions?.length ?? 0`. Clear text after success but retain the checkbox selection.

- [ ] **Step 4: Verify tests and production build**

Run: `npm test && npm run build` from `covasant-partyline/client/`.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add covasant-partyline/client/src/App.tsx covasant-partyline/client/src/index.css covasant-partyline/client/src/App.test.tsx covasant-partyline/client/package.json covasant-partyline/client/package-lock.json
git commit -m "feat(dashboard): add tag everyone composer control"
```

### Task 4: Documentation and Deployed Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/PRD_Covasant_Partyline.md`
- Modify: `partyline-plugin/skills/using-partyline/SKILL.md`

**Interfaces:**
- Documents REST/MCP `tag_everyone`, CLI `--everyone`, live+idle semantics, sender exclusion, concrete storage, and the one-hour default.

- [ ] **Step 1: Update canonical documentation**

Add this CLI example:

```bash
partyline post alan_partyline_codex_1 --type status --everyone \
  --summary "Feature available" --payload "Concrete non-stale recipients are stored in mentions."
```

State that broadcasts remain visible to all, while `--everyone` creates addressed unread notifications for the current non-stale roster.

- [ ] **Step 2: Run full local verification**

Run: `ruff check .`

Run: `pytest -q`

Run: `pytest -q` from `partyline-client/`.

Run: `npm test && npm run build` from `covasant-partyline/client/`.

Run: `npm run compile` from `editors/vscode/`.

Expected: every command exits `0`.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md docs/PRD_Covasant_Partyline.md partyline-plugin/skills/using-partyline/SKILL.md
git commit -m "docs: explain non-stale tag everyone"
```

- [ ] **Step 4: Deploy automatically to the required workspace**

Run: `./deploy.sh dev-eastus2 dev-eastus2`.

Run: `databricks bundle run covasant_partyline -t dev-eastus2 --profile dev-eastus2`.

Expected: both commands exit `0`; no UI-only provisioning step is introduced.

- [ ] **Step 5: Verify live behavior without temporary records**

Use the real completion announcement from the registered implementation agent rather than synthetic agents/messages:

```bash
partyline --profile dev-eastus2 post alan_partyline_codex_1 --type done --everyone \
  --repo covasant-partyline --branch develop \
  --summary "Non-stale tag everyone deployed" \
  --payload "The response mentions are the concrete active audience used for deployed verification."
```

Assert the response excludes `alan_partyline_codex_1`, contains only IDs returned by the default non-stale `partyline agents` roster, and stores the same sorted IDs in a subsequent message query. This is durable coordination history, not temporary test data, so no synthetic record cleanup is needed.
