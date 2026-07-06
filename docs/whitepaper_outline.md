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

## 5. Literature Review & Related Work

### 5.1 Multi-Agent Orchestration Frameworks

#### CrewAI (crewAIInc, 2024–present)
- **What:** Open-source Python framework (55k stars) for orchestrating role-based autonomous AI agents
- **Approach:** Crews (agent teams) + Flows (event-driven workflows); sequential/hierarchical process execution
- **Key difference from Partyline:** CrewAI orchestrates agents *within a single process* — it assigns roles, delegates tasks, and controls execution flow. It's an *orchestrator*, not a *coordination channel*. Agents don't independently decide what to work on; the Crew definition predetermines their collaboration pattern. No mutual exclusion on shared resources, no durable state across sessions, no checkpoint-based resume.
- **Overlap:** Both address multi-agent coordination; CrewAI agents could *use* Partyline as a coordination backend for cross-Crew awareness.
- **Ref:** https://github.com/crewAIInc/crewAI, https://docs.crewai.com

#### Microsoft AutoGen / Agent Framework (Microsoft Research, 2023–2026)
- **What:** Programming framework (59.5k stars) for multi-agent AI applications; now in maintenance mode, succeeded by Microsoft Agent Framework (MAF)
- **Approach:** Message-passing, event-driven agents with local and distributed runtime; AgentChat API for group chats and delegation; supports MCP and A2A
- **Key difference:** AutoGen focuses on conversational multi-agent patterns (group chat, delegation, tool use) within a managed runtime. Agents are co-located or connected via gRPC. No durable coordination log, no atomic resource claims, no per-agent checkpoint. State is in-memory or runtime-managed, not independently queryable.
- **Relevance:** MAF's enterprise-grade multi-agent orchestration validates the market need; Partyline solves the *inter-instance* coordination problem that runtime-coupled frameworks can't address when agents run as independent processes.
- **Ref:** https://github.com/microsoft/autogen, https://github.com/microsoft/agent-framework

#### LangGraph (LangChain, 2024–present)
- **What:** Low-level orchestration framework (36.6k stars) for stateful, long-running agents; inspired by Pregel/Apache Beam
- **Approach:** Graph-based workflow definition with durable execution, checkpointing, human-in-the-loop, and memory persistence
- **Key difference:** LangGraph provides durability and state *within a single agent's execution graph*, not *across independent agent instances*. Its checkpointing is for resuming a workflow, not for coordinating peer agents. Multi-agent patterns (supervisor, handoff) are within one graph/process, not across independent processes.
- **Overlap:** LangGraph's "durable execution" concept is conceptually adjacent to Partyline's checkpoint resume, but applied at different levels (intra-agent vs. inter-agent).
- **Ref:** https://github.com/langchain-ai/langgraph, https://docs.langchain.com/oss/python/langgraph/

#### OpenAI Swarm (OpenAI, 2024)
- **What:** Educational/experimental framework (21.8k stars) exploring lightweight multi-agent orchestration via handoffs; now superseded by OpenAI Agents SDK
- **Approach:** Two primitives: Agents (instructions + functions) and handoffs (conversation transfer between agents). Stateless between calls (like Chat Completions API).
- **Key difference:** Swarm is explicitly stateless and single-process; it models sequential conversation handoff, not parallel coordination. No shared awareness, no resource claims, no durability. Educational, not production-targeted.
- **Ref:** https://github.com/openai/swarm, https://github.com/openai/openai-agents-python

#### Omnigent (Databricks / omnigent-ai, 2026)
- **What:** Open-source meta-harness (6.4k stars, Apache 2.0) that provides a common orchestration layer over Claude Code, Codex, Cursor, Hermes, Pi, and custom YAML-defined agents. Enables swapping/combining harnesses, enforcing policies and sandboxing, real-time collaboration across devices.
- **Approach:** Supervisory architecture — a "Polly" orchestrator decomposes work, delegates to coding sub-agents in parallel git worktrees, routes diffs to cross-vendor reviewers. Supports managed sandboxes (Modal, Databricks, E2B, K8s), policies (spend caps, tool restrictions, approval gates), and multi-user collaboration (share/co-drive/fork sessions).
- **Key difference from Partyline:** Omnigent is a *control plane* — it owns the agents' lifecycle, decides what they work on, routes their output, and enforces governance. Partyline is a *coordination plane* — agents remain independently running processes that self-direct, but share a durable awareness channel for mutual exclusion and status. Omnigent agents are subordinate to the harness; Partyline agents are peers on a shared line.
- **Complementarity:** An Omnigent deployment could use Partyline as the durable coordination backend between its sub-agents — particularly for resource claims across parallel worktrees and checkpoint-based resume after sandbox restarts. Conversely, Partyline-coordinated agents might each be individually managed by Omnigent for policy enforcement.
- **Overlap:** Both address multi-agent coding at scale. Omnigent's "Polly" pattern (plan → delegate → review) could be implemented as a Partyline coordination protocol (post plan → agents claim subtasks → post done → reviewer agent picks up).
- **Ref:** https://github.com/omnigent-ai/omnigent, https://omnigent.ai, https://www.databricks.com/blog/introducing-omnigent-meta-harness-combine-control-and-share-your-agents

