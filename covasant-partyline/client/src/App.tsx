import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Message = {
  id: number;
  agent_id: string;
  author?: string | null;
  message_type: string;
  recipient?: string | null;
  repo?: string | null;
  resource_path?: string | null;
  summary?: string | null;
  payload?: string | null;
  conversation_id?: string | null;
  created_at: string;
};
// Search hits and conversation rows are Messages plus a source tag (live|archive).
type SearchHit = Message & { source?: string };
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

// Compact conversation-id chip: shows the first 8 chars, full id on hover, and copies the
// full id to the clipboard on click (agents paste it into get_conversation to read a thread).
function ConversationId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="conv-id"
      title={`conversation ${id} — click to copy`}
      onClick={() => {
        navigator.clipboard?.writeText(id).then(
          () => { setCopied(true); window.setTimeout(() => setCopied(false), 1200); },
          () => {},
        );
      }}
    >
      🧵 {copied ? "copied" : id.slice(0, 8)}
    </button>
  );
}

// Render a message body: the concise `summary` as the main line, with the full `payload`
// tucked behind a collapsible "details" disclosure. Falls back to payload-as-body when a
// message has no summary (legacy rows / summary-less posts), so nothing is hidden.
function MsgBody({ m }: { m: Message }) {
  const summary = m.summary?.trim();
  const payload = m.payload?.trim();
  if (summary) {
    // Only offer the expander when payload adds something beyond the summary.
    const hasDetail = !!payload && payload !== summary;
    return (
      <>
        <code>{summary}</code>
        {hasDetail && (
          <details className="msg-details">
            <summary className="muted">details</summary>
            <code>{payload}</code>
          </details>
        )}
      </>
    );
  }
  return payload ? <code>{payload}</code> : null;
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

/** Search the full line history (live + archived) and pivot any hit to its whole
 *  conversation thread. Backed by /api/search and /api/conversations/{id}. */
function Search() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [thread, setThread] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    const query = q.trim();
    if (!query) return;
    setBusy(true);
    setThread(null);
    try {
      setHits(await getJSON<SearchHit[]>(`/api/search?q=${encodeURIComponent(query)}&limit=50`));
    } catch {
      setHits([]);
    } finally {
      setBusy(false);
    }
  }

  async function openThread(cid?: string | null) {
    if (!cid) return;
    try {
      setThread(await getJSON<SearchHit[]>(`/api/conversations/${cid}`));
    } catch {
      setThread([]);
    }
  }

  const row = (m: SearchHit) => (
    <li key={`${m.source ?? "live"}-${m.id}`}>
      <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
      <b>{m.agent_id}</b>
      {m.recipient && <span className="muted"> → {m.recipient}</span>}
      {m.source === "archive" && <span className="muted"> (archived)</span>}
      <MsgBody m={m} />
      <time>{new Date(m.created_at).toLocaleString()}</time>
    </li>
  );

  return (
    <section className="search">
      <form className="search-row" onSubmit={run}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder='🔎 search history — "quoted phrase", OR, -exclude'
          aria-label="Search history"
        />
        <button type="submit" disabled={busy || !q.trim()}>{busy ? "…" : "Search"}</button>
        {(hits || thread) && (
          <button type="button" className="ghost" onClick={() => { setHits(null); setThread(null); }}>
            clear
          </button>
        )}
      </form>

      {thread ? (
        <div className="search-thread">
          <button type="button" className="ghost" onClick={() => setThread(null)}>← back to results</button>
          <ul>{thread.map(row)}{!thread.length && <li className="muted">Empty thread.</li>}</ul>
        </div>
      ) : hits ? (
        <ul className="search-results">
          {hits.map((m) => (
            <li key={`${m.source ?? "live"}-${m.id}`}>
              <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
              <b>{m.agent_id}</b>
              {m.source === "archive" && <span className="muted"> (archived)</span>}
              <MsgBody m={m} />
              {m.conversation_id && (
                <button type="button" className="link" onClick={() => openThread(m.conversation_id)}>
                  view thread →
                </button>
              )}
            </li>
          ))}
          {!hits.length && <li className="muted">No matches.</li>}
        </ul>
      ) : null}
    </section>
  );
}

const PAGE_SIZE = 50;

/** The message feed: newest-first, with type + text filtering and pagination over the
 *  loaded window. (Whole-history search lives in <Search />; this filters what's loaded.) */
