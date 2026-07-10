import * as vscode from "vscode";
import { execFile } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { PartylineClient, PartylineDiagnosticEvent, PartylineMessage } from "./client";
import { MessagesProvider, AgentsProvider, ClaimsProvider, MetricsProvider, DiagnosticsProvider } from "./views";
import { PartylineResponder } from "./responder";

let client: PartylineClient | undefined;
let responder: PartylineResponder | undefined;
let diagnosticsProvider: DiagnosticsProvider;
let extVersion = "?";
let statusBarItem: vscode.StatusBarItem;
let pendingMessages: PartylineMessage[] = [];
let diagnosticsChannel: vscode.OutputChannel;
let zeroAgentsWarningShown = false;
let tokenRefreshTimer: NodeJS.Timeout | undefined;
let activeMetricsProvider: MetricsProvider | undefined;
let currentAgentId = "";

/** Resolved partyline config with precedence: env → .partyline/config.json → settings.json → derived */
interface PartylineConfig {
  url: string;
  agentId: string;
  databricksProfile: string;
  token: string;
  /** Where agentId was resolved from (for diagnostics) */
  agentIdSource: string;
}

/**
 * Resolve partyline config with precedence:
 *   1. Environment variables (PARTYLINE_AGENT_ID, PARTYLINE_URL)
 *   2. .partyline/config.json in workspace root (per-clone, untracked)
 *   3. VSCode settings.json (partyline.* — legacy/fallback)
 *   4. Derived defaults (folder name + instance)
 */
function resolveConfig(): PartylineConfig {
  const vsConfig = vscode.workspace.getConfiguration("partyline");

  // --- Read .partyline/config.json if it exists ---
  let fileConfig: { url?: string; agentId?: string; databricksProfile?: string } = {};
  const workspaceFolders = vscode.workspace.workspaceFolders;
  if (workspaceFolders) {
    for (const folder of workspaceFolders) {
      const configPath = path.join(folder.uri.fsPath, ".partyline", "config.json");
      try {
        const raw = fs.readFileSync(configPath, "utf-8");
        fileConfig = JSON.parse(raw);
        break; // use first found
      } catch {
        // not present in this folder — try next
      }
    }
  }

  // --- Derive a last-resort agentId from workspace folder name ---
  const derivedAgentId = workspaceFolders?.[0]
    ? path.basename(workspaceFolders[0].uri.fsPath).replace(/[^a-zA-Z0-9_-]/g, "_") + "_1"
    : "";

  // --- Resolve with precedence ---
  let agentId: string;
  let agentIdSource: string;
  if (process.env.PARTYLINE_AGENT_ID) {
    agentId = process.env.PARTYLINE_AGENT_ID;
    agentIdSource = "env (PARTYLINE_AGENT_ID)";
  } else if (fileConfig.agentId) {
    agentId = fileConfig.agentId;
    agentIdSource = ".partyline/config.json";
  } else if (vsConfig.get<string>("agentId")) {
    agentId = vsConfig.get<string>("agentId")!;
    agentIdSource = "settings.json (legacy)";
  } else {
    agentId = derivedAgentId;
    agentIdSource = "derived from folder name";
  }

  const url = process.env.PARTYLINE_URL
    || fileConfig.url
    || vsConfig.get<string>("url")
    || "";

  const databricksProfile = fileConfig.databricksProfile
    || vsConfig.get<string>("databricksProfile")
    || "dev-eastus2";

  const token = vsConfig.get<string>("token") || process.env.PARTYLINE_TOKEN || "";

  return { url, agentId, databricksProfile, token, agentIdSource };
}

/**
 * Attempt to get a Databricks auth token via CLI.
 * Falls back to empty string if CLI is unavailable.
 */
async function getDatabricksToken(profile?: string): Promise<string> {
  return new Promise((resolve) => {
    const args = ["auth", "token"];
    if (profile) { args.push("-p", profile); }
    execFile("databricks", args, { timeout: 15_000 }, (err, stdout) => {
      if (err) {
        diagnosticsChannel.appendLine(
          `[${new Date().toISOString()}] [token] databricks auth token failed: ${err.message}`
        );
        resolve("");
        return;
      }
      try {
        const parsed = JSON.parse(stdout);
        resolve(parsed.access_token || "");
      } catch {
        diagnosticsChannel.appendLine(
          `[${new Date().toISOString()}] [token] Failed to parse databricks CLI output`
        );
        resolve("");
      }
    });
  });
}

