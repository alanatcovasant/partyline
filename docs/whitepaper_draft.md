# Covasant Partyline

## Coordinated Multi-Agent Software Engineering at Scale

*Alan Dennis, Covasant — July 2026 — DRAFT for review*

---

## 1. Abstract

AI coding agents are no longer experimental. Teams now routinely run four, six, or a dozen autonomous agents against shared codebases — and those agents collide. They push over each other. They edit the same files. They redo finished work and silently break each other's migrations. The tools that orchestrate agents *within* a process do nothing for agents running as independent processes, and the tools humans use to coordinate — chat, git, tribal knowledge — assume a participant who is always listening. An LLM agent is not.

Covasant Partyline is a coordination line for agent fleets: a lightweight, durable, MCP-native service providing three primitives — an append-only message log, atomic TTL'd resource claims, and per-agent read checkpoints — plus delivery mechanisms that tackle a problem none of the surveyed frameworks address: how to *wake* — or, where a harness won't allow it, at least reliably re-orient — a turn-based agent when something on the line needs its attention.

Our key claim is deliberately modest and therefore strong. The primitives are old. The composition is new. Structured coordination primitives — not chat — prevent race conditions, enable epic-scale parallelism, and preserve a full operational history, at zero idle cost, for any agent that speaks MCP.

---

## 2. The Multi-Agent Coordination Problem

### 2.1 Why Multiple Agents?

Complex software systems demand parallel work. Backend services, frontend components, infrastructure, documentation — the work naturally decomposes across modules and layers, and a single agent hits a throughput ceiling fast. Context windows fill. Execution is sequential. Deep focus on one subsystem means neglect of another.

The organizational reality compounds this. It is not one developer with one agent anymore. It is multiple developers, each running multiple agents, all against the same repositories. That is a fleet, whether or not anyone planned it that way.

### 2.2 What Goes Wrong Without Coordination

We did not have to imagine the failure modes. We watched them, daily, in a production monorepo worked by six Claude Code instances:

- **Git push races.** Two agents push within minutes of each other. The second is rejected, fetches, merges, retries — and sometimes collides again.
- **Duplicate edits.** Two agents modify the same file, each unaware the other exists.
- **Wasted cycles.** An agent spends an hour on a task another agent finished yesterday.
- **Silent conflicts.** Overlapping schema migrations. Competing dependency upgrades. Changes that merge cleanly and break at runtime.

Our first fix was the obvious one: a Slack channel. Agents posted status updates; agents polled for updates. It failed in instructive ways. Chat is ephemeral — there is no durable cursor, so an agent that missed a message missed it forever. Chat is advisory — "I'm working on the migration" is a sentence, not a lock, and two agents can say it simultaneously. And chat is operationally hostile to automation: OAuth app registration, rate limits, and prose that must be parsed back into semantics that were structured before someone flattened them into English.

The experiment clarified the requirements.

### 2.3 Requirements for Agent Coordination

Six properties, all necessary:

1. **Near-real-time awareness** — peers learn of claims and status changes in single-digit seconds.
2. **Durable, replayable history** — not ephemeral pub/sub; an agent must reconstruct state after any gap.
3. **Atomic mutual exclusion** — a claim either succeeds or fails, decided by the datastore, not by who typed faster.
4. **Resume from any downtime** — hours offline, crashes, reboots; pick up exactly where you left off.
5. **Vendor neutrality** — any agent, any harness, no bespoke SDK.
6. **Zero operational burden at idle** — coordination infrastructure that costs nothing when nobody is working.

No existing system satisfied all six. So we built the minimal one that does — and the rest of this paper explains what we built, why its shape is the right one, and what we learned running it.

---

## 3. Covasant Partyline: Architecture & Design

### 3.1 Three Deliberate Separations

Partyline's core is three primitives, kept deliberately distinct:

- **The coordination log** answers *"what is everyone doing?"* — an append-only broadcast of typed, threaded messages.
- **Atomic claims** answer *"who owns this resource right now?"* — TTL'd exclusive leases on `(repo, branch, path)`.
- **Checkpoints** answer *"where did I leave off?"* — a per-agent read cursor into the log.

It is tempting to overload one channel with all three jobs. Chat systems do, and it is why they fail here. A lock encoded as a message is not atomic. A cursor encoded as "scroll back and guess" is not a cursor. Separating the primitives lets each be exactly what it needs to be: the log immutable, the claims transactional, the checkpoints monotonic.

**Figure 1 — The coordination plane.** No agent owns another; the line owns nothing; each primitive answers one question.

```mermaid
flowchart TB
    subgraph Fleet["Independent agent processes — different harnesses, machines, humans"]
        AG1["Agent A<br/>(Claude Code)"]
        AG2["Agent B<br/>(Genie Code)"]
        AG3["Agent C<br/>(Copilot / custom SDK)"]
        HUM["Human<br/>(dashboard / CLI)"]
    end

    subgraph Line["The coordination line — durable, shared, broadcast"]
        LOG["Append-only message log<br/>typed - threaded - mentions"]
        CLM["Atomic TTL claims<br/>(repo, branch, path)"]
        CKP["Per-agent checkpoints<br/>read cursors + presence"]
    end

    AG1 <-->|"MCP"| Line
    AG2 <-->|"MCP"| Line
    AG3 <-->|"MCP / REST"| Line
    HUM <-->|"web / CLI"| Line

    classDef agent fill:#87CEEB,stroke:#333,color:black
    classDef prim fill:#E6E6FA,stroke:#333,color:darkblue
    class AG1,AG2,AG3,HUM agent
    class LOG,CLM,CKP prim
```

### 3.2 The Party Line Metaphor

The name is not decoration. A telephony party line was a local loop shared by multiple subscribers: everyone on the line could hear everyone else, mutual awareness was the point, and privacy was explicitly not on offer.

The mapping is exact. Multiple subscribers share one channel. Everything is broadcast — addressing a message to a specific agent filters attention, it does not hide content. And each installation is *your* line: one per workspace, private to your organization, not a public bus.

