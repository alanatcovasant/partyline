import * as vscode from "vscode";
import { PartylineClient, PartylineMessage } from "./client";

/**
 * Autonomous coordination responder backed by the VS Code Language Model API.
 *
 * When a message addressed to this agent arrives over SSE, this wakes a `vscode.lm`
 * tool-calling loop that reads the message and responds on the Partyline using the
 * coordination tools below. It is the one path that can act without an active Copilot
 * chat session — the extension itself is the agent.
 *
 * Scope is coordination ONLY (ack / answer / status / blocked / claim / route). It has
 * no file-editing tools by construction. Off by default (`partyline.autoRespond`).
 */

const SYSTEM_PROMPT = `You are the Partyline coordination responder for agent "{AGENT_ID}".
A message addressed to you just arrived. Decide how to respond and use the provided tools.

Rules:
- You coordinate; you do NOT edit code, run commands, or start long work. If the message
  asks for code changes, post an "ack" saying you received it and that it will be handled
  by the coding agent/human in this window — do not attempt the change.
- Reply on the line with post_reply. Use message_type: "ack" to accept an assignment,
  "status" for progress, "done" when a coordination task is complete, "blocked" (with the
  specific blocker) if you cannot act, or "status" to answer a question concisely.
- Keep replies short and specific. One reply is usually enough — then stop.
- Never invent facts. If you lack context, call get_recent_context first.
- Address the original sender (the tool does this for you). Do not @-mention broadly.`;

const TOOLS: vscode.LanguageModelChatTool[] = [
  {
    name: "post_reply",
    description:
      "Post a reply on the Partyline, addressed back to the sender of the incoming message and threaded to it.",
    inputSchema: {
      type: "object",
      properties: {
        message_type: {
          type: "string",
          enum: ["ack", "status", "done", "blocked", "question"],
          description: "The coordination message type.",
        },
        payload: { type: "string", description: "The reply text. Concise." },
      },
      required: ["message_type", "payload"],
    },
  },
  {
    name: "claim_resource",
    description:
      "Claim a shared resource (TTL lease) when the request is about who owns a file/surface.",
    inputSchema: {
      type: "object",
      properties: {
        resource_path: { type: "string" },
        reason: { type: "string" },
      },
      required: ["resource_path"],
    },
  },
  {
    name: "get_recent_context",
    description:
      "Return recent Partyline messages (whole line) so you can understand the situation before replying.",
    inputSchema: { type: "object", properties: {} },
  },
];

function formatIncoming(m: PartylineMessage): string {
  return (
    `id=${m.id} type=${m.message_type} from=${m.agent_id}` +
    (m.repo ? ` repo=${m.repo}${m.branch ? "/" + m.branch : ""}` : "") +
    (m.resource_path ? ` file=${m.resource_path}` : "") +
    `\n${m.payload || "(no payload)"}`
  );
}

export class PartylineResponder {
  private busy = false;
  private handled = new Set<number>();
  private noModelWarned = false;

  constructor(
    private readonly client: PartylineClient,
    private readonly agentId: string,
    private readonly log: (msg: string) => void
  ) {}

  /** Loop-safety + relevance gate. Only act on messages directly addressed to us. */
  shouldRespond(m: PartylineMessage): boolean {
    if (!m.id || this.handled.has(m.id)) { return false; }
    if (m.agent_id === this.agentId) { return false; } // never reply to ourselves
    if (m.recipient !== this.agentId) { return false; } // require a direct "To:" — mentions/cc are FYI
    // Don't reply to terminal/reply types (prevents responder ping-pong).
    if (["ack", "done", "release"].includes(m.message_type)) { return false; }
    return true;
  }

  async handle(m: PartylineMessage): Promise<void> {
    if (!this.shouldRespond(m)) { return; }
    if (this.busy) {
      this.log(`[responder] busy; skipping #${m.id} (will not auto-reply)`);
      return;
    }
    this.handled.add(m.id);
    this.busy = true;
    this.log(`[responder] handling #${m.id} from ${m.agent_id} (type=${m.message_type})`);
    const cts = new vscode.CancellationTokenSource();
    const timer = setTimeout(() => cts.cancel(), 60_000);
    try {
      await this.runLoop(m, cts.token);
    } catch (e) {
      const err = e as Error & { code?: string };
      if (err?.name === "LanguageModelError") {
        this.log(`[responder] LM error on #${m.id}: code=${err.code ?? "?"} ${err.message}`);
        if (!this.noModelWarned) {
          this.noModelWarned = true;
          vscode.window.showWarningMessage(
            `Partyline auto-responder could not reach a language model: ${err.message}. ` +
            `Ensure Copilot is signed in, or disable partyline.autoRespond.`
          );
        }
      } else {
        this.log(`[responder] error on #${m.id}: ${err?.message || String(e)}`);
      }
    } finally {
      clearTimeout(timer);
      cts.dispose();
      this.busy = false;
    }
  }