export function activate(context: vscode.ExtensionContext) {
  extVersion = (context.extension?.packageJSON?.version as string) || "?";
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
  const metricsProvider = new MetricsProvider();
  diagnosticsProvider = new DiagnosticsProvider(extVersion);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("partyline.messages", messagesProvider),
    vscode.window.registerTreeDataProvider("partyline.agents", agentsProvider),
    vscode.window.registerTreeDataProvider("partyline.claims", claimsProvider),
    vscode.window.registerTreeDataProvider("partyline.metrics", metricsProvider),
    vscode.window.registerTreeDataProvider("partyline.diagnostics", diagnosticsProvider)
  );

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("partyline.connect", () => {
      connect(context, messagesProvider, agentsProvider, claimsProvider, metricsProvider);
    }),
    vscode.commands.registerCommand("partyline.disconnect", () => {
      disconnect();
    }),
    vscode.commands.registerCommand("partyline.markRead", () => {
      if (client) {
        client.markAllRead();
        updateStatusBar(0);
      }
    }),
    vscode.commands.registerCommand("partyline.copyDiagnostics", () => copyDiagnostics()),
    vscode.commands.registerCommand("partyline.copyAgentId", async (id?: string) => {
      const agentId = id || resolveConfig().agentId;
      if (!agentId) {
        vscode.window.showWarningMessage("Partyline: no agent ID resolved yet.");
        return;
      }
      await vscode.env.clipboard.writeText(agentId);
      vscode.window.showInformationMessage(`Copied agent ID: ${agentId}`);
    })
  );

  // Register MCP server for any LM in VS Code (Copilot, Claude, etc.)
  registerMcpServer(context);

  // Register LM tool so the model can check for messages proactively
  registerPartylineTool(context);

  // Register @partyline chat participant
  registerChatParticipant(context);

  // Auto-connect if configured
  const resolved = resolveConfig();
  currentAgentId = resolved.agentId;
  diagnosticsProvider.setAgentId(resolved.agentId);
  if (resolved.url && resolved.agentId) {
    connect(context, messagesProvider, agentsProvider, claimsProvider, metricsProvider);
  }
}

