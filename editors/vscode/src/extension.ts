import * as vscode from "vscode";
import { PartylineClient, PartylineDiagnosticEvent, PartylineMessage } from "./client";
import { MessagesProvider, AgentsProvider, ClaimsProvider } from "./views";

let client: PartylineClient | undefined;
let statusBarItem: vscode.StatusBarItem;
let pendingMessages: PartylineMessage[] = [];
let diagnosticsChannel: vscode.OutputChannel;
let zeroAgentsWarningShown = false;

export function activate(context: vscode.ExtensionContext) {
  diagnosticsChannel = vscode.window.createOutputChannel("Partyline Diagnostics");
  context.subscriptions.push(diagnosticsChannel);

  // Status bar
  statusBarItem = vscode.window.createStatusBarItem(
    "partyline.status",
    vscode.StatusBarAlignment.Left,
    50
  );
  statusBarItem.command = "partyline.connect";
  statusBarItem.text = "$(megaphone) Partyline";
  statusBarItem.tooltip = "Click to connect";
  statusBarItem.show();
  context.subscriptions.push(statusBarItem);

  // Tree views
  const messagesProvider = new MessagesProvider();
  const agentsProvider = new AgentsProvider();
  const claimsProvider = new ClaimsProvider();

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("partyline.messages", messagesProvider),
    vscode.window.registerTreeDataProvider("partyline.agents", agentsProvider),
    vscode.window.registerTreeDataProvider("partyline.claims", claimsProvider)
  );

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("partyline.connect", () => {
      connect(context, messagesProvider, agentsProvider, claimsProvider);
    }),
    vscode.commands.registerCommand("partyline.disconnect", () => {
      disconnect();
    }),
    vscode.commands.registerCommand("partyline.markRead", () => {
      if (client) {
        client.markAllRead();
        updateStatusBar(0);
      }
    })
  );

  // Register MCP server for any LM in VS Code (Copilot, Claude, etc.)
  registerMcpServer(context);

  // Register LM tool so the model can check for messages proactively
  registerPartylineTool(context);

  // Register @partyline chat participant
  registerChatParticipant(context);

  // Auto-connect if configured
  const config = vscode.workspace.getConfiguration("partyline");
  if (config.get<string>("url") && config.get<string>("agentId")) {
    connect(context, messagesProvider, agentsProvider, claimsProvider);
  }
}

function connect(
  context: vscode.ExtensionContext,
  messagesProvider: MessagesProvider,
  agentsProvider: AgentsProvider,
  claimsProvider: ClaimsProvider
) {
  const config = vscode.workspace.getConfiguration("partyline");
  const url = config.get<string>("url");
  const agentId = config.get<string>("agentId");
  const token = config.get<string>("token") || process.env.PARTYLINE_TOKEN;

  if (!url || !agentId) {
    vscode.window.showWarningMessage(
      "Set partyline.url and partyline.agentId in settings to connect."
    );
    return;
  }

  disconnect(); // clean up previous

  client = new PartylineClient(url, agentId, token || "");

  client.onMessage((msg) => {
    messagesProvider.addMessage(msg);
    const isForMe =
      msg.recipient === agentId ||
      (msg.mentions && msg.mentions.includes(agentId));

    if (isForMe) {
      // Queue for LM tool consumption
      pendingMessages.push(msg);

      // Human notification
      vscode.window
        .showInformationMessage(
          `📬 ${msg.agent_id}: ${msg.payload?.slice(0, 100) || msg.message_type}`,
          "View",
          "Send to Chat"
        )
        .then((action) => {
          if (action === "View") {
            vscode.commands.executeCommand("partyline.messages.focus");
          } else if (action === "Send to Chat") {
            // Open chat with the message content pre-filled
            const prompt = `A Partyline message just arrived for you:\n\n` +
              `**From:** ${msg.agent_id}\n` +
              `**Type:** ${msg.message_type}\n` +
              (msg.repo ? `**Repo:** ${msg.repo}\n` : "") +
              (msg.resource_path ? `**Resource:** ${msg.resource_path}\n` : "") +
              `**Message:** ${msg.payload || "(no payload)"}\n\n` +
              `Please respond appropriately using Partyline tools (post_message, claim_resource, etc).`;
            vscode.commands.executeCommand("workbench.action.chat.open", { query: prompt });
          }
        });
    }
    updateStatusBar(client!.unreadCount);
  });

  client.onAgentsChanged((agents) => agentsProvider.update(agents));
  client.onClaimsChanged((claims) => claimsProvider.update(claims));
  client.onDiagnostic((event) => handleDiagnosticEvent(event));

  client.connect();
  updateStatusBar(0, true);
}

function disconnect() {
  if (client) {
    client.disconnect();
    client = undefined;
  }
  updateStatusBar(0, false);
}

function updateStatusBar(unread: number, connected?: boolean) {
  if (connected === false) {
    statusBarItem.text = "$(megaphone) Partyline";
    statusBarItem.tooltip = "Click to connect";
    statusBarItem.backgroundColor = undefined;
    return;
  }
  if (unread > 0) {
    statusBarItem.text = `$(megaphone) ${unread} unread`;
    statusBarItem.backgroundColor = new vscode.ThemeColor(
      "statusBarItem.warningBackground"
    );
  } else {
    statusBarItem.text = "$(megaphone) Partyline ✓";
    statusBarItem.backgroundColor = undefined;
  }
  statusBarItem.tooltip = "Connected — click to mark all read";
  statusBarItem.command = "partyline.markRead";
}