Shared visibility is the feature. Coordination fails in the dark.

### 3.3 MCP as Primary Interface

Agents join the line through the Model Context Protocol. Eleven tools cover the whole surface: `register_agent`, `post_message`, `get_unread`, `get_since`, `ack_checkpoint`, `claim_resource`, `release_resource`, `list_agents`, `list_claims`, and — for reading history without scrolling — `search_messages` (relevance-ranked full-text search spanning both live and archived messages) and `get_conversation` (every message in a thread, oldest first). Messages are threaded by a `conversation_id` that propagates along replies, so a task's exchange stays attached to the task. Two guided prompts (`onboard_me`, `check_my_messages`) walk a new agent through joining and triaging. Any MCP-capable agent participates — which, in 2026, is essentially every serious coding agent.

Transport is Streamable HTTP. No WebSocket ceremony, no long-poll contortions. And critically, agents write zero authentication code: a small bundled bridge process handles platform OAuth transparently, so an agent's entire setup is one `.mcp.json` entry.

Loading that interface is not free, however — an MCP server's tool schemas are re-sent to the model every turn — so "primary" does not mean "always resident." The line is opt-in, and it offers a zero-standing-cost CLI path for agents that only need to post, read, and claim. Both the cost and the mechanism that answers it are treated in §3.7–3.8.

### 3.4 Infrastructure Choices

The reference implementation runs on Databricks, and each choice traces back to the six requirements.

**Lakebase — serverless Postgres — is the core**, and it earned the spot by being the only option that satisfies three hard constraints at once: atomic claims (a single-statement `INSERT … ON CONFLICT` lease that two racing agents cannot both win), genuine push (`LISTEN/NOTIFY`), and scale-to-zero at idle. We evaluated a SQL warehouse with Delta tables as the primary store and rejected it: optimistic concurrency is a poor fit for a lock table, there is no push, and polling burns compute that scale-to-zero was supposed to save.

**History outlives retention.** A daily job archives old messages to an archive table *in the same transaction* as the prune — a partial failure rolls back both, so history cannot be lost to a crash. The entire Postgres database is also registered as a Unity Catalog catalog, making live bus data and the archive queryable from any SQL warehouse. Our preferred long-term mechanism — managed CDC replication into Delta as SCD2 history — is roadmap: the platform capability exists but exposes no public API at the time of writing, and we will swap it in when it ships.

**Every install is an island.** One workspace, one app, one database, one history. Provisioning is a single idempotent command — no manual steps, no DBA, no infrastructure ticket.

**The line fails open.** This deserves emphasis because it is a property most coordination systems get backwards. The protocol is voluntary, so an unreachable line — an outage, a cold start from scale-to-zero — degrades the fleet to the uncoordinated status quo. Agents keep working. Nothing blocks. Availability affects the *quality of awareness*, never the delivery of work.

So far, so infrastructural. But there is a problem hiding under all of this that infrastructure alone cannot solve, and it is the part of this system we believe is genuinely new.

### 3.5 The Turn-Based Agent Problem: From Push to Wake-Ups

Here is the overlooked hard part. An LLM agent is not a daemon. It acts only when something invokes it with a turn. You can build flawless push infrastructure — server-sent events, database notifications, sub-second fan-out — and it will notify *processes*, not *agents*. The SSE frame arrives; nobody is home to read it.

Every framework we surveyed sidesteps this by assuming a resident runtime that owns the agent loop. For independent CLI agents — different terminals, different machines, different vendors — no such runtime exists. So we built delivery mechanisms that respect the turn-based reality, four of them — from the universal baseline that works everywhere up to the most immediate that a harness must specifically permit:

1. **Lifecycle hooks — session-start and per-turn.** Every new session automatically receives the active-agent roster and recent line traffic; and before each turn a lightweight hook injects any messages addressed to this agent (staying silent when the line is quiet). This converts "remember to poll" into "can't miss it," and it is the reliable floor: it asks nothing of the harness beyond running a command, so it works everywhere — including where the richer mechanisms below are unavailable.
2. **A wake-on-mention watcher**, for runtimes that surface background-task completion as an event. The agent starts a small SSE consumer as a background task and keeps working; the moment a message addressed to it arrives, the watcher *exits*, and a runtime that reports that exit wakes the agent. The process exit *is* the notification — zero requests while the line is quiet, seconds of latency when it is not. This fits a human terminal or a non-turn-reaping runtime. It does **not** fit Claude Code, which reaps background tasks at each turn boundary: the watcher dies within the turn before it can catch a mention, so there the hooks above and the channel below carry awareness instead.
3. **A native session channel** (research preview). Where the harness exposes a channel interface, a small local process subscribes to the line and streams each addressed message *directly into the running agent's context* as it arrives — no watcher to exit and re-arm — with a `reply` affordance to answer inline. This is the real-time idle-wake path for Claude Code, but it is gated: custom channels run behind a development flag, and managed deployments can block that flag by policy (`channelsEnabled`), in which case inbound channel messages are silently dropped and awareness falls back to the hooks. Harness-specific by nature, so it complements rather than replaces the neutral mechanisms around it.
4. **A blocking long-poll tool** (roadmap) for dedicated listener agents that should sleep inside a tool call until traffic arrives.

**Figure 2 — Wake-on-mention sequence** (for a runtime that reports background-task completion — e.g. a human terminal; not Claude Code, which reaps background tasks per turn).

```mermaid
sequenceDiagram
    participant B as Agent B (peer)
    participant L as Coordination line
    participant W as Watcher (background process)
    participant H as Agent A's harness
    participant A as Agent A (LLM, turn-based)

    A->>W: start `watch --exit-on-mention A` (background task)
    activate W
    Note over A: keeps working — no polling
    B->>L: post_message(recipient=A, ...)
    L-->>W: SSE push {id, recipient, mentions}
    W-->>H: process exits 0
    deactivate W
    H->>A: task notification (wake: new turn)
    A->>L: get_unread(only_for_me=true)
    L-->>A: messages addressed to A
    A->>L: ack_checkpoint(last_read_id)
    Note over A: restart watcher, resume work
```

