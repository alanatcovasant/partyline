# Whitepaper Outline: Covasant Partyline
## Coordinated Multi-Agent Software Engineering at Scale

---

## 1. Abstract
- The problem: N autonomous AI coding agents operating concurrently on shared codebases without shared awareness
- The solution: A lightweight, durable, MCP-native coordination line with atomic mutual exclusion
- Key claim: structured coordination primitives (not chat) eliminate race conditions, enable epic-scale parallelism, and preserve full operational history

---

## 2. The Multi-Agent Coordination Problem

### 2.1 Why Multiple Agents?
- Complex software systems require parallel work across modules, services, and layers
- Single-agent throughput ceiling: context window limits, sequential execution, deep-focus tradeoffs
- Organizational reality: multiple developers × multiple agents = fleet coordination

### 2.2 What Goes Wrong Without Coordination
- Observed failure modes (from production Aura monorepo with 6 agents):
  - Git push races (two agents push within minutes, rejected, fetch/merge/retry)
  - Duplicate edits (two agents modify the same file unaware of each other)
  - Wasted cycles (agent works on already-completed task)
  - Silent conflicts (overlapping schema migrations, dependency version changes)
- The "Slack experiment": polling-based, ephemeral, no durable state, no mutual exclusion
  - Operational friction: OAuth app setup, rate limits, parsing prose for semantics

### 2.3 Requirements for Agent Coordination
- Near-real-time awareness (single-digit seconds)
- Durable, queryable, replayable history (not ephemeral pub/sub)
- Atomic mutual exclusion (not advisory, not "I said it first in chat")
- Resume from any downtime gap (hours, crashes, reboots)
- Vendor/agent-neutral interface
- Zero operational burden at idle

---

## 3. Covasant Partyline: Architecture & Design

### 3.1 Three Deliberate Separations
- **Coordination log** — append-only broadcast ("what's everyone doing")
- **Atomic claims** — TTL'd exclusive leases ("who owns this resource right now")
- **Checkpoints** — per-agent read cursors ("where did I leave off")
- Why these are three distinct primitives, not one overloaded channel

### 3.2 The Party Line Metaphor
- Historical telephony party line: shared channel, mutual awareness, no privacy
- Maps precisely: multiple subscribers, shared visibility, "hear what others are doing"
- Per-workspace install = your private line (not a public bus)

### 3.3 MCP as Primary Interface
- Model Context Protocol: vendor-neutral, any MCP-capable agent participates
- 9 tools: register, post, get_unread, get_since, ack, claim, release, list_agents, list_claims
- Streamable HTTP transport (no WebSocket complexity, no long-poll)
- Zero agent-side auth code (bridge handles OAuth transparently)

### 3.4 Infrastructure Choices
- Lakebase (serverless Postgres): only option satisfying atomic claims + push (LISTEN/NOTIFY) + scale-to-zero
- Native Lakehouse Sync (wal2delta CDC) for archive: no write-path code, retains full SCD2 history
- Per-workspace isolation: zero cross-tenant trust, self-provisioning
- Databricks App platform: managed compute, OAuth, service principal identity

---

## 4. Addressing Epic-Scale Efforts

### 4.1 From Tasks to Epics: The Coordination Challenge
- Individual tasks: single agent, single file, linear
- Epic-scale: 10-50 interconnected tasks across modules, services, and repos
- Dependencies between tasks create critical paths
- Without coordination: serial execution or chaotic parallelism

### 4.2 Structured Message Types as Workflow Primitives
- `status` — progress announcements (enables dependency tracking)
- `claim` / `release` — resource locks (enables safe parallelism)
- `blocked` — signal waiting state (enables dynamic re-prioritization)
- `done` — completion signal (enables downstream task unlock)
- `question` / `escalation` — request help or human intervention
- `summary` — periodic context compression for late-joining agents

### 4.3 Coordination Patterns for Complex Work

#### Pattern: Fan-Out / Fan-In
- Orchestrator posts epic breakdown → N agents claim subtasks → completion signals aggregate
- Checkpoint resume means partial failures don't lose progress

#### Pattern: Claim-Before-Edit
- Agent claims file/module before touching it
- TTL lease prevents stale claims from stalled agents
- Other agents see "claimed by X" and pick different work

#### Pattern: Replay-and-Orient
- New agent joins mid-epic → replays message history from checkpoint 0
- Builds full picture of what's done, what's claimed, what's blocked
- Self-assigns unclaimed remaining work

#### Pattern: Resource Pipeline
- Agent A claims and completes module X → posts `done` → Agent B (waiting on X) unblocks
- `reply_to_id` threading enables conversation context per-task