  private async runLoop(m: PartylineMessage, token: vscode.CancellationToken): Promise<void> {
    const models = await vscode.lm.selectChatModels({ vendor: "copilot" });
    if (models.length === 0) {
      this.log(`[responder] no language model available; cannot auto-respond to #${m.id}`);
      if (!this.noModelWarned) {
        this.noModelWarned = true;
        vscode.window.showWarningMessage(
          "Partyline auto-responder is on but no language model is available (is Copilot signed in?)."
        );
      }
      return;
    }
    const model = models[0];
    this.log(`[responder] using model ${model.vendor}/${model.family} (${model.id})`);

    const messages: vscode.LanguageModelChatMessage[] = [
      vscode.LanguageModelChatMessage.User(
        SYSTEM_PROMPT.replace("{AGENT_ID}", this.agentId) +
        `\n\n--- Incoming message ---\n${formatIncoming(m)}`
      ),
    ];

    for (let turn = 0; turn < 5; turn++) {
      if (token.isCancellationRequested) { return; }
      const response = await model.sendRequest(
        messages,
        {
          tools: TOOLS,
          justification:
            "Partyline auto-responder handling an inter-agent message addressed to this agent",
        },
        token
      );

      let text = "";
      const toolCalls: vscode.LanguageModelToolCallPart[] = [];
      for await (const part of response.stream) {
        if (part instanceof vscode.LanguageModelTextPart) {
          text += part.value;
        } else if (part instanceof vscode.LanguageModelToolCallPart) {
          toolCalls.push(part);
        }
      }

      if (toolCalls.length === 0) {
        if (text.trim()) { this.log(`[responder] #${m.id} model note: ${text.trim().slice(0, 200)}`); }
        return; // model is done
      }

      const assistantParts: (vscode.LanguageModelTextPart | vscode.LanguageModelToolCallPart)[] = [];
      if (text.trim()) { assistantParts.push(new vscode.LanguageModelTextPart(text)); }
      assistantParts.push(...toolCalls);
      messages.push(vscode.LanguageModelChatMessage.Assistant(assistantParts));

      const resultParts: vscode.LanguageModelToolResultPart[] = [];
      for (const call of toolCalls) {
        const result = await this.execTool(call.name, call.input as Record<string, unknown>, m);
        resultParts.push(
          new vscode.LanguageModelToolResultPart(call.callId, [
            new vscode.LanguageModelTextPart(JSON.stringify(result)),
          ])
        );
      }
      messages.push(vscode.LanguageModelChatMessage.User(resultParts));
    }
    this.log(`[responder] #${m.id} hit max turns; stopping`);
  }

  private async execTool(
    name: string,
    input: Record<string, unknown>,
    incoming: PartylineMessage
  ): Promise<unknown> {
    try {
      if (name === "post_reply") {
        const posted = await this.client.postMessage({
          agent_id: this.agentId,
          message_type: String(input.message_type || "status"),
          payload: String(input.payload || ""),
          recipient: incoming.agent_id, // reply to the sender
          reply_to_id: incoming.id,
          repo: incoming.repo,
          branch: incoming.branch,
        });
        this.log(
          `[responder] posted ${input.message_type} #${posted.id} -> ${incoming.agent_id} (re #${incoming.id})`
        );
        return { posted: true, id: posted.id };
      }
      if (name === "claim_resource") {
        const res = await this.client.claimResource({
          agent_id: this.agentId,
          repo: incoming.repo || "",
          resource_path: String(input.resource_path || ""),
          branch: incoming.branch,
          reason: input.reason ? String(input.reason) : undefined,
        });
        this.log(`[responder] claim ${input.resource_path} acquired=${res.acquired}`);
        return res;
      }
      if (name === "get_recent_context") {
        const recent = await this.client.getUnread(false, 20);
        return recent.map((r) => ({
          id: r.id,
          type: r.message_type,
          from: r.agent_id,
          to: r.recipient,
          payload: (r.payload || "").slice(0, 200),
        }));
      }
      return { error: `unknown tool ${name}` };
    } catch (e) {
      const msg = (e as Error)?.message || String(e);
      this.log(`[responder] tool ${name} failed: ${msg}`);
      return { error: msg };
    }
  }
}
