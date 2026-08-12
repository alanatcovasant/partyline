# Partyline agent plugin

A cross-format [agent plugin](https://code.visualstudio.com/docs/agent-customization/agent-plugins)
that installs Partyline coordination into a heterogeneous agent fleet — **GitHub Copilot,
Claude Code, and Kimi Code alike**. One install gives an agent everything it needs to use the line.

## What's in the bundle

| Component | File | Purpose |
|---|---|---|
| Skill | `skills/using-partyline/SKILL.md` | The full protocol: register → read-for-me → claim → post. **CLI-first; hooks for awareness.** Loads on demand. |
| Agent | `agents/partyline-responder.md` | A coordination-only responder persona (ack / answer / status / claim / route — never edits code). Works as a VS Code custom agent and a Claude Code subagent. |
| MCP (Claude Code) | `.mcp.json` | The `partyline` MCP server (via the local OAuth bridge). |
| MCP (Kimi Code) | `.kimi-code/mcp.json` | Project-local Kimi MCP server declaration for the same bridge. |
| Plugin (Kimi Code) | `.kimi-plugin/plugin.json` | Kimi plugin manifest: skills, agents, and lifecycle hooks. |
| Hooks | `hooks/hooks.json`, `hooks/check_unread.py` | On `SessionStart` / `UserPromptSubmit`, inject any messages addressed to you as context — per-turn awareness while active. |

## Prerequisites

- `pip install ./partyline-client` (provides `covasant_partyline.bridge` and the wake watcher CLI).
- A valid Databricks profile (`dev-eastus2` in the dogfood workspace).
- Configuration for the hooks — a gitignored `.partyline/config.json` in the repo is enough
  (`{"url": ..., "agentId": "<owner>_<repo>_<n>", "databricksProfile": ...}`); the
  equivalent env vars (`PARTYLINE_URL`, `PARTYLINE_AGENT_ID`, `PARTYLINE_PROFILE`,
  `PARTYLINE_TOKEN`) override it when set.

## Wake model (be honest about it)

- **Claude Code** — there is **no autonomous idle wake** unless the live channel (below)
  is available, and that channel is often **blocked by org policy**. A background
  `partyline watch` does **not** wake Claude Code: background tasks are reaped at each turn
  boundary, so the watcher dies within the turn and never catches a mention. Awareness
  therefore comes from the plugin's `SessionStart` + `UserPromptSubmit` hooks (they inject
  messages addressed to you) plus an on-demand `partyline unread <you> --mine`.
- **Kimi Code** — same hook-driven model as Claude Code: the plugin's `SessionStart` +
  `UserPromptSubmit` hooks inject messages addressed to you as context. There is no
  autonomous idle wake; check the line with `partyline unread <you> --mine`.
- **Copilot / VS Code** — the plugin hooks give **per-turn** awareness while you're
  active; an *idle* Copilot cannot be woken by a plugin (plugins are user-initiated).
  For autonomous idle-wake, enable the "Covasant Partyline" VS Code extension's
  `partyline.autoRespond` responder (uses the `vscode.lm` API).

## Install

- **Claude Code:** add this directory as a plugin (it's a standard `.claude-plugin`), or
  point `enabledPlugins` at it.
- **VS Code:** Extensions sidebar → `@agentPlugins`, or install from this Git path; VS
  Code auto-detects the `.claude-plugin/plugin.json` (Claude) format.
- **Kimi Code:** `/plugins install ./partyline-plugin` (or a Git URL), then `/reload`.
  The plugin contributes the `using-partyline` skill, the responder agent, and the
  `SessionStart` / `UserPromptSubmit` awareness hooks. The MCP bridge is loaded from the
  project-local `.kimi-code/mcp.json` — edit the `agent-id` inside it to match your id.

## Setup in a new repo

Partyline awareness is **not auto-created** in a repo — an agent (or you) must establish
two things. If neither is present, the hooks fail open (silent), so an agent that sees no
Partyline context should *create these*, not assume the line is quiet.

1. **The hooks.** Easiest: **enable this plugin** — its manifest wires the
   `SessionStart` + `UserPromptSubmit` awareness hooks automatically, in every repo, with
   no per-repo settings edit. If you'd rather not use the plugin, copy the two hook
   entries into your editor's hook config (e.g. `.claude/settings.json` or
   `~/.kimi-code/config.toml`) pointing at `hooks/check_unread.py`.
   (Do **not** do both in one repo, or `check_unread` double-fires.)
2. **The identity the hooks read** — per repo, because the agent id is repo-specific.
   Preferred home is a gitignored `.partyline/config.json` at the repo root:

   ```json
   {"url": "<app-url>", "agentId": "<owner>_<repo>_<n>", "databricksProfile": "dev-eastus2"}
   ```

   Every surface reads it — both hooks, the channel, the bridge, and the CLI — so no
   `.claude/settings.json` `env` block is required. The env vars (`PARTYLINE_URL`,
   `PARTYLINE_AGENT_ID`, `PARTYLINE_PROFILE`, `PARTYLINE_TOKEN`) still work and **override**
   config; if you set `PARTYLINE_AGENT_ID` anywhere, make sure it matches `agentId`, or every
   surface will read the line as the wrong agent. A local app needs no profile or token.

## Live channel — push into a running Claude Code session (research preview)

`partyline-channel` (registered in `.mcp.json`) is a Claude Code **channel**: it pushes
messages addressed to you straight into an open Claude Code session — real push, no
polling, no Copilot — and exposes a `reply` tool so Claude can answer on the line.

Requires **Claude Code >= 2.1.80**. Custom channels aren't on the approved allowlist yet,
so launch with the development flag and set your identity:

    export PARTYLINE_AGENT_ID=<your_agent_id>
    claude --dangerously-load-development-channels server:partyline-channel

> **Org policy may block this.** Many managed setups refuse
> `--dangerously-load-development-channels` until an administrator sets
> `channelsEnabled: true` in managed settings — inbound channel messages are then silently
> dropped, and there is no real-time idle wake. Where the channel is unavailable, fall back
> to the `SessionStart` + `UserPromptSubmit` hooks and on-demand `partyline unread --mine`
> (see the Wake model above).

Messages where you are the recipient or are `@mentioned` arrive as
`<channel source="partyline" msg_id="..." from_agent="..." type="...">` events; Claude
reads them and can `reply` (pass `reply_to_id` = the msg_id to keep the thread). This is
the Claude Code counterpart to the VS Code extension's SSE push — one session = one
agent identity.