// Operator action: archive ALL live messages (moved to the archive, still searchable) and
// clear the live feed. Destructive to the live view, so it confirms first; reloads on success.
function ArchiveAllButton() {
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!window.confirm(
      "Archive ALL messages?\n\nEvery live message is moved to the archive and the live feed is cleared. " +
      "History stays searchable (search + conversations), but the live view resets for everyone.",
    )) return;
    setBusy(true);
    try {
      const r = await fetch("/api/messages/archive-all", { method: "POST" });
      if (!r.ok) throw new Error(String(r.status));
      const { archived } = await r.json();
      window.alert(`Archived ${archived} message(s).`);
      window.location.reload();
    } catch (e) {
      window.alert(`Archive failed: ${e}`);
      setBusy(false);
    }
  };
  return (
    <button type="button" className="ghost archive-all" onClick={run} disabled={busy}
      title="Move all live messages to the archive and clear the live feed">
      {busy ? "archiving…" : "🗄 archive all"}
    </button>
  );
}

function MessageStream({ agents, messages }: { agents: Agent[]; messages: Message[] }) {
  const [ftype, setFtype] = useState("all");
  const [fq, setFq] = useState("");
  const [page, setPage] = useState(0);

  const filtered = useMemo(() => {
    const needle = fq.trim().toLowerCase();
    return messages
      .slice()
      // Newest first by id, not by array position: the seed and the after_id poll can
      // append rows out of id-order (e.g. a mount-race poll that fetched from id 0), so a
      // bare .reverse() could surface old messages on top. Sorting by id is deterministic.
      .sort((a, b) => b.id - a.id)
      .filter((m) => ftype === "all" || m.message_type === ftype)
      .filter(
        (m) =>
          !needle ||
          (m.payload || "").toLowerCase().includes(needle) ||
          m.agent_id.toLowerCase().includes(needle) ||
          (m.recipient || "").toLowerCase().includes(needle)
      );
  }, [messages, ftype, fq]);

  // Snap back to the first (newest) page whenever the filter changes.
  useEffect(() => setPage(0), [ftype, fq]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const cur = Math.min(page, pageCount - 1);
  const shown = filtered.slice(cur * PAGE_SIZE, cur * PAGE_SIZE + PAGE_SIZE);

  return (
    <section className="stream">
      <h2>Message stream</h2>
      <Compose agents={agents} />
      <Search />
      <div className="stream-filter">
        <select value={ftype} onChange={(e) => setFtype(e.target.value)} aria-label="Filter by type">
          <option value="all">all types</option>
          {MESSAGE_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <input
          value={fq}
          onChange={(e) => setFq(e.target.value)}
          placeholder="filter loaded messages (agent, recipient, text)"
          aria-label="Filter messages"
        />
        <span className="muted">{filtered.length} shown</span>
        <ArchiveAllButton />
      </div>
      <ul>
        {shown.map((m) => (
          <li key={m.id}>
            <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
            <b>{m.agent_id}</b>
            {m.recipient && <span className="muted"> → {m.recipient}</span>}
            {m.repo && <span className="muted"> [{m.repo}{m.resource_path ? `:${m.resource_path}` : ""}]</span>}
            <MsgBody m={m} />
            <span className="msg-ids muted">
              <span title={`message id ${m.id}`}>#{m.id}</span>
              {m.conversation_id && (
                <ConversationId id={m.conversation_id} />
              )}
            </span>
            <time title={new Date(m.created_at).toString()}>{new Date(m.created_at).toLocaleString()}</time>
          </li>
        ))}
        {!filtered.length && <li className="muted">No messages match.</li>}
      </ul>
      {pageCount > 1 && (
        <div className="pager">
          <button type="button" disabled={cur === 0} onClick={() => setPage(cur - 1)}>← newer</button>
          <span className="muted">page {cur + 1} / {pageCount}</span>
          <button type="button" disabled={cur >= pageCount - 1} onClick={() => setPage(cur + 1)}>older →</button>
        </div>
      )}
    </section>
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
  // Background poll interval (ms); 0 = off. Persisted. Default 30s — SSE push delivers
  // messages in real time, so this poll is only a metrics refresh + catch-up fallback.
  // A slower poll (and pausing when the tab is hidden, below) lets Lakebase scale to zero
  // instead of being kept warm by an idle open dashboard.
  const [pollMs, setPollMs] = useState<number>(() => {
    const v = Number(localStorage.getItem("partyline.pollMs"));
    return Number.isFinite(v) && v >= 0 ? v : 30000;
  });
  const lastId = useRef(0);
  const seeded = useRef(false);

  // Pull messages newer than our cursor and append. Dedupes by id so the SSE push and
  // the poll (below) can both call this without double-appending, and advances the cursor
  // to the highest id seen. Skips until the initial seed has set lastId, so a mount-race
  // call can't fetch after_id=0 (the OLDEST rows) and clobber the newest-messages view.
  const pullNew = useCallback(() => {
    if (!seeded.current) return;
    getJSON<Message[]>(`/api/messages?after_id=${lastId.current}&limit=200`)
      .then((rows) => {
        if (!rows.length) return;
        lastId.current = Math.max(lastId.current, ...rows.map((m) => m.id));
        setMessages((prev) => {
          const seen = new Set(prev.map((m) => m.id));
          const fresh = rows.filter((m) => !seen.has(m.id));
          if (!fresh.length) return prev;
          // Keep the array id-sorted and retain the newest 500 BY ID — otherwise a poll
          // that raced the seed (fetching from id 0) could append old rows and push the
          // genuinely-newest messages out of the retained window.
          const merged = [...prev, ...fresh].sort((a, b) => a.id - b.id);
          return merged.slice(-500);
        });
      })
      .catch(() => {});
  }, []);

  // Seed history + poll agents/claims/metrics/diagnostics/health.
  useEffect(() => {
    // Seed with the LATEST messages, not the oldest — otherwise a large backlog fills
    // the window with ancient rows and recent posts never show (looked "not persisted").
    getJSON<Message[]>("/api/messages?latest=true&limit=200")
      .then((m) => {
        setMessages(m);
        lastId.current = m.length ? Math.max(...m.map((x) => x.id)) : 0;
        seeded.current = true; // now safe to tail forward from the cursor
        pullNew(); // catch anything posted between the seed and the listener arming
      })
      .catch(() => {});
    const refresh = () => {
      pullNew(); // self-heal: tail messages even if the SSE push never reaches the browser
      getJSON<Agent[]>("/api/agents").then(setAgents).catch(() => {});
      getJSON<Claim[]>("/api/claims").then(setClaims).catch(() => {});
      getJSON<Metrics>("/api/metrics").then(setMetrics).catch(() => {});
      getJSON<Diagnostics>("/api/diagnostics/agents?active_within_seconds=3600&sample_size=25")
        .then(setDiag).catch(() => {});
      getJSON<Health>("/api/health").then(setHealth).catch(() => {});
    };
    refresh(); // one immediate refresh on mount / when the interval changes
    if (pollMs <= 0) return; // polling off — rely on SSE push only
    let timer: number | undefined;
    // Only poll while the tab is visible: a backgrounded dashboard stops hitting the DB,
    // so an idle open tab no longer keeps Lakebase warm. Resume (and refresh once) on show.
    const arm = () => {
      window.clearInterval(timer);
      if (document.hidden) return;
      refresh();
      timer = window.setInterval(refresh, pollMs);
    };
    arm();
    document.addEventListener("visibilitychange", arm);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", arm);
    };
  }, [pullNew, pollMs]);

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
      pullNew();
    };
    return () => es.close();
  }, [pullNew]);

  return (
    <div className="wrap">
      <header>
        <h1>Covasant Partyline</h1>
        {health?.version && <span className="muted">v{health.version}</span>}
        <span className={connected ? "dot on" : "dot off"} />
        <span className="muted">{connected ? "live" : "disconnected"}</span>
        <label className="poll-select muted" title="How often the dashboard refreshes metrics/agents from the database. Messages still arrive live via SSE regardless. A slower interval (or Off) lowers database load.">
          refresh:{" "}
          <select
            value={pollMs}
            onChange={(e) => {
              const v = Number(e.target.value);
              setPollMs(v);
              localStorage.setItem("partyline.pollMs", String(v));
            }}
          >
            <option value={5000}>5s</option>
            <option value={15000}>15s</option>
            <option value={30000}>30s</option>
            <option value={60000}>60s</option>
            <option value={0}>off (live only)</option>
          </select>
        </label>
        <a className="ext-dl" href="/partyline-extension-latest.vsix" download title="Download the Covasant Partyline VS Code extension (.vsix)">
          ⬇ VS Code extension
        </a>
      </header>

      <div className="panels">
          <section className="diag-panel" style={{ order: 4 }}>
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

          <section className="metrics-panel" style={{ order: 3 }}>
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

          <section style={{ order: 1 }}>
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

          <section style={{ order: 2 }}>
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
      </div>
      <MessageStream agents={agents} messages={messages} />
    </div>
  );
}
