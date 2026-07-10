import { useEffect, useRef, useState } from "react";

type Message = {
  id: number;
  agent_id: string;
  author?: string | null;
  message_type: string;
  recipient?: string | null;
  repo?: string | null;
  resource_path?: string | null;
  payload?: string | null;
  created_at: string;
};
type Agent = {
  agent_id: string;
  owner?: string | null;
  repo?: string | null;
  branch?: string | null;
  last_heartbeat: string;
  last_status?: string | null;
  last_status_at?: string | null;
};

// An agent counts as "online" if it has heartbeat within this window (every partyline
// call refreshes the heartbeat). Older-but-still-listed agents render as offline.
const ONLINE_WITHIN_SECS = 120;
type Claim = {
  id: string;
  resource_path: string;
  repo: string;
  branch?: string | null;
  agent_id: string;
  expires_at: string;
};
type Metrics = {
  total_messages: number;
  messages_last_1m: number;
  messages_last_5m: number;
  messages_last_1h: number;
  active_agents_5m: number;
  active_agents_1h: number;
  active_claims: number;
  last_message_at: string | null;
  unique_posters_1h: number;
  sse_subscribers: number;
};
type Diagnostics = {
  active_within_seconds: number;
  total_agents: number;
  active_agents: number;
  stale_agents: number;
  oldest_heartbeat: string | null;
  newest_heartbeat: string | null;
  sampled_agents: Array<{
    agent_id: string;
    repo?: string | null;
    branch?: string | null;
    heartbeat_age_seconds: number;
    is_active: boolean;
  }>;
};
type Health = {
  status: string;
  db_configured: boolean;
  version?: string;
  build_timestamp?: string;
  git_commit?: string;
  build_host?: string;
};

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function fmtAge(seconds: number): string {
  if (seconds < 0) return "—";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

const MESSAGE_TYPES = [
  "status", "question", "claim", "release", "blocked",
  "done", "ack", "escalation", "summary",
] as const;

/** Compose box: lets a human post to the line. Broadcast like any other message —
 *  agents see it via SSE push or their next get_unread poll. `@name` in the text
 *  becomes a mention (so only_for_me filters catch it). */
function Compose({ agents }: { agents: Agent[] }) {
  const [from, setFrom] = useState(() => localStorage.getItem("partyline.from") ?? "");
  const [type, setType] = useState<string>("status");
  const [to, setTo] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (!from.trim() || !text.trim() || busy) return;
    setBusy(true);
    setError(null);
    localStorage.setItem("partyline.from", from.trim());
    const mentions = [...text.matchAll(/@([\w-]+)/g)].map((m) => m[1]);
    try {
      // Auto-register the human sender (idempotent upsert) so dashboard posters
      // appear in Active agents like everyone else on the line.
      await fetch("/api/agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: from.trim(),
          owner: from.trim(),
          repo: "dashboard",
          display_name: `${from.trim()} (human, via dashboard)`,
        }),
      }).catch(() => {}); // best-effort: never block the message on presence
      const r = await fetch("/api/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: from.trim(),
          message_type: type,
          recipient: to.trim() || null,
          payload: text.trim(),
          mentions,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setText(""); // stream updates itself via SSE
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="compose" onSubmit={send}>
      <div className="compose-row">
        <input
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          placeholder="from (e.g. alan_human)"
          aria-label="From"
          required
        />
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">
          {MESSAGE_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <input
          value={to}
          onChange={(e) => setTo(e.target.value)}
          placeholder="to (optional)"
          aria-label="Recipient"
          list="agent-ids"
        />
        <datalist id="agent-ids">
          {agents.map((a) => (
            <option key={a.agent_id} value={a.agent_id} />
          ))}
        </datalist>
      </div>
      <div className="compose-row">
        <input
          className="compose-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="message — @name to mention"
          aria-label="Message"
          required
        />
        <button type="submit" disabled={busy || !from.trim() || !text.trim()}>
          {busy ? "…" : "Send"}
        </button>
      </div>
      {error && <div className="compose-error">{error}</div>}
    </form>
  );
}