async function connect(
  context: vscode.ExtensionContext,
  messagesProvider: MessagesProvider,
  agentsProvider: AgentsProvider,
  claimsProvider: ClaimsProvider,
  metricsProvider: MetricsProvider
) {
  const config = vscode.workspace.getConfiguration("partyline");
  const resolved = resolveConfig();
  const url = resolved.url;
  const agentId = resolved.agentId;
  const profile = resolved.databricksProfile;
  let token = resolved.token;

  if (!url || !agentId) {
    vscode.window.showWarningMessage(
      "Set agentId and url in .partyline/config.json, env vars, or partyline.* settings to connect."
    );
    return;
  }

  currentAgentId = agentId;
  diagnosticsProvider.setAgentId(agentId);
  diagnosticsChannel.appendLine(
    `[${new Date().toISOString()}] [config] agentId="${agentId}" (source: ${resolved.agentIdSource}), url="${url}"`
  );

  // Auto-acquire token from Databricks CLI if none configured
  if (!token) {
    diagnosticsChannel.appendLine(
      `[${new Date().toISOString()}] [token] No token configured, attempting databricks auth token -p ${profile}`
    );
    token = await getDatabricksToken(profile);
    if (!token) {
      vscode.window.showWarningMessage(
        "Partyline: No token available. Set partyline.token, PARTYLINE_TOKEN env, or ensure `databricks` CLI is on PATH."
      );
      return;
    }
    diagnosticsChannel.appendLine(
      `[${new Date().toISOString()}] [token] Acquired token from Databricks CLI (profile: ${profile})`
    );
    diagnosticsProvider.setToken(`Databricks CLI (${profile})`);
  } else if (config.get<string>("token")) {
    diagnosticsProvider.setToken("workspace setting");
  } else {
    diagnosticsProvider.setToken("PARTYLINE_TOKEN env");
  }

  disconnect(); // clean up previous

  diagnosticsProvider.setConnection("Connecting…");
  diagnosticsProvider.setResponder(
    config.get<boolean>("autoRespond", false) ? "on · idle" : "off"
  );

  client = new PartylineClient(url, agentId, token);
  responder = new PartylineResponder(client, agentId, (m) => {
    diagnosticsChannel.appendLine(`[${new Date().toISOString()}] ${m}`);
    diagnosticsProvider.pushEvent("responder", m.replace(/^\[responder\]\s*/, ""));
    diagnosticsProvider.setResponder(`on · ${m.replace(/^\[responder\]\s*/, "").slice(0, 60)}`);
  });

  // Schedule token refresh every 45 minutes (tokens expire ~1hr)
  tokenRefreshTimer = setInterval(async () => {
    const freshToken = await getDatabricksToken(profile);
    if (freshToken && client) {
      client.updateToken(freshToken);
      diagnosticsChannel.appendLine(
        `[${new Date().toISOString()}] [token] Refreshed token from Databricks CLI`
      );
      diagnosticsProvider.setToken(`Databricks CLI (${profile}) · refreshed`);
      diagnosticsProvider.pushEvent("token", "refreshed from Databricks CLI");
    }
  }, 45 * 60 * 1000);

  client.onMessage((msg) => {
    messagesProvider.addMessage(msg);
    metricsProvider.recordMessage(msg);
    diagnosticsProvider.noteMessage();

    // Autonomous coordination responder (opt-in). Wakes the extension's own LM agent
    // for messages addressed to us — the one path that doesn't need an active chat
    // session. The responder itself gates on recipient/loop-safety.
    if (vscode.workspace.getConfiguration("partyline").get<boolean>("autoRespond", false)) {
      void responder?.handle(msg);
    }

    const isForMe =
      msg.recipient === agentId ||
      (msg.mentions && msg.mentions.includes(agentId));
    const isBroadcast = !msg.recipient && (!msg.mentions || msg.mentions.length === 0);
    const notifyAll = config.get<boolean>("notifyBroadcast", false);

    if (isForMe || (isBroadcast && notifyAll)) {
      // Queue for LM tool consumption
      pendingMessages.push(msg);

      // Auto-open in Copilot chat (opt-in) — drives the Copilot agent with no manual
      // click. Only for messages actually addressed to us (never broadcasts), and once
      // per message. Off by default because it steals editor focus / starts a chat turn.
      const autoOpenChat = vscode.workspace
        .getConfiguration("partyline")
        .get<boolean>("autoOpenChat", false);
      if (isForMe && autoOpenChat) {
        openInChat(msg);
      }

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
            openInChat(msg);
          }
        });
    }
    updateStatusBar(client!.unreadCount);
  });

  let lastAgentCount = 0;
  let lastClaimCount = 0;
  client.onAgentsChanged((agents) => {
    agentsProvider.update(agents);
    metricsProvider.updateAgentCount(agents.length);
    lastAgentCount = agents.length;
    diagnosticsProvider.setCounts(lastAgentCount, lastClaimCount);
  });
  client.onClaimsChanged((claims) => {
    claimsProvider.update(claims);
    metricsProvider.updateClaimCount(claims.length);
    lastClaimCount = claims.length;
    diagnosticsProvider.setCounts(lastAgentCount, lastClaimCount);
  });
  client.onDiagnostic((event) => handleDiagnosticEvent(event));

  client.connect();
  metricsProvider.start();
  activeMetricsProvider = metricsProvider;
  updateStatusBar(0, true);
}

function disconnect() {
  if (tokenRefreshTimer) {
    clearInterval(tokenRefreshTimer);
    tokenRefreshTimer = undefined;
  }
  if (activeMetricsProvider) {
    activeMetricsProvider.stop();
    activeMetricsProvider = undefined;
  }
  if (client) {
    client.disconnect();
    client = undefined;
  }
  responder = undefined;
  if (diagnosticsProvider) { diagnosticsProvider.reset(); }
  updateStatusBar(0, false);
}

