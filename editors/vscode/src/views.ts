import * as vscode from "vscode";
import type { PartylineMessage, PartylineAgent, PartylineClaim } from "./client";

// --- Messages Tree ---

export class MessagesProvider implements vscode.TreeDataProvider<PartylineMessage> {
  private messages: PartylineMessage[] = [];
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  addMessage(msg: PartylineMessage) {
    this.messages.unshift(msg);
    if (this.messages.length > 200) { this.messages.length = 200; }
    this._onDidChange.fire();
  }

  getTreeItem(msg: PartylineMessage): vscode.TreeItem {
    const label = `${typeIcon(msg.message_type)} ${msg.agent_id}`;
    const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
    item.description = msg.payload?.slice(0, 80) || msg.message_type;
    item.tooltip = new vscode.MarkdownString(
      `**${msg.agent_id}** — \`${msg.message_type}\`\n\n` +
      (msg.payload || "") +
      (msg.resource_path ? `\n\n📁 ${msg.resource_path}` : "") +
      `\n\n_${msg.created_at}_`
    );
    item.contextValue = "message";
    return item;
  }

  getChildren(): PartylineMessage[] {
    return this.messages;
  }
}

// --- Agents Tree ---

export class AgentsProvider implements vscode.TreeDataProvider<PartylineAgent> {
  private agents: PartylineAgent[] = [];
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  update(agents: PartylineAgent[]) {
    this.agents = agents;
    this._onDidChange.fire();
  }

  getTreeItem(agent: PartylineAgent): vscode.TreeItem {
    const item = new vscode.TreeItem(
      agent.display_name || agent.agent_id,
      vscode.TreeItemCollapsibleState.None
    );

    // Online = heartbeat within the last 2 min (every partyline call refreshes it).
    const ageMs = Date.now() - new Date(agent.last_heartbeat).getTime();
    const online = isFinite(ageMs) && ageMs < 120_000;

    // Description shows the last status (the useful at-a-glance signal); fall back to the
    // repo when the agent hasn't posted a status yet.
    const repoLabel = `${agent.repo}${agent.branch ? "/" + agent.branch : ""}`;
    item.description = agent.last_status
      ? agent.last_status.replace(/\s+/g, " ").slice(0, 60)
      : repoLabel;

    const statusBlock = agent.last_status
      ? `\n\n**Last status**${agent.last_status_at ? ` · ${new Date(agent.last_status_at).toLocaleString()}` : ""}:\n\n${agent.last_status}`
      : "\n\n_No status posted yet._";
    item.tooltip = new vscode.MarkdownString(
      `**${agent.agent_id}** — ${online ? "🟢 online" : "⚪ offline"}\n\n` +
      `Repo: ${repoLabel}\n\nOwner: ${agent.owner}\n\n` +
      `Last seen: ${new Date(agent.last_heartbeat).toLocaleString()}` +
      statusBlock
    );

    // Green filled dot when online, hollow when not.
    item.iconPath = online
      ? new vscode.ThemeIcon("circle-filled", new vscode.ThemeColor("charts.green"))
      : new vscode.ThemeIcon("circle-outline");
    return item;
  }

  getChildren(): PartylineAgent[] {
    return this.agents;
  }
}

// --- Claims Tree ---

export class ClaimsProvider implements vscode.TreeDataProvider<PartylineClaim> {
  private claims: PartylineClaim[] = [];
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  update(claims: PartylineClaim[]) {
    this.claims = claims;
    this._onDidChange.fire();
  }

  getTreeItem(claim: PartylineClaim): vscode.TreeItem {
    const item = new vscode.TreeItem(
      claim.resource_path,
      vscode.TreeItemCollapsibleState.None
    );
    item.description = `held by ${claim.agent_id}`;
    item.tooltip = `Reason: ${claim.reason || "none"}\nExpires: ${claim.expires_at}`;
    item.iconPath = new vscode.ThemeIcon("lock");
    return item;
  }

  getChildren(): PartylineClaim[] {
    return this.claims;
  }
}

// --- Metrics Tree ---

interface MetricItem {
  label: string;
  value: string;
  icon: string;
}