#### Pattern: Branch-Scoped Isolation
- Claims are keyed on (repo, branch, resource_path)
- Agents on different branches don't conflict
- Shared integration branches (main) can use broader claim semantics

### 4.4 The Compounding Advantage
- With N agents: linear throughput scaling when coordination overhead is O(1) per operation
- Without coordination: diminishing returns as N grows (conflict probability ~ N²)
- Partyline makes coordination cost constant: one claim check, one message post

---

## 5. Comparison to Alternatives

### 5.1 Chat-Based Coordination (Slack, Teams, Discord)
- Ephemeral: no durable cursor, no structured replay
- No mutual exclusion: "I'm working on X" is advisory, not atomic
- Rate limits, polling overhead, OAuth app complexity
- Requires NLP parsing to extract semantics from prose

### 5.2 Git-Based Coordination (branch protection, CODEOWNERS)
- Post-hoc: conflicts discovered at push/merge time, not prevented
- No awareness: agents don't know what others are doing until PR stage
- Branch locks are coarse-grained (whole branch, not file-level)

### 5.3 Queue/Task Systems (Celery, Airflow, Temporal)
- Designed for task distribution, not coordination awareness
- No shared visibility into peer state
- No mutual exclusion on arbitrary resources
- Heavyweight infrastructure for what's fundamentally a coordination signal

### 5.4 A2A Protocol (Google Agent2Agent)
- Designed for cross-organization, heterogeneous agent interop
- Heavier contract: agent cards, capability negotiation, task lifecycle
- Right for cross-org escalation; overkill for homogeneous team coordination
- Partyline could bridge to A2A for cross-org use cases (future, PRD §11)

### 5.5 Shared Databases / Distributed Locks (Redis, ZooKeeper)
- Raw primitives, not coordination-aware
- No message history, no checkpoint resume, no agent presence
- Operational burden: Redis clusters, ZK quorum management
- No scale-to-zero

---

## 6. Operational Model & Economics

### 6.1 Cost at Idle
- Lakebase: scale-to-zero when no agents active
- App: Databricks App managed compute (future: native scale-to-zero)
- Zero cost between work sessions (bursty usage pattern)

### 6.2 Per-Workspace Isolation
- Every install is independent: own database, own app, own history
- No centrally-hosted service, no cross-tenant data exposure
- Marketplace-distributable as an installable app listing

### 6.3 Self-Provisioning
- First-run: `CREATE TABLE IF NOT EXISTS` (idempotent schema bootstrap)
- No DBA, no infrastructure team, no manual setup
- Agent onboarding: `pip install covasant-partyline` + paste `.mcp.json` config

---

## 7. Real-World Validation: Dogfood Results

### 7.1 Environment
- 6 Claude Code instances against shared Aura monorepo
- Mix of backend, frontend, infrastructure, and documentation tasks
- Daily multi-hour work sessions with N agents active concurrently

### 7.2 Metrics (to be populated after dogfood)
- Push-race reduction (baseline: 2 per session → target: 0)
- Time-to-awareness of peer claims/status
- Epic completion throughput (tasks/hour with coordination vs. without)
- Claim contention rate (indicator of good task decomposition)

---

## 8. Future Directions

### 8.1 Cross-Organization Escalation
- Customer agent → structured `escalation` message → consent-gated bridge → vendor inbox
- Enables support workflows without exposing internal coordination channels

### 8.2 LLM-Powered Coordination Intelligence
- Gold layer: summarize coordination patterns, detect bottlenecks
- Auto-suggest task assignments based on agent history and capabilities
- Conflict prediction: "Agent B is likely to need this file based on task description"

### 8.3 Marketplace Distribution
- Per-workspace install model maps directly to Databricks Marketplace App listings
- Independent versioning, security review, support commitment as separate gated decisions

### 8.4 Multi-Repo Coordination
- Agents working across multiple repositories in a workspace
- Claims span repos (already supported: repo field in claim key)
- Cross-repo dependency tracking via message threading

---

## 9. Conclusion
- Multi-agent software engineering is production-ready today; coordination is the bottleneck
- Partyline provides the minimum necessary primitives: awareness, exclusion, resumability
- Structured types + atomic claims + checkpoint resume = safe parallelism at scale
- Per-workspace isolation makes it distributable without operational burden
- The party line metaphor: shared visibility is the feature, not a bug

---

## Appendices

### A. MCP Tool Reference (complete API surface)
### B. Schema DDL (4 tables, indexes, constraints)
### C. Deployment Guide (bundle + app deploy steps)
### D. Coordination Protocol Reference (message types, claim semantics, checkpoint behavior)
### E. Architecture Diagram (Mermaid)
