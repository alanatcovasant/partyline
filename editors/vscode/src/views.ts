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
    item.description = `${agent.repo}${agent.branch ? "/" + agent.branch : ""}`;
    item.tooltip = `Owner: ${agent.owner}\nLast seen: ${agent.last_heartbeat}`;
    item.iconPath = new vscode.ThemeIcon("person");
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