export class MetricsProvider implements vscode.TreeDataProvider<MetricItem> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  private arrivals: number[] = [];  // timestamps of message arrivals
  private latencies: number[] = []; // ms from created_at to local receipt
  private connectedSince: number | undefined;
  private totalMessages = 0;
  private lastMessageAt: number | undefined;
  private agentCount = 0;
  private claimCount = 0;
  private refreshTimer: NodeJS.Timeout | undefined;

  start() {
    this.connectedSince = Date.now();
    // Refresh the view every 2 seconds for live counters
    this.refreshTimer = setInterval(() => this._onDidChange.fire(), 2000);
  }

  stop() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = undefined;
    }
    this.connectedSince = undefined;
    this._onDidChange.fire();
  }

  recordMessage(msg: { created_at: string }) {
    const now = Date.now();
    this.arrivals.push(now);
    this.totalMessages++;
    this.lastMessageAt = now;

    // Calculate SSE delivery latency
    const created = new Date(msg.created_at).getTime();
    if (!isNaN(created)) {
      const latency = now - created;
      if (latency >= 0 && latency < 60_000) { // ignore clock-skew outliers
        this.latencies.push(latency);
        if (this.latencies.length > 100) { this.latencies.shift(); }
      }
    }

    // Keep only last 5 minutes of arrivals for rate calculation
    const cutoff = now - 5 * 60_000;
    this.arrivals = this.arrivals.filter(t => t > cutoff);

    this._onDidChange.fire();
  }

  updateAgentCount(count: number) {
    this.agentCount = count;
    this._onDidChange.fire();
  }

  updateClaimCount(count: number) {
    this.claimCount = count;
    this._onDidChange.fire();
  }

  getTreeItem(item: MetricItem): vscode.TreeItem {
    const treeItem = new vscode.TreeItem(
      `${item.icon} ${item.label}`,
      vscode.TreeItemCollapsibleState.None
    );
    treeItem.description = item.value;
    return treeItem;
  }

  getChildren(): MetricItem[] {
    if (!this.connectedSince) {
      return [{ label: "Status", value: "Disconnected", icon: "⚫" }];
    }

    const now = Date.now();
    const uptimeSec = Math.floor((now - this.connectedSince) / 1000);
    const uptime = formatDuration(uptimeSec);

    // Messages per minute (over last 5 min window)
    const windowMs = Math.min(now - this.connectedSince, 5 * 60_000);
    const rate = windowMs > 0
      ? (this.arrivals.length / (windowMs / 60_000)).toFixed(1)
      : "0.0";

    // Average SSE latency
    const avgLatency = this.latencies.length > 0
      ? Math.round(this.latencies.reduce((a, b) => a + b, 0) / this.latencies.length)
      : null;

    // P95 latency
    const p95Latency = this.latencies.length >= 5
      ? Math.round(sorted(this.latencies)[Math.floor(this.latencies.length * 0.95)])
      : null;

    const lastAgo = this.lastMessageAt
      ? formatDuration(Math.floor((now - this.lastMessageAt) / 1000)) + " ago"
      : "—";

    const items: MetricItem[] = [
      { label: "Uptime", value: uptime, icon: "⏱️" },
      { label: "Total messages", value: String(this.totalMessages), icon: "📨" },
      { label: "Rate", value: `${rate} msg/min`, icon: "📈" },
      { label: "Last message", value: lastAgo, icon: "🕐" },
      { label: "Avg latency", value: avgLatency !== null ? `${avgLatency}ms` : "—", icon: "⚡" },
      { label: "P95 latency", value: p95Latency !== null ? `${p95Latency}ms` : "—", icon: "🔥" },
      { label: "Active agents", value: String(this.agentCount), icon: "👥" },
      { label: "Active claims", value: String(this.claimCount), icon: "🔒" },
    ];

    return items;
  }
}