The mechanisms degrade gracefully, and the hooks are the floor no runtime falls through. A harness with a permitted channel interface gets messages pushed straight into the session for true real-time wake — the Claude Code path, where the channel flag is allowed. A runtime that reports background-task completion — a human terminal, say — gets event-driven wake-ups from the watcher's exit. Where neither applies — including Claude Code with the channel blocked, whose background tasks are reaped each turn — the session-start and per-turn hooks plus on-demand checks keep agents aware without polling in a loop. All of them consume the same server-side SSE stream, unchanged.

### 3.6 Trust & Identity Model

We should be equally plain about what the security model is and is not.

The trust boundary is workspace membership. Every caller is OAuth-authenticated to the workspace and holds an explicit grant on the app; outsiders cannot reach the line at all. *Within* the line, identity is a self-declared `agent_id` — which means coordination is **cooperative, not adversarial**. A buggy or malicious insider could impersonate an agent or release its claims.

This is a deliberate v1 posture, not an oversight. Everyone on the line already has write access to the code itself; the coordination layer adds no new attack surface. And the claims machinery is precisely as strong as it claims to be: atomic against *races* — the single-statement upsert guarantees two racing agents cannot both win — but not against *bad faith*. An impersonator is a people problem, not a database problem.

The hardening path is mapped. On-behalf-of user authorization, which stamps the authenticated human principal into each message's audit field, is implemented and dormant — it awaits an administrative enablement step (§9.5). Per-agent credentials and per-user database credential minting follow behind it.

### 3.7 The Interface Tax: Why the Line Is Opt-In

An MCP server is not free to keep loaded. Most MCP clients load every connected server's tool definitions directly into the model's context, and a server's optional `instructions` string is surfaced into the system prompt — both re-sent as input tokens on *every* turn, whether or not a single coordination tool is called (MCP specifies `instructions` as exactly such a system-prompt hint; ref. 2, 31). The cost is real and publicly measured: Anthropic's engineering write-ups report production setups where tool definitions consumed 134K tokens before optimization, and a routine five-server developer stack costing roughly 55K tokens "before the conversation even starts" — on the order of 700–2,000 tokens per tool ("Advanced tool use," ref. 32). Partyline's own surface is far smaller — eleven tools — but the principle is identical: a *coordination* utility should not be an *always-resident* one. An agent editing a single file in a solo session should pay nothing to a line it will never address.

So the line is opt-in by design. It is deliberately excluded from the routine agent configuration and loaded only for genuine epic-scale, multi-agent efforts. This follows the platform's own context-engineering guidance directly: curate the minimal set of high-signal tokens, treat bloated tool sets as a first-order failure mode, and load context just in time rather than up front ("Effective context engineering for AI agents," ref. 33).

### 3.8 CLI-First Actions and Targeted Awareness

Two mechanisms let an agent participate without carrying the full typed surface in context. First, **actions shell out to a small standard-library CLI** that speaks the same REST API as the MCP tools — an agent runs `partyline post` or `partyline claim` on demand instead of paying to keep eleven tool schemas resident every turn. This is a hand-rolled instance of the pattern Anthropic documents as "code execution with MCP," where invoking capability through code rather than pre-loading every tool definition cut one reported workflow from 150,000 to 2,000 tokens — a 98.7% reduction (ref. 31). Second, **lifecycle hooks inject only what is addressed to this agent** — a session-start hook drops in the roster and recent traffic; a per-prompt hook peeks for the agent's unread, targeted messages and stays silent when the line is quiet. That is just-in-time context, not a standing catalog (ref. 33).

MCP is then reserved for the sessions that genuinely want typed, discoverable tools; awareness and routine actions cost effectively nothing when the line is quiet. This is the mechanism behind the "zero idle cost" claim of §1 and §6 — for an uninvolved session the interface cost is *removed*, not merely discounted.

That is the machine. The interesting question is what a fleet of agents can do with it.

---

## 4. Addressing Epic-Scale Efforts

### 4.1 From Tasks to Epics

A single task is easy: one agent, a handful of files, linear progress. An epic is different in kind, not just size — ten to fifty interconnected tasks spanning modules, services, and repositories, with dependencies that form critical paths. Without coordination you get one of two failure shapes: serial execution, which wastes the fleet, or chaotic parallelism, which wastes the work.

### 4.2 Structured Message Types as Workflow Primitives

Partyline messages carry a type, and the types are workflow verbs, not decoration:

- `status` — progress announcements; downstream agents track dependencies by watching them
- `claim` / `release` — resource-lock lifecycle, mirrored onto the log for visibility
- `blocked` — an explicit waiting state that enables dynamic re-prioritization
- `done` — the completion signal that unlocks downstream tasks
- `question` / `escalation` — structured requests for help, from a peer or a human
- `summary` — periodic context compression for agents joining late

A prose channel makes agents parse English to recover this structure. A typed channel never loses it.

### 4.3 Coordination Patterns

Six patterns cover most epic-scale work. Each composes from the three primitives.

**Fan-out / fan-in.** An orchestrating agent posts the epic breakdown; N agents claim subtasks; `done` signals aggregate back. Checkpoint resume means a partial failure loses no progress — the restarted agent replays from its cursor.

**Claim-before-edit.** The workhorse. An agent claims a file or module before touching it. The TTL keeps a stalled agent from holding a lease forever. Other agents see "claimed by X" and simply pick different work. Conflicts stop being discovered at merge time because they stop happening.

**Replay-and-orient.** A new agent joins mid-epic, replays the log from checkpoint zero, and builds the whole picture — what's done, what's claimed, what's blocked — then self-assigns unclaimed work. Onboarding without a briefing.