### 5.2 Agent Communication Protocols

#### A2A — Agent2Agent Protocol (Google/Linux Foundation, 2025–present)
- **What:** Open protocol (24.7k stars) for communication between opaque agentic applications; under Linux Foundation governance
- **Approach:** JSON-RPC 2.0 over HTTPS; Agent Cards for discovery; supports sync, streaming (SSE), and async push; rich data exchange (text, files, JSON); enterprise security/auth
- **Key difference:** A2A solves *cross-organization*, *heterogeneous* agent interop — different vendors, different frameworks, different trust domains. It's a bilateral protocol (client↔server), not a shared broadcast channel. No shared durable state, no resource claims, no per-agent checkpoint. A2A agents negotiate capabilities; Partyline agents share a coordination log.
- **Complementarity:** A2A and Partyline solve adjacent problems. A2A enables the cross-org escalation bridge (PRD §11) that Partyline's architecture deliberately enables but doesn't implement in v1.
- **Ref:** https://github.com/a2aproject/A2A, https://a2a-protocol.org

#### MCP — Model Context Protocol (Anthropic, 2024–present)
- **What:** Open protocol for LLM applications to access external tools and data; standardizes tool invocation
- **Approach:** Client/server architecture; JSON-RPC over stdio, HTTP, or SSE; tools, resources, prompts as primitives
- **Key difference:** MCP is a *tool access* protocol, not a *coordination* protocol. It defines how an agent calls tools, not how agents coordinate with each other. Partyline is *implemented as* an MCP server (agents access coordination via MCP tools), but MCP itself provides no coordination semantics.
- **Relationship:** Partyline's primary interface *is* MCP — making it accessible to any MCP-capable agent without custom SDK integration. MCP is the transport; Partyline is the coordination logic.
- **Ref:** https://modelcontextprotocol.io, https://github.com/modelcontextprotocol/specification

### 5.3 AI Coding Agents (Single-Instance)

#### SWE-agent (Princeton/Stanford, 2024–present)
- **What:** Autonomous coding agent (19.7k stars) that takes GitHub issues and fixes them; state-of-the-art on SWE-bench
- **Approach:** Single agent, single issue, tool-based interaction with code/terminal; free-flowing LM agency
- **Key difference:** Purely single-agent, single-task. No coordination with other instances. When you want N SWE-agents working in parallel, there's no built-in mechanism to prevent conflicts.
- **Relevance:** Represents the "unit of work" that Partyline coordinates at scale. Multiple SWE-agent instances could use Partyline for mutual exclusion.
- **Ref:** https://github.com/SWE-agent/SWE-agent, https://swe-agent.com

#### Claude Code / Cursor / Codex (various, 2024–2026)
- **What:** IDE-integrated AI coding assistants with agentic capabilities (file editing, terminal, multi-step reasoning)
- **Approach:** Single-agent operation within one developer's workspace; no native inter-instance awareness
- **Key difference:** These are the *agents* that need coordination, not the coordination mechanism itself. When one developer runs 6 Claude Code instances, nothing native prevents them from colliding.
- **Relevance:** Partyline's primary dogfood environment is 6 Claude Code instances coordinating via MCP tools.

### 5.4 Distributed Coordination Primitives

#### Apache ZooKeeper / etcd / Consul
- **What:** Distributed coordination services providing consensus, leader election, distributed locks, configuration management
- **Approach:** CP systems (consistency over availability); require cluster quorum; provide raw lock/lease primitives
- **Key difference:** General-purpose distributed coordination, not agent-aware. No message history, no checkpoint resume, no structured message types, no presence semantics. Heavy operational burden (cluster management). Not scale-to-zero.
- **Ref:** https://zookeeper.apache.org, https://etcd.io, https://www.consul.io

#### Redis / Redlock
- **What:** In-memory data store with distributed lock capabilities via Redlock algorithm
- **Key difference:** Ephemeral (memory-only unless configured), no message log, no per-agent state, no queryable history. Operational overhead of Redis cluster. No native scale-to-zero.

#### Temporal / Durable Execution Engines
- **What:** Workflow orchestration platform with durable execution, retries, saga patterns
- **Key difference:** Designed for workflow orchestration (sequences of activities with durability), not peer-to-peer agent coordination. Temporal schedules and retries tasks; Partyline gives agents shared awareness to self-coordinate.
- **Ref:** https://temporal.io