function formatDuration(seconds: number): string {
  if (seconds < 60) { return `${seconds}s`; }
  if (seconds < 3600) { return `${Math.floor(seconds / 60)}m ${seconds % 60}s`; }
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function sorted(arr: number[]): number[] {
  return [...arr].sort((a, b) => a - b);
}

// --- Diagnostics Tree ---

interface RecentEvent {
  time: number;
  kind: string;
  message: string;
}

/**
 * Health/observability view: connection state, token status, responder activity,
 * last message, and a recent-events feed. This is the "is it actually working?" panel.
 */
export class DiagnosticsProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  private connection = "Disconnected";
  private lastMessageAt: number | undefined;
  private token = "—";
  private responder = "off";
  private agents = 0;
  private claims = 0;
  private errorCount = 0;
  private events: RecentEvent[] = [];
  private agentId = "—";

  constructor(private readonly version: string = "?") {}

  setAgentId(id: string) { this.agentId = id || "—"; this._onDidChange.fire(); }
  setConnection(state: string) { this.connection = state; this._onDidChange.fire(); }
  noteMessage() { this.lastMessageAt = Date.now(); this._onDidChange.fire(); }
  setToken(status: string) { this.token = status; this._onDidChange.fire(); }
  setResponder(status: string) { this.responder = status; this._onDidChange.fire(); }
  setCounts(agents: number, claims: number) { this.agents = agents; this.claims = claims; this._onDidChange.fire(); }

  pushEvent(kind: string, message: string) {
    this.events.unshift({ time: Date.now(), kind, message });
    if (this.events.length > 50) { this.events.length = 50; }
    if (/error|closed/i.test(kind)) { this.errorCount++; }
    this._onDidChange.fire();
  }

  reset() {
    this.connection = "Disconnected";
    this.token = "—";
    this.responder = "off";
    this.agents = 0;
    this.claims = 0;
    this._onDidChange.fire();
  }

  private connIcon(): vscode.ThemeIcon {
    if (/connected|✓/i.test(this.connection)) { return new vscode.ThemeIcon("pass"); }
    if (/error|disconnect/i.test(this.connection)) { return new vscode.ThemeIcon("error"); }
    return new vscode.ThemeIcon("sync");
  }

  /** Raw state for building a copyable diagnostic report. */
  snapshot() {
    return {
      connection: this.connection,
      lastMessageAt: this.lastMessageAt,
      token: this.token,
      responder: this.responder,
      agents: this.agents,
      claims: this.claims,
      errorCount: this.errorCount,
      events: this.events.slice(),
    };
  }

  getTreeItem(item: vscode.TreeItem): vscode.TreeItem { return item; }

  getChildren(): vscode.TreeItem[] {
    const now = Date.now();
    const row = (label: string, value: string, icon: string): vscode.TreeItem => {
      const t = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
      t.description = value;
      t.iconPath = new vscode.ThemeIcon(icon);
      return t;
    };

    const conn = new vscode.TreeItem("Connection", vscode.TreeItemCollapsibleState.None);
    conn.description = this.connection;
    conn.iconPath = this.connIcon();

    const lastMsg = this.lastMessageAt
      ? formatDuration(Math.floor((now - this.lastMessageAt) / 1000)) + " ago"
      : "—";

    // Agent ID — click to copy. This is the identity the human/other agents address.
    const agentIdItem = new vscode.TreeItem("Agent ID", vscode.TreeItemCollapsibleState.None);
    agentIdItem.description = this.agentId;
    agentIdItem.iconPath = new vscode.ThemeIcon("account");
    agentIdItem.contextValue = "agentId";
    if (this.agentId && this.agentId !== "—") {
      agentIdItem.command = {
        command: "partyline.copyAgentId",
        title: "Copy Agent ID",
        arguments: [this.agentId],
      };
      agentIdItem.tooltip = new vscode.MarkdownString(
        `\`${this.agentId}\`\n\n_Click to copy_`
      );
    } else {
      agentIdItem.tooltip = "No agent ID resolved yet";
    }

    const items: vscode.TreeItem[] = [
      agentIdItem,
      row("Version", `v${this.version}`, "tag"),
      conn,
      row("Last message", lastMsg, "comment"),
      row("Token", this.token, "key"),
      row("Responder", this.responder, "hubot"),
      row("Active agents", String(this.agents), "person"),
      row("Active claims", String(this.claims), "lock"),
      row("Errors (session)", String(this.errorCount), this.errorCount > 0 ? "warning" : "check"),
    ];

    if (this.events.length > 0) {
      const header = new vscode.TreeItem("Recent events", vscode.TreeItemCollapsibleState.None);
      header.description = `(last ${Math.min(this.events.length, 10)})`;
      items.push(header);
      for (const e of this.events.slice(0, 10)) {
        const it = new vscode.TreeItem(
          `${e.kind}: ${e.message}`,
          vscode.TreeItemCollapsibleState.None
        );
        it.description = formatDuration(Math.floor((now - e.time) / 1000)) + " ago";
        it.tooltip = new vscode.MarkdownString(`\`${e.kind}\`\n\n${e.message}`);
        it.iconPath = new vscode.ThemeIcon(/error|closed/i.test(e.kind) ? "circle-filled" : "circle-outline");
        items.push(it);
      }
    }

    return items;
  }
}

// --- Helpers ---

function typeIcon(type: string): string {
  switch (type) {
    case "status": return "📋";
    case "question": return "❓";
    case "claim": return "🔒";
    case "release": return "🔓";
    case "blocked": return "🚫";
    case "done": return "✅";
    case "escalation": return "🚨";
    case "summary": return "📝";
    default: return "💬";
  }
}