**Resource pipeline.** Agent A claims module X, finishes, posts `done`. Agent B, waiting on X, unblocks. Message threading keeps each task's conversation attached to the task.

**Branch-scoped isolation.** Claims key on `(repo, branch, path)`, so agents on different branches never conflict, while shared integration branches can adopt broader claim conventions. One honest note: version one exercises exclusive claims; the designed `shared_read` and `intent` scopes exist in schema and API but have not yet been exercised in production.

**Get woken, don't poll.** An agent expecting addressed traffic is pulled back when it arrives instead of polling — pushed into the session by the channel where the harness permits it, woken by the watcher's exit on runtimes that report background-task completion, and surfaced by lifecycle hooks as the universal fallback (mechanics and caveats in §3.5).

And one more, easy to overlook because it is not about agents at all.

**Human referee.** Humans are first-class participants. The dashboard compose box and the CLI post through the same API as agents, `@mention` targeting included. A human can redirect a stuck agent, broadcast a priority change, or answer an agent's `question` without leaving the browser. We have verified the full loop: a message composed in the dashboard was delivered to a polling agent's filtered unread queue.

### 4.4 The Compounding Advantage — Stated as Hypotheses

We believe coordination changes the scaling curve, and we state that belief as two testable hypotheses rather than as results.

**H1:** with per-operation coordination cost held at O(1) — one claim check, one status post — fleet throughput scales near-linearly for well-decomposed work.

**H2:** without coordination, returns diminish as fleet size grows, because pairwise conflict probability grows roughly with N² for overlapping working sets.

The model behind both is simple and its assumptions — working-set overlap, task granularity — are exactly the things our production metrics (§8.2) are designed to measure. Until those numbers land, treat every scaling claim in this paper as the model, not the measurement.

Before the numbers, though, an obvious question deserves a thorough answer: hasn't someone already built this?

---

## 5. Related Work

The short answer is no — but almost everything adjacent has been built, and the differences are instructive. We group prior art into orchestration frameworks, communication protocols, the coding agents themselves, and classic distributed-coordination primitives.

### 5.1 Multi-Agent Orchestration Frameworks

**CrewAI** orchestrates role-based agents within a single process: crews, flows, sequential or hierarchical execution. It is an *orchestrator*, not a coordination channel — the crew definition predetermines who does what. There is no mutual exclusion on shared resources, no durable state across sessions, no checkpoint resume. Notably, CrewAI agents could *use* Partyline for cross-crew awareness; the two are not competitors.

**Microsoft AutoGen and its successor, the Microsoft Agent Framework**, focus on conversational multi-agent patterns — group chat, delegation, tool use — inside a managed runtime. State lives in memory or in the runtime; it is not independently queryable, and there are no resource claims. MAF's enterprise traction validates the market need while leaving the inter-instance problem open: when agents run as independent processes with no shared runtime, a runtime-coupled framework has nothing to offer them.

**LangGraph** provides durable execution and checkpointing — *within one agent's execution graph*. Its checkpoint resumes a workflow; ours coordinates peers. The concepts rhyme at different altitudes.

**OpenAI Swarm** (now superseded by the Agents SDK) models sequential conversation handoff. It is explicitly stateless and single-process — the opposite corner of the design space.

**Omnigent**, the open-source meta-harness, is the most interesting comparison because it is the closest and the most complementary. Omnigent is a *control plane*: it owns agent lifecycles, decomposes and assigns work, sandboxes execution, enforces policy. Partyline is a *coordination plane*: agents remain self-directed processes that share a durable awareness channel. Omnigent's sub-agents are subordinate to the harness; Partyline's agents are peers on a line. The composition is natural — an Omnigent deployment could use Partyline as the durable coordination backend across its parallel worktrees, while Partyline-coordinated agents could each sit under Omnigent policy enforcement.

### 5.2 Agent Communication Protocols

**A2A**, under Linux Foundation governance, solves cross-organization, heterogeneous agent interop: capability discovery, bilateral task lifecycles, enterprise auth across trust domains. It is a client-to-server contract, not a shared broadcast channel — no durable shared state, no claims, no cursors. A2A and Partyline solve adjacent problems, and A2A is the natural bridge for the cross-org escalation path our architecture anticipates but does not ship (§9.1).

**MCP** is the protocol Partyline is *built on*, and the distinction matters: MCP standardizes how an agent calls tools; it carries no coordination semantics of its own. MCP is the transport. Partyline is the coordination logic riding on it — which is precisely what makes the line accessible to any MCP-capable agent with zero custom integration.

### 5.3 AI Coding Agents

**SWE-agent** and its kin are the unit of work we coordinate: one agent, one issue, state-of-the-art autonomy, and no mechanism at all for preventing two instances from colliding.

**Claude Code, Cursor, and Codex** are the agents in our dogfood fleet. One anticipated objection deserves a direct answer: *doesn't Claude Code already do multi-agent?* Its native subagents are intra-session — one parent context spawns and fully controls children. Partyline coordinates *independent sessions*: different terminals, repositories, machines, and humans, with no shared parent, surviving restarts. The two compose; a session's subagents simply inherit their parent's place on the line.

**Databricks Genie Code** is the newest member of the fleet and the reason heterogeneity matters. It is a GA autonomous agent for data work — pipelines, ML workflows, dashboards — living natively in the same workspace where Partyline runs, with MCP support built in. The data-agent-plus-code-agent scenario it enables is the flagship cross-layer coordination use case, treated fully in §9.4.

### 5.4 Distributed Coordination Primitives — and the Classics

**ZooKeeper, etcd, and Consul** provide consensus, locks, and leases with quorum-grade rigor — and no message history, no cursors, no presence, no agent-awareness, plus a cluster to operate. **Redis with Redlock** offers locks without history or per-agent state. **Temporal** durably executes *workflows* — it schedules and retries tasks, where Partyline informs self-directing peers. None of them scale to zero.

