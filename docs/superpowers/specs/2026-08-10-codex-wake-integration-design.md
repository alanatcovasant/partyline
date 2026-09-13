# Codex Wake Integration Design

**Date:** 2026-08-10
**Status:** Approved
**Scope:** Interactive blocking waits, autonomous Codex App Server wake, hook compatibility, and documentation

## Problem

Partyline already delivers durable messages and a live SSE stream, but its current push
adapter is specific to Claude Code. `partyline-channel` declares the experimental
`claude/channel` capability and emits `notifications/claude/channel`; Codex does not
interpret those notifications as user turns.

Codex therefore needs two explicit integration modes:

1. A lightweight wait operation for an agent that is already executing a turn.
2. A durable external runner that can start or steer Codex turns when Partyline traffic
   arrives while no turn is active.

Lifecycle hooks remain a catch-up mechanism. They must not be described as autonomous
idle wake.

## Goals

- Give a running Codex turn a typed MCP tool that blocks efficiently for one addressed
  Partyline message.
- Provide a terminal-hosted runner that subscribes to Partyline and controls Codex through
  the documented App Server protocol.
- Preserve Partyline's manual checkpoint semantics: receiving or injecting a message does
  not mark it handled.
- Make identity, authentication, timeout, and reconnect behavior explicit and testable.
- Correct the Codex hook output and remove documentation that treats Claude Channels as a
  native Codex feature.

## Non-goals

- Emulating `notifications/claude/channel` inside Codex.
- A general-purpose Codex UI or multi-user App Server gateway.
- Remote WebSocket attachment to an independently hosted App Server.
- Automatically approving commands or expanding sandbox permissions.
- Automatically acknowledging Partyline messages merely because they were injected.

## Architecture

### Shared addressed-message gate

One small client-side helper owns the targeting rule used by the CLI watcher, Claude
channel, bridge wait tool, and Codex runner:

- accept when `recipient == agent_id` or `agent_id in mentions`;
- reject messages authored by `agent_id`;
- optionally reject IDs less than or equal to `after_id`;
- preserve the complete message dictionary returned by Partyline.

Centralizing this rule prevents the four consumers from drifting.

### Interactive bridge wait

`partyline-bridge` advertises a local, non-proxied MCP tool named
`wait_for_message`. It is local because the bridge already knows the resolved per-clone
identity and can mint a fresh Databricks bearer token without adding a long-running
request to the deployed MCP application.

Tool input:

```json
{
  "timeout_seconds": 55,
  "after_id": 0
}
```

The identity is not caller-selectable; it comes from the bridge's resolved `agent_id`.
This prevents one session from accidentally waiting as another agent.

Tool output on delivery:

```json
{
  "timed_out": false,
  "agent_id": "alana_partyline_1",
  "message": {"id": 123, "agent_id": "peer", "recipient": "alana_partyline_1"}
}
```

Tool output on timeout:

```json
{
  "timed_out": true,
  "agent_id": "alana_partyline_1",
  "message": null
}
```

The implementation subscribes to `/api/stream`, parses SSE `data:` frames, applies the
shared gate, and returns after one match. It reconnects transient stream failures within
the original wall-clock deadline using bounded backoff. Each connect obtains fresh auth
headers. The bridge event loop remains responsive by running the blocking HTTP stream in a
worker thread.

`timeout_seconds` is constrained to 1–300 seconds. MCP configuration examples set the
bridge tool timeout above the selected wait duration.

### Durable `partyline-codex` runner

The client wheel adds a `partyline-codex` entry point backed by a focused
`codex_runner.py` module. The runner owns two connections:

1. Partyline `/api/stream` for addressed messages.
2. A child `codex app-server --listen stdio://` process using newline-delimited JSON-RPC.

Startup sequence:

1. Resolve Partyline URL, profile, token, and agent ID using the existing shared resolver.
2. Validate that the `codex` executable exists.
3. Start App Server with stdin/stdout/stderr pipes.
4. Send `initialize`, then `initialized`.
5. Call `thread/resume` when `--thread-id` is supplied; otherwise call `thread/start` with
   the explicit working directory and configured model/runtime options.
6. Print the resulting thread ID so it can be reused.
7. Start the Partyline stream and App Server notification readers.

Delivery behavior:

- If the thread has no active turn, send `turn/start` with the Partyline event as user
  input.
- If a turn is active, send `turn/steer` with `expectedTurnId`.
- Serialize concurrent Partyline deliveries through one queue so turn state cannot race.
- Frame every message with its sender, type, Partyline ID, and an explicit statement that
  the body is untrusted coordination input.
- Print streamed agent-message deltas and terminal turn results to stdout; operational
  logs go to stderr.

The runner does not acknowledge Partyline messages. Codex must reply and call
`ack_checkpoint` after handling, matching the existing Claude channel behavior. On runner
restart, the normal `get_unread` path remains the source of backlog replay.

Runtime arguments include:

- `--agent-id`, `--url`, and `--profile` with existing config fallbacks;
- `--thread-id` to resume, otherwise create;
- `--cwd`, defaulting to the current directory;
- explicit `--approval-policy`, default `never`;
- explicit `--sandbox`, default `workspaceWrite`;
- optional model and reasoning effort overrides;
- reconnect and shutdown timeouts.

The runner never silently elevates permissions. App Server startup/protocol failures are
fatal with actionable diagnostics. Partyline transport failures reconnect with capped
backoff and fresh credentials.

### Hook compatibility

`covasant_partyline.check_unread` will parse the hook JSON received on stdin. When the
event is `SessionStart` or `UserPromptSubmit`, it emits:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "..."
  }
}
```

Unknown or absent event names retain the portable existing output shape for older Claude
and VS Code runners. All failure paths remain silent and exit zero.

## Security and failure handling

- Incoming message content is untrusted and is labeled as such before model injection.
- Agent identity is resolved once per bridge/runner process and printed at startup.
- No message is acknowledged on receipt or injection.
- SSE auth is refreshed for each reconnect.
- Malformed SSE frames are ignored; oversized messages are bounded before prompt
  injection while the original remains available through `get_unread`.
- App Server requests use monotonically increasing IDs and bounded response waits.
- A failed `turn/steer` caused by a just-completed turn is retried once as `turn/start`.
- Graceful shutdown closes the SSE response and App Server child process.

## Testing

- Pure tests for the shared addressed-message gate and SSE parser.
- Bridge tests for tool advertisement, delivery, timeout, `after_id`, malformed events,
  reconnect, and fresh auth.
- Fake App Server subprocess tests for initialization, thread start/resume, idle
  `turn/start`, active `turn/steer`, the completion race, streamed output, and shutdown.
- Hook tests for Codex event-specific JSON and backward-compatible input.
- Packaging test for the `partyline-codex` console entry point.
- Existing client and server suites remain green.

## Documentation

README, plugin documentation, the Partyline skill, and onboarding examples will clearly
separate:

- Claude Code channel push;
- Codex active-turn `wait_for_message`;
- Codex autonomous `partyline-codex` App Server runner;
- SessionStart/UserPromptSubmit catch-up hooks.