function updateStatusBar(unread: number, connected?: boolean) {
  const idSuffix = currentAgentId ? ` as ${currentAgentId}` : "";
  if (connected === false) {
    statusBarItem.text = "$(megaphone) Partyline";
    statusBarItem.tooltip = `Click to connect${idSuffix}`;
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
  statusBarItem.tooltip = `Connected${idSuffix} — click to mark all read`;
  statusBarItem.command = "partyline.markRead";
}

function registerMcpServer(context: vscode.ExtensionContext) {
  // Dynamically register Partyline as an MCP server for any LM chat participant
  const provider: vscode.McpServerDefinitionProvider<vscode.McpServerDefinition> = {
    provideMcpServerDefinitions(_token) {
      const resolved = resolveConfig();
      if (!resolved.url) { return []; }

      const headers: Record<string, string> = {};
      if (resolved.token) {
        headers["Authorization"] = `Bearer ${resolved.token}`;
      }

      return [
        new vscode.McpHttpServerDefinition(
          "Covasant Partyline",
          vscode.Uri.parse(`${resolved.url}/mcp`),
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
      const resolved = resolveConfig();
      const agentId = resolved.agentId || "unknown";

      if (request.command === "status") {
        // Show current line status
        response.markdown(`**Partyline Status**\n\n`);
        response.markdown(`- Agent ID: \`${agentId}\` (${resolved.agentIdSource})\n`);
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

async function copyDiagnostics() {
  const cfg = vscode.workspace.getConfiguration("partyline");
  const resolved = resolveConfig();
  const snap = diagnosticsProvider.snapshot();

  // Live language-model probe — the single most useful signal for "why did the
  // responder error?" (Copilot not signed in / no model / consent not granted).
  let lmInfo: string;
  try {
    const models = await vscode.lm.selectChatModels({ vendor: "copilot" });
    lmInfo = models.length
      ? models.map((m) => `${m.vendor}/${m.family} id=${m.id} maxInput=${m.maxInputTokens}`).join("; ")
      : "NONE available (no Copilot models — is Copilot signed in / enabled?)";
  } catch (e) {
    const err = e as Error & { code?: string };
    lmInfo = `probe FAILED: ${err.name} code=${err.code ?? "?"} — ${err.message}`;
  }

  const age = (ms?: number) =>
    ms ? `${Math.floor((Date.now() - ms) / 1000)}s ago (${new Date(ms).toISOString()})` : "—";

  const lines = [
    "# Partyline Diagnostics",
    `Generated: ${new Date().toISOString()}`,
    "",
    "## Environment",
    `- Extension version: ${extVersion}`,
    `- VS Code: ${vscode.version}`,
    `- Platform: ${process.platform}`,
    `- Connected client: ${client ? "yes" : "no"}`,
    "",
    "## Config",
    `- url set: ${resolved.url ? "yes" : "NO"}`,
    `- agentId: ${resolved.agentId || "(unset)"} (${resolved.agentIdSource})`,
    `- databricksProfile: ${resolved.databricksProfile}`,
    `- autoRespond: ${cfg.get<boolean>("autoRespond", false)}`,
    `- notifyBroadcast: ${cfg.get<boolean>("notifyBroadcast", false)}`,
    `- token source: ${snap.token}`,
    "",
    "## Connection",
    `- state: ${snap.connection}`,
    `- last message: ${age(snap.lastMessageAt)}`,
    `- active agents: ${snap.agents}`,
    `- active claims: ${snap.claims}`,
    `- errors (session): ${snap.errorCount}`,
    "",
    "## Language model (live probe)",
    `- ${lmInfo}`,
    "",
    "## Responder",
    `- autoRespond enabled: ${cfg.get<boolean>("autoRespond", false)}`,
    `- last status: ${snap.responder}`,
    "",
    `## Recent events (most recent first — ${snap.events.length})`,
    ...(snap.events.length
      ? snap.events.map((e) => `- [${new Date(e.time).toISOString()}] ${e.kind}: ${e.message}`)
      : ["- (none)"]),
    "",
    "## Notes",
    '- Full unabridged log: Output panel → "Partyline Diagnostics".',
  ];
  const text = lines.join("\n");
  await vscode.env.clipboard.writeText(text);
  const doc = await vscode.workspace.openTextDocument({ content: text, language: "markdown" });
  await vscode.window.showTextDocument(doc, { preview: false });
  vscode.window.showInformationMessage("Partyline diagnostics copied to clipboard.");
}

export function deactivate() {
  disconnect();
}

function openInChat(msg: PartylineMessage) {
  const prompt =
    `A Partyline message just arrived for you:\n\n` +
    `**From:** ${msg.agent_id}\n` +
    `**Type:** ${msg.message_type}\n` +
    (msg.repo ? `**Repo:** ${msg.repo}\n` : "") +
    (msg.resource_path ? `**Resource:** ${msg.resource_path}\n` : "") +
    `**Message:** ${msg.payload || "(no payload)"}\n\n` +
    `Please respond appropriately using Partyline tools (post_message, claim_resource, etc).`;
  // Open Copilot chat in agent mode with the message as the query. Best-effort: whether
  // this auto-submits the turn or just prefills depends on the VS Code chat version.
  vscode.commands.executeCommand("workbench.action.chat.open", { query: prompt, mode: "agent" });
}

function handleDiagnosticEvent(event: PartylineDiagnosticEvent) {
  diagnosticsChannel.appendLine(
    `[${event.timestamp}] [${event.kind}] ${event.message}`
  );

  diagnosticsProvider.pushEvent(event.kind, event.message);
  if (event.kind === "sse_connected") { diagnosticsProvider.setConnection("Connected ✓"); }
  else if (event.kind === "sse_error") { diagnosticsProvider.setConnection("Error — reconnecting"); }
  else if (event.kind === "sse_closed") { diagnosticsProvider.setConnection("Reconnecting…"); }
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