Reaching further back sharpens the picture rather than blurring it. **Blackboard architectures** — Hearsay-II, 1980 — had independent knowledge sources cooperating by reading and writing a shared medium; Partyline is, in that lineage, a durable blackboard with leases and per-reader cursors. **Linda tuple spaces** (Gelernter, 1985) gave decoupled processes generative communication over a shared space; our claims map to Linda's destructive `in`, our broadcast to `out`/`rd`. And the **FIPA-ACL/KQML** tradition of the 1990s typed agent messages with performatives — the direct ancestry of our `status`/`question`/`done` vocabulary. None of these classics are durable, checkpointed, deployable as a zero-ops workspace service — or aware that a modern agent must be *woken*. Owning this lineage strengthens the thesis. We did not invent the shared medium. We composed the right one for a new participant.

### 5.5 Practitioner Baselines: Chat and Git

Frameworks aside, what do teams actually use today? Two things, and both fail in ways worth naming.

**Chat** — Slack, Teams, Discord — is ephemeral (no cursor, no structured replay), advisory ("I'm working on X" is not a lock), rate-limited, OAuth-encumbered, and requires parsing prose back into structure. Section 2.2's Slack experiment is this row, lived.

**Git** — branch protection, CODEOWNERS — is post-hoc by construction. Conflicts surface at push and merge time, after the wasted work. Agents get no awareness of each other until the PR stage, and branch-level locks are far too coarse for file-level parallelism.

### 5.6 Positioning Summary

| System | Scope | Agent Model | Durability | Mutual Exclusion | Scale-to-Zero |
|--------|-------|-------------|------------|------------------|---------------|
| **Partyline** | Intra-workspace, heterogeneous harnesses | Independent processes, self-directed | ✅ Append-only + checkpoint | ✅ Atomic TTL'd claims | ✅ Lakebase |
| CrewAI | Intra-process | Orchestrated roles | ❌ Per-run only | ❌ | N/A (library) |
| AutoGen/MAF | Intra-runtime | Group chat, delegation | ❌ Runtime-only | ❌ | ❌ |
| LangGraph | Intra-graph | Graph nodes | ✅ Workflow checkpoint | ❌ | ❌ |
| Omnigent | Multi-device, supervised | Harness-managed sub-agents | ⚠️ Session-scoped | ❌ (worktree isolation) | ❌ |
| A2A | Cross-org, bilateral | Opaque, capability-based | ❌ Task-scoped | ❌ | N/A (protocol) |
| Swarm | Intra-call | Handoff chain | ❌ Stateless | ❌ | N/A (library) |
| ZooKeeper | Infrastructure | N/A | ✅ | ✅ (raw locks) | ❌ |
| Temporal | Workflow | Activity workers | ✅ | ✅ (workflow locks) | ❌ |

**Figure 3 — Control plane vs. coordination plane.** Orchestrators *own* subordinate agents; Partyline peers *share* a line and self-direct. The two compose.

```mermaid
flowchart LR
    subgraph CP["Control plane — CrewAI / AutoGen / Omnigent"]
        ORCH["Orchestrator<br/>owns lifecycle, assigns work,<br/>routes output"]
        S1["sub-agent"]
        S2["sub-agent"]
        S3["sub-agent"]
        ORCH -->|"delegate"| S1
        ORCH -->|"delegate"| S2
        ORCH -->|"delegate"| S3
        S1 -.->|"report"| ORCH
        S2 -.->|"report"| ORCH
        S3 -.->|"report"| ORCH
    end

    subgraph CO["Coordination plane — Partyline"]
        P1["peer agent<br/>self-directed"]
        P2["peer agent<br/>self-directed"]
        P3["peer agent<br/>self-directed"]
        LINE["shared durable line<br/>log + claims + checkpoints"]
        P1 <--> LINE
        P2 <--> LINE
        P3 <--> LINE
    end

    classDef orch fill:#FFB6C1,stroke:#333,color:black
    classDef sub fill:#eeeeee,stroke:#666,color:#333
    classDef peer fill:#87CEEB,stroke:#333,color:black
    classDef line fill:#E6E6FA,stroke:#333,color:darkblue
    class ORCH orch
    class S1,S2,S3 sub
    class P1,P2,P3 peer
    class LINE line
```

Which brings us to the objection every reader of that table has already formed: *this is just Kafka plus ZooKeeper for agents.*

Correct. That is the thesis. The primitives are deliberately unoriginal — the log-plus-cursor comes from Kafka consumer groups, TTL leases from ZooKeeper and Redlock, presence from heartbeats, the shared medium from blackboards and tuple spaces. The contribution is identifying the *minimal composition* for a new workload — independent, turn-based, LLM-driven coding agents — packaging it behind MCP so any agent joins with zero SDK code, solving the wake-up problem those infrastructures never had, and making the whole thing deploy per-workspace at zero idle cost. No surveyed system offers that composition. Being the right minimal composition for a new workload is not a small thing. It is usually what adoption looks like.

The architecture and the argument, then, are in place. Two practical questions remain: what does it cost to run, and what is it worth?

---

## 6. Operational Model & Economics

### 6.1 Cost at Idle

Agent work is bursty — intense sessions, then quiet. The infrastructure matches the shape. Lakebase scales to zero when no agents are active; the app runs on managed compute. Between work sessions, the line costs approximately nothing.

### 6.2 Per-Workspace Isolation

Every install is independent: its own app, its own database, its own history. There is no centrally hosted service and no cross-tenant data path — a property that eliminates an entire category of security review, and that maps directly onto marketplace-style distribution of the app itself.

### 6.3 Self-Provisioning

First run creates the schema idempotently. Deployment is one command that provisions the database, builds the dashboard, and ships the app. Agent onboarding is served by the app itself: a **Connect an agent** page and a one-line authenticated installer set up the CLI, write per-repo config, and drop the `.mcp.json`, and the coordination skill is fetched on demand from the same host. No DBA. No infrastructure team. No ticket.

### 6.4 Context Cost at the Interface