### 5.5 Positioning Summary

| System | Scope | Agent Model | Durability | Mutual Exclusion | Scale-to-Zero |
|--------|-------|-------------|------------|------------------|---------------|
| **Partyline** | Intra-workspace, homogeneous | Independent processes, self-directed | ✅ Append-only + checkpoint | ✅ Atomic TTL'd claims | ✅ Lakebase |
| CrewAI | Intra-process | Orchestrated roles | ❌ Per-run only | ❌ | N/A (library) |
| AutoGen/MAF | Intra-runtime | Group chat, delegation | ❌ Runtime-only | ❌ | ❌ |
| LangGraph | Intra-graph | Graph nodes | ✅ Workflow checkpoint | ❌ | ❌ |
| Omnigent | Multi-device, supervised | Harness-managed sub-agents | ⚠️ Session-scoped | ❌ (worktree isolation) | ❌ |
| A2A | Cross-org, bilateral | Opaque, capability-based | ❌ Task-scoped | ❌ | N/A (protocol) |
| Swarm | Intra-call | Handoff chain | ❌ Stateless | ❌ | N/A (library) |
| ZooKeeper | Infrastructure | N/A | ✅ | ✅ (raw locks) | ❌ |
| Temporal | Workflow | Activity workers | ✅ | ✅ (workflow locks) | ❌ |

---

## 6. Comparison to Alternatives (Condensed)

### 6.1 Chat-Based Coordination (Slack, Teams, Discord)
- Ephemeral: no durable cursor, no structured replay
- No mutual exclusion: "I'm working on X" is advisory, not atomic
- Rate limits, polling overhead, OAuth app complexity
- Requires NLP parsing to extract semantics from prose

### 6.2 Git-Based Coordination (branch protection, CODEOWNERS)
- Post-hoc: conflicts discovered at push/merge time, not prevented
- No awareness: agents don't know what others are doing until PR stage
- Branch locks are coarse-grained (whole branch, not file-level)

### 6.3 Queue/Task Systems (Celery, Airflow, Temporal)
- Designed for task distribution, not coordination awareness
- No shared visibility into peer state
- No mutual exclusion on arbitrary resources
- Heavyweight infrastructure for what's fundamentally a coordination signal

### 6.4 A2A Protocol (Google Agent2Agent)
- Designed for cross-organization, heterogeneous agent interop
- Heavier contract: agent cards, capability negotiation, task lifecycle
- Right for cross-org escalation; overkill for homogeneous team coordination
- Partyline could bridge to A2A for cross-org use cases (future, PRD §11)

### 6.5 Shared Databases / Distributed Locks (Redis, ZooKeeper)
- Raw primitives, not coordination-aware
- No message history, no checkpoint resume, no agent presence
- Operational burden: Redis clusters, ZK quorum management
- No scale-to-zero

---

## 7. Operational Model & Economics

### 7.1 Cost at Idle
- Lakebase: scale-to-zero when no agents active
- App: Databricks App managed compute (future: native scale-to-zero)
- Zero cost between work sessions (bursty usage pattern)

### 7.2 Per-Workspace Isolation
- Every install is independent: own database, own app, own history
- No centrally-hosted service, no cross-tenant data exposure
- Marketplace-distributable as an installable app listing

### 7.3 Self-Provisioning
- First-run: `CREATE TABLE IF NOT EXISTS` (idempotent schema bootstrap)
- No DBA, no infrastructure team, no manual setup
- Agent onboarding: `pip install covasant-partyline` + paste `.mcp.json` config

---

## 8. Real-World Validation: Dogfood Results

### 8.1 Environment
- 6 Claude Code instances against shared Aura monorepo
- Mix of backend, frontend, infrastructure, and documentation tasks
- Daily multi-hour work sessions with N agents active concurrently

### 8.2 Metrics (to be populated after dogfood)
- Push-race reduction (baseline: 2 per session → target: 0)
- Time-to-awareness of peer claims/status
- Epic completion throughput (tasks/hour with coordination vs. without)
- Claim contention rate (indicator of good task decomposition)

---

## 9. Future Directions

### 9.1 Cross-Organization Escalation
- Customer agent → structured `escalation` message → consent-gated bridge → vendor inbox
- Enables support workflows without exposing internal coordination channels

### 9.2 LLM-Powered Coordination Intelligence
- Gold layer: summarize coordination patterns, detect bottlenecks
- Auto-suggest task assignments based on agent history and capabilities
- Conflict prediction: "Agent B is likely to need this file based on task description"

### 9.3 Marketplace Distribution
- Per-workspace install model maps directly to Databricks Marketplace App listings
- Independent versioning, security review, support commitment as separate gated decisions

### 9.4 Multi-Repo Coordination
- Agents working across multiple repositories in a workspace
- Claims span repos (already supported: repo field in claim key)
- Cross-repo dependency tracking via message threading