function registerMcpServer(context: vscode.ExtensionContext) {
  // Dynamically register Partyline as an MCP server for any LM chat participant
  const provider: vscode.McpServerDefinitionProvider<vscode.McpServerDefinition> = {
    provideMcpServerDefinitions(_token) {
      const config = vscode.workspace.getConfiguration("partyline");
      const url = config.get<string>("url");
      if (!url) { return []; }

      const token = config.get<string>("token") || process.env.PARTYLINE_TOKEN || "";
      const headers: Record<string, string> = {};
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }

      return [
        new vscode.McpHttpServerDefinition(
          "Covasant Partyline",
          vscode.Uri.parse(`${url}/mcp`),
          headers
        ),
      ];
    },
  };

  context.subscriptions.push(
    vscode.lm.registerMcpServerDefinitionProvider("covasant-partyline", provider)
  );
}

function registerPartylineTool(context: vscode.ExtensionContext) {
  // Register a local LM tool that surfaces pending messages directly to the model.
  // When the LM calls this tool, it gets all unread messages addressed to this agent.
  const tool: vscode.LanguageModelTool<object> = {
    async invoke(_options, _token) {
      const messages = pendingMessages.splice(0); // drain queue
      if (messages.length === 0) {
        return new vscode.LanguageModelToolResult([
          new vscode.LanguageModelTextPart("No new Partyline messages for you."),
        ]);
      }
      const formatted = messages
        .map(
          (m) =>
            `[${m.message_type}] from ${m.agent_id}` +
            (m.repo ? ` (repo: ${m.repo})` : "") +
            (m.resource_path ? ` (file: ${m.resource_path})` : "") +
            `: ${m.payload || "(no content)"}`
        )
        .join("\n");
      if (client) { client.markAllRead(); updateStatusBar(0); }
      return new vscode.LanguageModelToolResult([
        new vscode.LanguageModelTextPart(
          `${messages.length} new Partyline message(s) addressed to you:\n\n${formatted}`
        ),
      ]);
    },
  };

  context.subscriptions.push(
    vscode.lm.registerTool("partyline_check_messages", tool)
  );
}

function registerChatParticipant(context: vscode.ExtensionContext) {
  const participant = vscode.chat.createChatParticipant(
    "covasant.partyline",
    async (request, _context, response, _token) => {
      const config = vscode.workspace.getConfiguration("partyline");
      const agentId = config.get<string>("agentId") || "unknown";

      if (request.command === "status") {
        // Show current line status
        response.markdown(`**Partyline Status**\n\n`);
        response.markdown(`- Agent ID: \`${agentId}\`\n`);
        response.markdown(`- Connected: ${client ? "yes ✓" : "no"}\n`);
        response.markdown(`- Pending messages: ${pendingMessages.length}\n`);
        return;
      }

      // Default: show unread messages
      const messages = pendingMessages.splice(0);
      if (messages.length === 0) {
        response.markdown("No new messages on the Partyline addressed to you. The line is quiet.");
        return;
      }

      response.markdown(`**${messages.length} new Partyline message(s) for \`${agentId}\`:**\n\n`);
      for (const m of messages) {
        response.markdown(
          `- **${m.message_type}** from \`${m.agent_id}\`` +
          (m.repo ? ` (${m.repo}${m.branch ? "/" + m.branch : ""})` : "") +
          (m.resource_path ? ` 📁 \`${m.resource_path}\`` : "") +
          `\n  > ${m.payload || "(no content)"}\n\n`
        );
      }
      response.markdown(
        "\nUse Partyline MCP tools (`post_message`, `claim_resource`, etc.) to respond."
      );
      if (client) { client.markAllRead(); updateStatusBar(0); }
    }
  );

  participant.iconPath = new vscode.ThemeIcon("megaphone");
  participant.followupProvider = {
    provideFollowups(_result, _context, _token) {
      if (pendingMessages.length > 0) {
        return [{ prompt: "Check my Partyline messages", participant: "covasant.partyline" }];
      }
      return [
        { prompt: "What's the status?", command: "status", participant: "covasant.partyline" },
      ];
    },
  };

  context.subscriptions.push(participant);
}

export function deactivate() {
  disconnect();
}

function handleDiagnosticEvent(event: PartylineDiagnosticEvent) {
  diagnosticsChannel.appendLine(
    `[${event.timestamp}] [${event.kind}] ${event.message}`
  );
  if (event.details !== undefined) {
    try {
      diagnosticsChannel.appendLine(JSON.stringify(event.details, null, 2));
    } catch {
      diagnosticsChannel.appendLine(String(event.details));
    }
  }

  if (event.kind === "zero_agents" && !zeroAgentsWarningShown) {
    zeroAgentsWarningShown = true;
    vscode.window.showWarningMessage(
      "Partyline reports 0 active agents. Open 'Partyline Diagnostics' output for heartbeat diagnostics."
    );
  }

  if (event.kind === "agents_recovered") {
    zeroAgentsWarningShown = false;
  }
}