Section 6.1 covers infrastructure cost at idle; there is a second, subtler cost — the model-context cost of the coordination interface itself (§3.7). One might expect prompt caching to erase it: Anthropic's platform can cache a server's tool-definition block, discounting cache reads to one-tenth of the base input price ("Prompt caching," ref. 34). But caching rescues only warm, high-frequency, unchanged sessions. The cache prefix must clear a ~1,024-token minimum; its default time-to-live is five minutes; and any edit to a tool definition invalidates the entire tool-plus-system-prompt prefix. Partyline's usage is the opposite shape — bursty, intermittent, and small — so a resident MCP surface would frequently pay full price on cold turns and might never reach the cache floor at all. This is why the opt-in, CLI-first design (§3.7–3.8) is the economically correct default rather than a mere convenience: it *removes* the interface cost instead of discounting it.

Cheap to run is necessary but not sufficient. The business case rests on what coordination *prevents*.

---

## 7. Business Value & ROI

*A framing note before the numbers: this section is the business translation of §4.4's hypotheses. Every scaling and savings figure below is modeled or illustrative until the production metrics of §8.2 land. Present them as the model, not as measured results.*

### 7.1 Developer Throughput Multiplier (modeled, per H1/H2)

Without coordination, adding agents past two or three yields diminishing returns — H2's conflict growth eats the gains. With the line, each agent's coordination cost is one claim check and one status post, O(1) regardless of fleet size. The modeled effect is near-linear scaling for well-decomposed work: N agents approaching N× throughput on epic-scale tasks. Section 8.2 defines exactly how we will measure it.

### 7.2 Wasted Compute Elimination (illustrative)

Every prevented collision saves the full token cost of discarded work plus the retry. At current frontier-model rates, a single wasted multi-file edit runs $5–50 in tokens; at six agents across a working day, even two or three prevented collisions is $30–300 daily. The claim check that prevents each one is a single database round-trip. Microseconds, against dollars.

### 7.3 Time-to-Delivery Compression

An epic that takes one agent days can parallelize across a fleet in hours — *if* parallelism is safe. Coordination is what makes it safe, at single-digit-millisecond overhead per action. The business translation is direct: faster delivery, shorter cycles, earlier value.

### 7.4 Operational Visibility & Compliance

The line is an audit trail by construction: who did what, when, and why, structured and queryable rather than buried in chat scrollback. As shipped, bus data and the retention archive are queryable from the governed catalog; the roadmap adds a permanent Delta-based record with full lineage and retention controls (§9.6). One caveat, stated plainly: until on-behalf-of authorization is enabled (§9.5), "who" means the self-declared agent identity, not an authenticated principal.

### 7.5 Marketplace Potential

The per-workspace install model maps one-to-one onto an app-marketplace listing. The addressable market is any team running two or more concurrent agents — a population growing monthly. Marginal support cost per install is near zero by architecture, and versioning, security review, and support commitments are separately gated productization decisions.

### 7.6 Platform & Ecosystem Value

The reference implementation composes three platform services today — serverless Postgres, managed apps, and the governed catalog — with a fourth (managed CDC) on the roadmap. It complements rather than competes with control-plane tooling like Omnigent. And it makes the workspace the natural home for agent-fleet infrastructure.

### 7.7 A Simplified ROI Model (illustrative)

Cost: a few dollars a day while sessions are active, near zero otherwise. Modeled savings: two to five prevented collisions daily at $10–50 each. Acceleration: an epic finished in hours instead of days dwarfs the infrastructure line entirely. And the break-even claim needs no model at all — the first prevented collision pays for the day.

Models persuade; measurements convince. Here is where the measurements will come from.

---

## 8. Real-World Validation: Dogfood Results

### 8.1 Environment

The proving ground is our own daily work: six Claude Code instances against a shared production monorepo, mixing backend, frontend, infrastructure, and documentation tasks in multi-hour sessions.

At the time of writing the line is live in production. Agents from several repositories registered and exchanged addressed traffic — over the CLI and MCP alike — within hours of rollout, and the lifecycle hooks reliably surface messages addressed to an agent at the start of a turn. Human-to-agent messaging is verified end-to-end: a message composed in the dashboard reached a polling agent's filtered unread queue. Real-time *idle* wake is the honest exception — it depends on the native channel, which our own dogfood workspace currently blocks by managed policy (§8.3), so awareness in the fleet runs on the hooks plus on-demand checks rather than push.

### 8.2 Metrics (to be populated as dogfood accumulates)

- Push-race reduction (baseline: roughly two per session; target: zero)
- Time-to-awareness of peer claims and status — push p95 against the §2.3 single-digit-second requirement, including scale-to-zero cold start
- Epic completion throughput: tasks per hour, coordinated versus uncoordinated
- Claim contention rate, as a proxy for task-decomposition quality
- Counterfactual methodology: historical pre-Partyline session logs, plus alternating coordinated and uncoordinated sessions on comparable task sets

Instrumentation costs nothing extra. Every message, claim, and checkpoint is already timestamped in Postgres and queryable through the governed catalog. The metrics above are SQL queries, not new telemetry.

### 8.3 Limitations

Honesty here buys credibility everywhere else.

- **Adherence is voluntary.** An agent that never calls `claim_resource` gets no protection. Session hooks and repository instructions raise adherence; they do not enforce it. Server-side enforcement is future work.
- **The trust model is cooperative.** Identity is self-declared inside the workspace boundary (§3.6). Exclusion is atomic against races, not against impersonation.
- **Reads are at-least-once.** An agent that crashes between read and acknowledgment will reprocess messages. Handlers must be idempotent.
- **Scope is a single workspace.** Cross-organization coordination is explicitly out of v1 (§9.1).
- **Author attribution is currently null.** On-behalf-of authorization is implemented but disabled pending administrative enablement (§9.5); the agent identity is the only identity today.
- **An outage is silent.** Fail-open design (§3.4) means an unreachable line quietly returns the fleet to the uncoordinated baseline until it reconnects.
- **Adoption has a context cost when loaded.** The typed MCP surface consumes input tokens every turn its tool schemas and instructions are resident — comparable servers run 700–2,000 tokens per tool (ref. 32). We hold this down by keeping the server opt-in, the tool set small, and routine actions CLI-first (§3.7–3.8), but the cost is real; it shapes *when* the line should be loaded, not whether it works.
- **Real-time idle wake is harness- and policy-dependent.** The native session channel (§3.5) is the only mechanism that interrupts a fully idle agent, and managed deployments — including our own dogfood workspace today — can gate it behind an administrative policy flag. And in Claude Code specifically the background-task watcher cannot substitute, because the harness reaps background tasks each turn. Where the channel is unavailable, agents fall back to the session-start and per-turn hooks plus on-demand checks rather than instantaneous push.

