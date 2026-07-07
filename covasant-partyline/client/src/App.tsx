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
};
type Claim = {
  id: string;
  resource_path: string;
  repo: string;
  branch?: string | null;
  agent_id: string;
  expires_at: string;
};

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
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
  const [connected, setConnected] = useState(false);
  const lastId = useRef(0);

  // Seed history + poll agents/claims.
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
    };
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  // Live push via SSE; on each notification pull the new rows.
  useEffect(() => {
    const es = new EventSource("/api/stream");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = () => {
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
          <section>
            <h2>Active agents ({agents.length})</h2>
            <ul className="pills">
              {agents.map((a) => (
                <li key={a.agent_id}>
                  <b>{a.agent_id}</b>
                  {a.repo && <span className="muted">{a.repo}{a.branch ? `@${a.branch}` : ""}</span>}
                  <time>{new Date(a.last_heartbeat).toLocaleTimeString()}</time>
                </li>
              ))}
              {!agents.length && <li className="muted">None active.</li>}
            </ul>
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
