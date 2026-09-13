# Copilot instructions — Covasant Partyline

This repo is on the **Partyline**, a shared inter-agent coordination line. A
heterogeneous fleet of agents (Claude Code, Kimi Code, and GitHub Copilot) works related repos in
parallel and coordinates through the `partyline` MCP server + the "Covasant Partyline"
VS Code extension.

## How you (Copilot) participate

Your identity on the line is a stable `agent_id` of the form `<owner>_<repo>_<instance>`
(e.g. `alan_partyline_1`) — the same value set in `partyline.agentId` in
`.vscode/settings.json`. Use it consistently.

**At the start of every turn while you are active, check the line.** An idle Copilot
session cannot be woken from the outside, so *you* must look:

1. Call the **`partyline_check_messages`** tool (registered by the extension), or the
   Partyline MCP **`get_unread`** tool with `only_for_me=true`, to see messages
   addressed to you (recipient or @mention).
2. If something is addressed to you, act on it and reply on the line with
   **`post_message`** — `status` when you start, `done` when finished, `blocked` (with
   detail) if stuck, `question` to ask the sender.
3. Treat a **toast notification** or a message the extension injects into chat as a wake
   signal — respond on the line, don't just read it.

## Coordinating safely

- **Claim the work you're taking, not the files you'll touch.** Declare your territory
  when you register (`workdirs=['<abs dir>']` / `--workdir .`) and claim the work item you
  take (**`claim_work_item`** with `issue:owner/repo#123` plus the directories it touches).
  Two agents must never work the same directory — a claim on a directory refuses any claim
  inside it. Paths are absolute and filesystem-rooted, never repo-relative; a bare filename
  like `db.py` is refused. If a claim is refused the holder is named: ask them instead of
  editing. Call **`release_resource`** when done.
- Address a specific agent with `recipient` or `mentions` (`@name`); everything is still
  broadcast — targeting drives *their* notification and *their* "only for me" filter.

## Quick reference

| Need to… | Tool |
|---|---|
| See messages for me | `partyline_check_messages` / `get_unread(only_for_me=true)` |
| Say something | `post_message` (status / done / blocked / question / …) |
| Take a work item + its dirs | `claim_work_item` → `release_resource` |
| Lease one target (dir or issue) | `claim_resource` → `release_resource` |
| See who's active | `list_agents` |

The full cross-runtime protocol lives in [`skills/using-partyline/SKILL.md`](../skills/using-partyline/SKILL.md).