---

## 10. Conclusion
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

---

## References

### Protocols & Standards
1. **A2A Protocol.** "Agent2Agent: An open protocol enabling communication and interoperability between opaque agentic applications." Linux Foundation / Google, 2025. https://a2a-protocol.org / https://github.com/a2aproject/A2A
2. **MCP — Model Context Protocol.** "An open protocol for connecting AI models to external tools and data." Anthropic, 2024. https://modelcontextprotocol.io / https://github.com/modelcontextprotocol/specification
3. **JSON-RPC 2.0 Specification.** https://www.jsonrpc.org/specification

### Multi-Agent Frameworks
4. **CrewAI.** J. Moura et al. "Fast and Flexible Multi-Agent Automation Framework." crewAIInc, 2024. https://github.com/crewAIInc/crewAI (55k stars)
5. **AutoGen.** E. Wu, S. Chi et al. "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation." Microsoft Research, 2023. arXiv:2308.08155. https://github.com/microsoft/autogen (59.5k stars)
6. **Microsoft Agent Framework (MAF).** "Enterprise-ready successor to AutoGen." Microsoft, 2026. https://github.com/microsoft/agent-framework
7. **LangGraph.** N. Campos, W. Chase et al. "Low-level orchestration framework for building stateful agents." LangChain, 2024. https://github.com/langchain-ai/langgraph (36.6k stars)
8. **OpenAI Swarm.** I. Bigio, J. Hills et al. "Educational framework exploring ergonomic, lightweight multi-agent orchestration." OpenAI, 2024. https://github.com/openai/swarm (21.8k stars)
9. **OpenAI Agents SDK.** "Production-ready evolution of Swarm." OpenAI, 2025. https://github.com/openai/openai-agents-python
10. **Omnigent.** D. Lok, P. Sattara, S. Ruan et al. "The open-source meta-harness for all your AI agents." Databricks / omnigent-ai, 2026. https://github.com/omnigent-ai/omnigent (6.4k stars, Apache 2.0). Orchestrates Claude Code, Codex, Cursor, Hermes, Pi; supports policies, sandboxing, real-time collaboration, parallel worktrees.

### AI Coding Agents
10. **SWE-agent.** J. Yang, C.E. Jimenez et al. "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering." Princeton/Stanford, 2024. arXiv:2405.15793. https://github.com/SWE-agent/SWE-agent (19.7k stars)
11. **Devin.** Cognition Labs. "The first AI software engineer." 2024. https://devin.ai
12. **Claude Code.** Anthropic. "Agentic coding tool in the terminal." 2025. https://docs.anthropic.com/en/docs/claude-code

### Distributed Systems & Coordination
13. **Malewicz, G. et al.** "Pregel: A System for Large-Scale Graph Processing." SIGMOD 2010. (Inspiration for LangGraph's graph execution model)
14. **Apache ZooKeeper.** P. Hunt et al. "ZooKeeper: Wait-free Coordination for Internet-scale Systems." USENIX ATC 2010. https://zookeeper.apache.org
15. **Redis / Redlock.** S. Sanfilippo. "Distributed Locks with Redis." https://redis.io/docs/manual/patterns/distributed-locks/
16. **Temporal.** "Durable execution for distributed applications." https://temporal.io
17. **etcd.** "A distributed, reliable key-value store." https://etcd.io
18. **Lamport, L.** "Time, Clocks, and the Ordering of Events in a Distributed System." CACM 1978. (Foundational work on event ordering in distributed systems; relevant to checkpoint/cursor design)

### Databricks Platform
19. **Databricks Apps.** "Build and deploy apps on Databricks." https://docs.databricks.com/en/dev-tools/databricks-apps/
20. **Lakebase (Serverless Postgres).** "Managed PostgreSQL with scale-to-zero." Databricks, 2025. https://docs.databricks.com/en/lakebase/
21. **Native Lakehouse Sync (wal2delta).** "Continuous CDC replication from Postgres to Delta Lake." Databricks, 2025.
22. **Databricks Asset Bundles.** "Infrastructure as code for Databricks." https://docs.databricks.com/en/dev-tools/bundles/

### Conceptual Foundations
23. **Party Line (telephony).** Wikipedia. "A party line is a local loop telephone circuit shared by multiple subscribers." https://en.wikipedia.org/wiki/Party_line_(telephony) — the metaphorical basis for the system's shared-visibility, no-privacy design.
24. **Kafka Consumer Groups.** J. Kreps et al. "Kafka: a Distributed Messaging System for Log Processing." NetDB 2011. (Checkpoint/cursor model inspiration)
25. **Conway's Law.** M. Conway. "How Do Committees Invent?" Datamation, 1968. (Multi-agent coordination reflects organizational structure; relevant to why per-workspace isolation matters)