export function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [sseReconnects, setSseReconnects] = useState(0);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const lastId = useRef(0);

  // Seed history + poll agents/claims/metrics/diagnostics/health.
  useEffect(() => {
    getJSON<Message[]>("/api/messages?after_id=0&limit=200")
      .then((m) => {
        setMessages(m);
        lastId.current = m.length ? m[m.length - 1].id : 0;
      })
      .catch(() => {});
    const refresh = () => {
      getJSON<Agent[]>("/api/agents").then(setAgents).catch(() => {});
      getJSON<Claim[]>("/api/claims").then(setClaims).catch(() => {});
      getJSON<Metrics>("/api/metrics").then(setMetrics).catch(() => {});
      getJSON<Diagnostics>("/api/diagnostics/agents?active_within_seconds=3600&sample_size=25")
        .then(setDiag).catch(() => {});
      getJSON<Health>("/api/health").then(setHealth).catch(() => {});
    };
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  // 1s tick so "Xs ago" ages stay live between polls.
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // Live push via SSE; on each notification pull the new rows.
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.onopen = () => setConnected(true);
    es.onerror = () => {
      setConnected(false);
      setSseReconnects((n) => n + 1);
    };
    es.onmessage = () => {
      setLastEventAt(Date.now());
      getJSON<Message[]>(`/api/messages?after_id=${lastId.current}&limit=200`)
        .then((rows) => {
          if (!rows.length) return;
          lastId.current = rows[rows.length - 1].id;
          setMessages((prev) => [...prev, ...rows].slice(-500));
        })
        .catch(() => {});
    };
    return () => es.close();
  }, []);

  return (
    <div className="wrap">
      <header>
        <h1>Covasant Partyline</h1>
        {health?.version && <span className="muted">v{health.version}</span>}
        <span className={connected ? "dot on" : "dot off"} />
        <span className="muted">{connected ? "live" : "disconnected"}</span>
      </header>

      <div className="grid">
        <section className="stream">
          <h2>Message stream</h2>
          <Compose agents={agents} />
          <ul>
            {messages
              .slice()
              .reverse()
              .map((m) => (
                <li key={m.id}>
                  <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
                  <b>{m.agent_id}</b>
                  {m.recipient && <span className="muted"> → {m.recipient}</span>}
                  {m.repo && <span className="muted"> [{m.repo}{m.resource_path ? `:${m.resource_path}` : ""}]</span>}
                  {m.payload && <code>{m.payload}</code>}
                  <time>{new Date(m.created_at).toLocaleTimeString()}</time>
                </li>
              ))}
            {!messages.length && <li className="muted">No messages yet.</li>}
          </ul>
        </section>

        <aside>
          <section className="diag-panel">
            <h2>🩺 Diagnostics</h2>
            <dl className="metrics">
              <dt>App version</dt>
              <dd>{health?.version ? `v${health.version}` : "—"}</dd>
              <dt>Build</dt>
              <dd className="mono">
                {health?.git_commit && health.git_commit !== "unknown"
                  ? <>{health.git_commit}{health.build_timestamp && health.build_timestamp !== "dev" ? ` (${health.build_timestamp})` : ""}</>
                  : "dev"}
              </dd>
              <dt>Stream</dt>
              <dd>
                <span className={connected ? "dot on" : "dot off"} />{" "}
                {connected ? "connected" : "disconnected"}
              </dd>
              <dt>Last SSE event</dt>
              <dd>{lastEventAt ? fmtAge(Math.floor((nowMs - lastEventAt) / 1000)) : "—"}</dd>
              <dt>SSE subscribers</dt>
              <dd>{metrics ? metrics.sse_subscribers : "—"}</dd>
              <dt>SSE reconnects</dt>
              <dd className={sseReconnects > 0 ? "warn" : undefined}>{sseReconnects}</dd>
              <dt>Database</dt>
              <dd className={health && !health.db_configured ? "warn" : undefined}>
                {health ? (health.db_configured ? "configured ✓" : "not configured") : "—"}
              </dd>
              <dt>Agents active / total</dt>
              <dd className={diag && diag.active_agents === 0 ? "warn" : undefined}>
                {diag ? `${diag.active_agents} / ${diag.total_agents}` : "—"}
                {diag && diag.stale_agents > 0 ? ` (${diag.stale_agents} stale)` : ""}
              </dd>
              <dt>Newest heartbeat</dt>
              <dd>
                {diag && diag.newest_heartbeat
                  ? fmtAge(Math.floor((nowMs - new Date(diag.newest_heartbeat).getTime()) / 1000))
                  : "—"}
              </dd>
            </dl>
            {diag && diag.stale_agents > 0 && (
              <details className="diag-stale">
                <summary>{diag.stale_agents} stale agent(s)</summary>
                <ul className="pills">
                  {diag.sampled_agents
                    .filter((a) => !a.is_active)
                    .map((a) => (
                      <li key={a.agent_id}>
                        <b>{a.agent_id}</b>
                        {a.repo && <span className="muted">{a.repo}</span>}
                        <time>{fmtAge(a.heartbeat_age_seconds)}</time>
                      </li>
                    ))}
                </ul>
              </details>
            )}
          </section>

          <section className="metrics-panel">
            <h2>📊 Live Metrics</h2>
            {metrics ? (
              <dl className="metrics">
                <dt>Messages/min</dt>
                <dd>{metrics.messages_last_1m}</dd>
                <dt>Messages (5m)</dt>
                <dd>{metrics.messages_last_5m}</dd>
                <dt>Messages (1h)</dt>
                <dd>{metrics.messages_last_1h}</dd>
                <dt>Total messages</dt>
                <dd>{metrics.total_messages}</dd>
                <dt>Active agents (5m)</dt>
                <dd>{metrics.active_agents_5m}</dd>
                <dt>Active agents (1h)</dt>
                <dd>{metrics.active_agents_1h}</dd>
                <dt>Active claims</dt>
                <dd>{metrics.active_claims}</dd>
                <dt>SSE subscribers</dt>
                <dd>{metrics.sse_subscribers}</dd>
                <dt>Unique posters (1h)</dt>
                <dd>{metrics.unique_posters_1h}</dd>
                <dt>Last message</dt>
                <dd>{metrics.last_message_at ? new Date(metrics.last_message_at).toLocaleTimeString() : "—"}</dd>
              </dl>
            ) : (
              <p className="muted">Loading…</p>
            )}
          </section>

          <section>
            <h2>Active agents ({agents.length})</h2>
            <table className="agent-grid">
              <thead>
                <tr>
                  <th aria-label="online" />
                  <th>Agent</th>
                  <th>Last status</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {agents.map((a) => {
                  const ageSec = Math.floor((nowMs - new Date(a.last_heartbeat).getTime()) / 1000);
                  const online = ageSec < ONLINE_WITHIN_SECS;
                  return (
                    <tr key={a.agent_id}>
                      <td>
                        <span
                          className={online ? "dot on" : "dot off"}
                          title={online ? "online" : `offline — last seen ${fmtAge(ageSec)}`}
                        />
                      </td>
                      <td>
                        <b>{a.agent_id}</b>
                        {a.repo && (
                          <span className="muted"> {a.repo}{a.branch ? `@${a.branch}` : ""}</span>
                        )}
                      </td>
                      <td>
                        {a.last_status
                          ? <code title={a.last_status}>{a.last_status}</code>
                          : <span className="muted">—</span>}
                      </td>
                      <td>
                        <time title={a.last_status_at ? new Date(a.last_status_at).toLocaleString() : undefined}>
                          {a.last_status_at
                            ? fmtAge(Math.floor((nowMs - new Date(a.last_status_at).getTime()) / 1000))
                            : "—"}
                        </time>
                      </td>
                    </tr>
                  );
                })}
                {!agents.length && (
                  <tr><td colSpan={4} className="muted">None active.</td></tr>
                )}
              </tbody>
            </table>
          </section>

          <section>
            <h2>Active claims ({claims.length})</h2>
            <ul className="pills">
              {claims.map((c) => (
                <li key={c.id}>
                  <code>{c.repo}:{c.resource_path}</code>
                  <b>{c.agent_id}</b>
                  <time>→ {new Date(c.expires_at).toLocaleTimeString()}</time>
                </li>
              ))}
              {!claims.length && <li className="muted">Nothing claimed.</li>}
            </ul>
          </section>
        </aside>
      </div>
    </div>
  );
}
