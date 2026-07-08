import * as http from "http";
import * as https from "https";
import { URL } from "url";

export interface PartylineMessage {
  id: number;
  agent_id: string;
  message_type: string;
  recipient?: string;
  repo?: string;
  branch?: string;
  resource_path?: string;
  payload?: string;
  mentions?: string[];
  created_at: string;
}

export interface PartylineAgent {
  agent_id: string;
  owner: string;
  repo: string;
  branch?: string;
  display_name?: string;
  last_heartbeat: string;
}

export interface PartylineClaim {
  id: string;
  agent_id: string;
  repo: string;
  branch?: string;
  resource_path: string;
  reason?: string;
  expires_at: string;
}

export interface PartylineDiagnosticEvent {
  kind: "agents_poll_error" | "claims_poll_error" | "zero_agents" | "agents_recovered" | "sse_error";
  message: string;
  details?: unknown;
  timestamp: string;
}

type MessageHandler = (msg: PartylineMessage) => void;
type AgentsHandler = (agents: PartylineAgent[]) => void;
type ClaimsHandler = (claims: PartylineClaim[]) => void;
type DiagnosticsHandler = (event: PartylineDiagnosticEvent) => void;

export class PartylineClient {
  private baseUrl: string;
  private agentId: string;
  private token: string;
  private eventSource: any; // EventSource-like
  private messageHandlers: MessageHandler[] = [];
  private agentsHandlers: AgentsHandler[] = [];
  private claimsHandlers: ClaimsHandler[] = [];
  private diagnosticsHandlers: DiagnosticsHandler[] = [];
  private pollInterval: NodeJS.Timeout | undefined;
  private _unreadCount = 0;
  private lastAgentsCount = -1;
  private lastZeroAgentsDiagnosticAt = 0;

  constructor(baseUrl: string, agentId: string, token: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.agentId = agentId;
    this.token = token;
  }

  get unreadCount(): number {
    return this._unreadCount;
  }

  updateToken(newToken: string) {
    this.token = newToken;
    // Restart SSE with the new token
    if (this.eventSource) {
      this.eventSource.destroy?.();
      this.eventSource = undefined;
      this.startSSE();
    }
  }

  onMessage(handler: MessageHandler) {
    this.messageHandlers.push(handler);
  }

  onAgentsChanged(handler: AgentsHandler) {
    this.agentsHandlers.push(handler);
  }

  onClaimsChanged(handler: ClaimsHandler) {
    this.claimsHandlers.push(handler);
  }

  onDiagnostic(handler: DiagnosticsHandler) {
    this.diagnosticsHandlers.push(handler);
  }

  connect() {
    this.startSSE();
    this.pollAgentsAndClaims();
    this.pollInterval = setInterval(() => this.pollAgentsAndClaims(), 10_000);
  }

  disconnect() {
    if (this.eventSource) {
      this.eventSource.destroy?.();
      this.eventSource = undefined;
    }
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = undefined;
    }
  }

  markAllRead() {
    this._unreadCount = 0;
  }

  private startSSE() {
    const url = `${this.baseUrl}/api/stream`;
    const parsed = new URL(url);
    const mod = parsed.protocol === "https:" ? https : http;

    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (this.token) {
      headers["Authorization"] = `Bearer ${this.token}`;
    }

    const req = mod.get(url, { headers }, (res) => {
      res.setEncoding("utf8");
      let buffer = "";

      res.on("data", (chunk: string) => {
        buffer += chunk;
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data:")) { continue; }
          const data = line.slice(5).trim();
          if (!data || data === "{}") { continue; }
          try {
            const msg = JSON.parse(data) as PartylineMessage;
            if (msg.id) {
              this._unreadCount++;
              for (const h of this.messageHandlers) { h(msg); }
            }
          } catch { /* skip malformed */ }
        }
      });

      res.on("end", () => {
        // Reconnect after a short delay
        setTimeout(() => this.startSSE(), 3000);
      });
    });

    req.on("error", () => {
      this.emitDiagnostic({
        kind: "sse_error",
        message: "SSE stream error; reconnecting in 5s",
        details: { url },
      });
      setTimeout(() => this.startSSE(), 5000);
    });

    this.eventSource = req;
  }

  private async pollAgentsAndClaims() {
    try {
      const agents = await this.fetchJson<PartylineAgent[]>("/api/agents");
      for (const h of this.agentsHandlers) { h(agents); }

      if (agents.length === 0) {
        const now = Date.now();
        if (this.lastAgentsCount !== 0 || now - this.lastZeroAgentsDiagnosticAt > 60_000) {
          this.lastZeroAgentsDiagnosticAt = now;
          try {
            const diagnostics = await this.fetchJson<unknown>(
              "/api/diagnostics/agents?active_within_seconds=3600&sample_size=25"
            );
            this.emitDiagnostic({
              kind: "zero_agents",
              message: "Partyline reported zero active agents",
              details: diagnostics,
            });
          } catch (e) {
            this.emitDiagnostic({
              kind: "agents_poll_error",
              message: "Failed to fetch /api/diagnostics/agents after zero-agent result",
              details: this.errorToDetails(e),
            });
          }
        }
      } else if (this.lastAgentsCount === 0) {
        this.emitDiagnostic({
          kind: "agents_recovered",
          message: `Agent list recovered: ${agents.length} active`,
          details: { activeAgents: agents.length },
        });
      }
      this.lastAgentsCount = agents.length;
    } catch (e) {
      this.emitDiagnostic({
        kind: "agents_poll_error",
        message: "Failed to poll /api/agents",
        details: this.errorToDetails(e),
      });
    }

    try {
      const claims = await this.fetchJson<PartylineClaim[]>("/api/claims");
      for (const h of this.claimsHandlers) { h(claims); }
    } catch (e) {
      this.emitDiagnostic({
        kind: "claims_poll_error",
        message: "Failed to poll /api/claims",
        details: this.errorToDetails(e),
      });
    }
  }

  private emitDiagnostic(event: Omit<PartylineDiagnosticEvent, "timestamp">) {
    const enriched: PartylineDiagnosticEvent = {
      ...event,
      timestamp: new Date().toISOString(),
    };
    for (const h of this.diagnosticsHandlers) { h(enriched); }
  }

  private errorToDetails(err: unknown): unknown {
    if (err instanceof Error) {
      return { name: err.name, message: err.message, stack: err.stack };
    }
    return { raw: String(err) };
  }

  private fetchJson<T>(path: string): Promise<T> {
    return new Promise((resolve, reject) => {
      const url = `${this.baseUrl}${path}`;
      const parsed = new URL(url);
      const mod = parsed.protocol === "https:" ? https : http;

      const headers: Record<string, string> = { Accept: "application/json" };
      if (this.token) {
        headers["Authorization"] = `Bearer ${this.token}`;
      }

      const req = mod.get(url, { headers }, (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (d: string) => (body += d));
        res.on("end", () => {
          const status = res.statusCode || 0;
          if (status >= 400) {
            reject(
              new Error(
                `GET ${path} failed (${status}). Response: ${body.slice(0, 500)}`
              )
            );
            return;
          }
          try { resolve(JSON.parse(body)); }
          catch (e) { reject(e); }
        });
      });
      req.on("error", reject);
    });
  }
}