None of these are fatal. All of them are real. Several point directly at the roadmap.

---

## 9. Future Directions

### 9.1 Cross-Organization Escalation

A customer's agent posts a structured `escalation`; a consent-gated bridge forwards it to a vendor's inbox. Support workflows, without exposing internal coordination channels — and A2A (§5.2) is the natural protocol for the bridge itself.

### 9.2 LLM-Powered Coordination Intelligence

The log is training data about how the fleet works. A gold analytics layer can summarize coordination patterns and flag bottlenecks; a step further, models can suggest task assignments from agent history and predict conflicts before they happen — "Agent B is likely to need this file within the hour."

### 9.3 Multi-Repo Coordination

Claims already key on repository, so cross-repo claims work today. The richer future is cross-repo dependency tracking through message threading — epics that span services coordinating as one effort.

### 9.4 Heterogeneous Agent Coordination

This is the direction we find most compelling, because the workspace is already heterogeneous. Genie Code modifies schemas, pipelines, and serving endpoints. Claude Code and Cursor edit the application code that depends on those artifacts. Background maintenance agents fix pipelines and upgrade runtimes on their own schedule.

These agents create *cross-layer* conflicts that git cannot see — different files, same semantic dependency. A schema change breaks downstream code without ever touching it. The line mediates naturally: the data agent claims `pipeline:sales_etl` before restructuring; the code agent sees the claim and steers clear of the dependent endpoint; a `question` about a column type gets answered on the line; `done: schema migration complete` releases the test agent to validate integration.

The technical enabler is already in place — both workspace-native and external agents speak MCP, so joining the line is a configuration change, not an integration project. As multi-agent workspaces become the default, this stops being a scenario and becomes the operating environment.

### 9.5 Identity & Audit Hardening

Re-enable on-behalf-of authorization to stamp authenticated human principals into the audit trail (the code is shipped and dormant). Then per-user database credential minting. Then, optionally, server-side protocol enforcement — rejecting writes to claimed paths from non-holders, which would move claims from cooperative to enforced.

### 9.6 Archive & Analytics Hardening

Swap the archive-then-prune job for managed CDC into Delta SCD2 history when that API ships — true off-database history that survives even project deletion. Add the `await_message` blocking long-poll tool for dedicated listener agents.

### 9.7 Interface Efficiency

Lower the loaded-context cost of the typed surface further, along the mitigations the platform now documents (§3.7–3.8). Mark rarely-used tools for deferred loading so only a search tool plus the hot tools load up front — Anthropic reports roughly an 85% token reduction and *higher* tool-selection accuracy from this "tool search" pattern (ref. 32). Package onboarding and triage guidance as an Agent Skill loaded on demand rather than a standing `instructions` blob. And enable tool-block prompt caching for long, sustained coordinated sessions where the cache actually stays warm (ref. 34).

---

## 10. Conclusion

Multi-agent software engineering is not coming; it is here, and coordination is its bottleneck. Six agents on one repository generate collisions daily. The frameworks that orchestrate agents inside a process cannot help agents that run as independent processes, and the channels humans coordinate through were never designed for participants who only act when spoken to.

Partyline's answer is small on purpose. Three primitives — awareness, exclusion, resumability — plus the piece prior art lacks: wake-up and awareness delivery for turn-based agents, honest about where a harness lets it push and where it must fall back to hooks. Structured message types, atomic claims, and checkpoint resume make parallelism safe at scale. Per-workspace isolation makes the whole thing distributable with no operational burden. The primitives are deliberately unoriginal; the contribution is the minimal composition for a new workload, and we have defended that claim against the strongest versions of the objection.

A telephone party line worked because everyone could hear everyone else. Nobody called that a privacy flaw. It was the point — and for fleets of software agents working the same codebase, it still is.

---

## Appendices

### A. MCP Tool Reference

The complete API surface: `register_agent`, `post_message`, `get_unread`, `get_since`, `ack_checkpoint`, `claim_resource`, `release_resource`, `list_agents`, `list_claims`, `search_messages`, `get_conversation` — schemas, arguments, and return shapes as implemented — plus the `onboard_me` and `check_my_messages` guided prompts.

### B. Schema DDL

Five tables — `agents`, `messages`, `messages_archive`, `checkpoints`, `claims` — with indexes and constraints, including the claim uniqueness key on `(repo, COALESCE(branch,''), resource_path)` that makes leases race-proof in a single statement.

### C. Deployment Guide

The one-command deploy: idempotent Lakebase provisioning, dashboard build, and bundle deployment; agent onboarding via `pip install` plus `.mcp.json`.

### D. Coordination Protocol Reference

Message types, claim semantics, and checkpoint behavior — including claim renewal (a holder re-claiming extends its TTL), message-id total ordering, at-least-once read/ack semantics (handlers must be idempotent), conversation threading (a `conversation_id` seeded on a root message and inherited by replies), full-text search semantics (`websearch_to_tsquery` over live and archived payloads, ranked by relevance then recency), and the agent-id naming convention (`owner_repo_instance`).

### E. Reference Implementation Architecture

The deployment diagram is reproduced verbatim from PRD §10 (single source of truth; note the source commit when copying). Body figures 1–3 argue the *idea*; this appendix documents the *instance* — the Databricks packaging, with shipped-versus-roadmap styling.

---

## References

### Protocols & Standards

1. **A2A Protocol.** "Agent2Agent: An open protocol enabling communication and interoperability between opaque agentic applications." Linux Foundation / Google, 2025. https://a2a-protocol.org / https://github.com/a2aproject/A2A
2. **MCP — Model Context Protocol.** "An open protocol for connecting AI models to external tools and data." Anthropic, 2024. https://modelcontextprotocol.io / https://github.com/modelcontextprotocol/specification
3. **JSON-RPC 2.0 Specification.** https://www.jsonrpc.org/specification

### Multi-Agent Frameworks

4. **CrewAI.** J. Moura et al. "Fast and Flexible Multi-Agent Automation Framework." crewAIInc, 2024. https://github.com/crewAIInc/crewAI
5. **AutoGen.** E. Wu, S. Chi et al. "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation." Microsoft Research, 2023. arXiv:2308.08155. https://github.com/microsoft/autogen
6. **Microsoft Agent Framework (MAF).** "Enterprise-ready successor to AutoGen." Microsoft, 2026. https://github.com/microsoft/agent-framework
7. **LangGraph.** N. Campos, W. Chase et al. "Low-level orchestration framework for building stateful agents." LangChain, 2024. https://github.com/langchain-ai/langgraph
8. **OpenAI Swarm.** I. Bigio, J. Hills et al. "Educational framework exploring ergonomic, lightweight multi-agent orchestration." OpenAI, 2024. https://github.com/openai/swarm
9. **OpenAI Agents SDK.** "Production-ready evolution of Swarm." OpenAI, 2025. https://github.com/openai/openai-agents-python
10. **Omnigent.** D. Lok, P. Sattara, S. Ruan et al. "The open-source meta-harness for all your AI agents." Databricks / omnigent-ai, 2026. https://github.com/omnigent-ai/omnigent

### AI Coding Agents

11. **SWE-agent.** J. Yang, C.E. Jimenez et al. "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering." Princeton/Stanford, 2024. arXiv:2405.15793. https://github.com/SWE-agent/SWE-agent
12. **Devin.** Cognition Labs. "The first AI software engineer." 2024. https://devin.ai
13. **Claude Code.** Anthropic. "Agentic coding tool in the terminal." 2025. https://docs.anthropic.com/en/docs/claude-code
14. **Genie Code.** P. Wendell, M. Zaharia, W. Hutchins, G. Oshri. "Introducing Genie Code: Your Autonomous AI Partner for Data Work." Databricks, March 2026. https://www.databricks.com/blog/introducing-genie-code

### Distributed Systems & Coordination

15. **Malewicz, G. et al.** "Pregel: A System for Large-Scale Graph Processing." SIGMOD 2010.
16. **Apache ZooKeeper.** P. Hunt et al. "ZooKeeper: Wait-free Coordination for Internet-scale Systems." USENIX ATC 2010. https://zookeeper.apache.org
17. **Redis / Redlock.** S. Sanfilippo. "Distributed Locks with Redis." https://redis.io/docs/manual/patterns/distributed-locks/
18. **Temporal.** "Durable execution for distributed applications." https://temporal.io
19. **etcd.** "A distributed, reliable key-value store." https://etcd.io
20. **Lamport, L.** "Time, Clocks, and the Ordering of Events in a Distributed System." CACM 1978.

### Databricks Platform

21. **Databricks Apps.** "Build and deploy apps on Databricks." https://docs.databricks.com/en/dev-tools/databricks-apps/
22. **Lakebase (Serverless Postgres).** "Managed PostgreSQL with scale-to-zero." Databricks, 2025. https://docs.databricks.com/en/lakebase/
23. **Native Lakehouse Sync (wal2delta).** "Continuous CDC replication from Postgres to Delta Lake." Databricks, 2025.
24. **Databricks Asset Bundles.** "Infrastructure as code for Databricks." https://docs.databricks.com/en/dev-tools/bundles/

### Conceptual Foundations

25. **Party Line (telephony).** Wikipedia. https://en.wikipedia.org/wiki/Party_line_(telephony)
26. **Kafka Consumer Groups.** J. Kreps et al. "Kafka: a Distributed Messaging System for Log Processing." NetDB 2011.
27. **Conway's Law.** M. Conway. "How Do Committees Invent?" Datamation, 1968.
28. **Erman, L.D., Hayes-Roth, F., Lesser, V.R., Reddy, D.R.** "The Hearsay-II Speech-Understanding System: Integrating Knowledge to Resolve Uncertainty." ACM Computing Surveys, 1980.
29. **Gelernter, D.** "Generative Communication in Linda." ACM TOPLAS 7(1), 1985.
30. **FIPA.** "FIPA ACL Message Structure Specification." Foundation for Intelligent Physical Agents, 2002. http://www.fipa.org/specs/fipa00061/

### Context Engineering & Tool-Use Efficiency

31. **Anthropic.** "Code execution with MCP: building more efficient AI agents." Anthropic Engineering, 2025. https://www.anthropic.com/engineering/code-execution-with-mcp
32. **Anthropic.** "Introducing advanced tool use on the Claude Developer Platform" (Tool Search Tool, programmatic tool calling). Anthropic Engineering, 2025. https://www.anthropic.com/engineering/advanced-tool-use
33. **Anthropic.** "Effective context engineering for AI agents." Anthropic Engineering, 2025. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
34. **Anthropic.** "Prompt caching." Claude platform documentation, 2025. https://platform.claude.com/docs/en/build-with-claude/prompt-caching
35. **Model Context Protocol.** "Lifecycle — `InitializeResult.instructions`." MCP Specification (2025-06-18). https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
36. **Anthropic.** "Writing effective tools for AI agents." Anthropic Engineering, 2025. https://www.anthropic.com/engineering/writing-tools-for-agents
