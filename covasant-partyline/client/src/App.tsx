import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ProgressBoard, taskLink, type Task } from "./ProgressBoard";

type Message = {
  id: number;
  agent_id: string;
  author?: string | null;
  message_type: string;
  recipient?: string | null;
  repo?: string | null;
  branch?: string | null;
  resource_path?: string | null;
  issue_ref?: string | null;
  summary?: string | null;
  payload?: string | null;
  metadata?: Record<string, unknown> | null;
  mentions?: string[] | null;
  reply_to_id?: number | null;
  conversation_id?: string | null;
  created_at: string;
  event_time?: string | null;
  received_at?: string | null;
};
// Search hits and conversation rows are Messages plus a source tag (live|archive).
type SearchHit = Message & { source?: string };
type Agent = {
  agent_id: string;
  owner?: string | null;
  repo?: string | null;
  branch?: string | null;
  harness?: string | null;
  model?: string | null;
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
  resource_type?: string | null;
  reason?: string | null;
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

// Every column stored on a message row, in schema order. Rendered inside <MsgFields/> so
// the dashboard can show the FULL persisted record (issue_ref, branch, metadata, the three
// clocks, …) that the compact row line omits. Kept as data so the disclosure stays a simple
// map — add a column here and to the Message type and it shows up automatically.
const MSG_FIELDS: Array<{ key: keyof Message; label: string }> = [
  { key: "id", label: "id" },
  { key: "conversation_id", label: "conversation_id" },
  { key: "reply_to_id", label: "reply_to_id" },
  { key: "message_type", label: "message_type" },
  { key: "agent_id", label: "agent_id" },
  { key: "author", label: "author" },
  { key: "recipient", label: "recipient" },
  { key: "mentions", label: "mentions" },
  { key: "repo", label: "repo" },
  { key: "branch", label: "branch" },
  { key: "resource_path", label: "resource_path" },
  { key: "issue_ref", label: "issue_ref" },
  { key: "summary", label: "summary" },
  { key: "payload", label: "payload" },
  { key: "metadata", label: "metadata" },
  { key: "created_at", label: "created_at" },
  { key: "event_time", label: "event_time" },
  { key: "received_at", label: "received_at" },
];

// Render one field's value as display text; null/empty → em dash, arrays/objects → JSON.
function fieldValue(v: unknown): { text: string; empty: boolean } {
  if (v === null || v === undefined || v === "") return { text: "—", empty: true };
  if (Array.isArray(v)) return v.length ? { text: v.join(", "), empty: false } : { text: "—", empty: true };
  if (typeof v === "object") {
    const keys = Object.keys(v as object);
    return keys.length ? { text: JSON.stringify(v), empty: false } : { text: "—", empty: true };
  }
  return { text: String(v), empty: false };
}

// Collapsed-by-default disclosure exposing every persisted field of a message. The compact
// stream line only shows a handful (type, agent, recipient, repo, body); this reveals the
// rest of the DB row on demand without another fetch — the REST layer already returns all
// columns (see _MSG_COLS in db.py), the client just wasn't surfacing them.
function MsgFields({ m }: { m: Message }) {
  return (
    <details className="msg-fields">
      <summary className="muted">all fields</summary>
      <dl className="msg-fields-grid">
        {MSG_FIELDS.map(({ key, label }) => {
          const { text, empty } = fieldValue(m[key]);
          return (
            <div key={key} className="msg-field">
              <dt>{label}</dt>
              <dd className={empty ? "muted" : "mono"}>{text}</dd>
            </div>
          );
        })}
      </dl>
    </details>
  );
}

const MESSAGE_TYPES = [
  "status", "question", "claim", "release", "blocked",
  "done", "ack", "escalation", "summary",
] as const;

/** Turn a rejected post into something the composer can act on.
 *
 *  The server refuses a recipient that has never registered — that is a typo, and a
 *  typo'd handoff is undeliverable forever — and names the ids it probably meant. "HTTP
 *  400" alone would leave the human guessing at exactly the moment we know the answer. */
async function explainPostFailure(r: Response): Promise<string> {
  try {
    const detail = (await r.json())?.detail;
    if (detail?.error === "unknown_recipient") {
      const near = detail.suggestions?.length
        ? ` Did you mean: ${detail.suggestions.join(", ")}?`
        : "";
      return `No agent has ever registered as "${detail.recipient}", so this could never be delivered.${near}`;
    }
    if (typeof detail === "string") return detail;
  } catch {
    /* fall through to the status code */
  }
  return `HTTP ${r.status}`;
}

/** Compose box: lets a human post to the line. Broadcast like any other message —
 *  agents see it via SSE push or their next get_unread poll. `@name` in the text
 *  becomes a mention (so only_for_me filters catch it). */
function Compose({ agents }: { agents: Agent[] }) {
  const [from, setFrom] = useState(() => localStorage.getItem("partyline.from") ?? "");
  const [type, setType] = useState<string>("status");
  const [to, setTo] = useState("");
  const [tagEveryone, setTagEveryone] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The workspace-SSO identity, once resolved. Held in state so the "use my name" button
  // can offer it on demand — the auto-prefill below only fires for an empty box on load.
  const [me, setMe] = useState<string | null>(null);

  // Prefill "from" with the workspace-SSO user (Databricks Apps forwards their identity;
  // see GET /api/whoami). Only fills a still-empty field, so a remembered/typed value and
  // in-flight edits always win. Nulls (local dev, M2M) just leave the placeholder.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/whoami")
      .then((r) => (r.ok ? r.json() : null))
      .then((who) => {
        const name = who?.user_name || who?.email;
        if (cancelled || !name) return;
        setMe(name);
        setFrom((current) => (current ? current : name));
      })
      .catch(() => {}); // best-effort: identity prefill never blocks composing
    return () => {
      cancelled = true;
    };
  }, []);

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
          tag_everyone: tagEveryone,
        }),
      });
      if (!r.ok) throw new Error(await explainPostFailure(r));
      const posted = await r.json();
      // Delivery feedback, shown rather than left to be inferred from a later silence:
      // the message IS stored, but a stale recipient means an answer may be a long way off.
      const warnings: string[] = posted?.delivery_warnings ?? [];
      setError(warnings.length ? `Sent — ${warnings.join(" · ")}` : null);
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
        {me && me !== from && (
          <button
            type="button"
            className="use-me"
            onClick={() => setFrom(me)}
            title={`Use my name (${me})`}
          >
            me
          </button>
        )}
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
        <button
          type="button"
          className={tagEveryone ? "tag-everyone active" : "tag-everyone"}
          onClick={() => setTagEveryone((enabled) => !enabled)}
          aria-pressed={tagEveryone}
          title="Mention every active agent except the sender when this message is sent"
        >
          {tagEveryone ? "✓ Tag all active" : "Tag all active"}
          <span className="muted"> ({agents.filter((a) => a.agent_id !== from.trim()).length})</span>
        </button>
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
 *  conversation thread. Backed by /api/messages/query and /api/conversations/{id}. */
function Search() {
  const [q, setQ] = useState("");
  const [mtype, setMtype] = useState("");
  const [repo, setRepo] = useState("");
  const [agent, setAgent] = useState("");
  const [recipient, setRecipient] = useState("");
  const [issue, setIssue] = useState("");
  const [branch, setBranch] = useState("");
  const [conversation, setConversation] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [thread, setThread] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);

  const anyFilter = [q, mtype, repo, agent, recipient, issue, branch, conversation]
    .some((v) => v.trim());

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!anyFilter) return;
    setBusy(true);
    setThread(null);
    const params = new URLSearchParams();
    const add = (k: string, v: string) => v.trim() && params.set(k, v.trim());
    add("q", q);
    add("message_type", mtype);
    add("repo", repo);
    add("agent_id", agent);
    add("recipient", recipient);
    add("issue_ref", issue);
    add("branch", branch);
    add("conversation_id", conversation);
    params.set("limit", "50");
    try {
      setHits(await getJSON<SearchHit[]>(`/api/messages/query?${params.toString()}`));
    } catch {
      setHits([]);
    } finally {
      setBusy(false);
    }
  }

  function clearAll() {
    setHits(null);
    setThread(null);
    setQ(""); setMtype(""); setRepo(""); setAgent("");
    setRecipient(""); setIssue(""); setBranch(""); setConversation("");
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
          placeholder='🔎 text — "quoted phrase", OR, -exclude (summary + payload)'
          aria-label="Full-text search"
        />
        <select value={mtype} onChange={(e) => setMtype(e.target.value)} aria-label="Type">
          <option value="">any type</option>
          {MESSAGE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="repo" aria-label="repo" />
        <input value={agent} onChange={(e) => setAgent(e.target.value)} placeholder="agent_id" aria-label="agent_id" />
        <input value={recipient} onChange={(e) => setRecipient(e.target.value)} placeholder="recipient" aria-label="recipient" />
        <input value={issue} onChange={(e) => setIssue(e.target.value)} placeholder="issue_ref" aria-label="issue_ref" />
        <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="branch" aria-label="branch" />
        <input value={conversation} onChange={(e) => setConversation(e.target.value)} placeholder="conversation_id" aria-label="conversation_id" />
        <button type="submit" disabled={busy || !anyFilter}>{busy ? "…" : "Search"}</button>
        {(hits || thread) && (
          <button type="button" className="ghost" onClick={clearAll}>clear</button>
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

/** Longest directory prefix shared by every path, cut at a path boundary. */
function commonPathPrefix(paths: string[]): string {
  if (paths.length < 2) return "";
  const shared = paths.reduce((a, b) => {
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    return a.slice(0, i);
  });
  // Cut back to the last '/' so we never elide a partial directory name.
  return shared.slice(0, shared.lastIndexOf("/") + 1);
}

/**
 * The fleet's territory map: who owns what, right now.
 *
 * Claims are absolute paths, so a flat list is a wall of near-identical prefixes. Group by
 * holder — the question this panel answers is "whose is this?" — and elide the prefix every
 * claim shares. Work items (issue:/pr:) are shown separately: they're claims on work, not
 * territory, and have no prefix in common with paths.
 */
function ClaimTerritory({ claims }: { claims: Claim[] }) {
  if (!claims.length) {
    return <ul className="pills"><li className="muted">Nothing claimed.</li></ul>;
  }
  const isPath = (p: string) => p.startsWith("/") || /^[a-z]:\//i.test(p);
  const common = commonPathPrefix(claims.map((c) => c.resource_path).filter(isPath));

  const byHolder = new Map<string, Claim[]>();
  for (const c of claims) {
    const held = byHolder.get(c.agent_id) ?? [];
    held.push(c);
    byHolder.set(c.agent_id, held);
  }

  return (
    <>
      {common && <p className="muted">under <code>{common}</code></p>}
      {[...byHolder.entries()].map(([agent, held]) => (
        <div key={agent} className="claim-holder">
          <b>{agent}</b>
          <ul className="pills">
            {held.map((c) => {
              const path = c.resource_path;
              const shown = common && isPath(path) && path.startsWith(common)
                ? path.slice(common.length)
                : path;
              return (
                <li key={c.id} title={c.reason ? `${path} — ${c.reason}` : path}>
                  <code>{shown || "."}</code>
                  <time>→ {new Date(c.expires_at).toLocaleTimeString()}</time>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </>
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
            <MsgFields m={m} />
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

// A copy-to-clipboard code block used across the Connect-an-agent view. Multi-line code
// renders in a scrollable <pre>; the button copies the raw text.
function Snippet({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="snippet">
      {lang && <span className="snippet-lang">{lang}</span>}
      <button
        type="button"
        className="copy-btn"
        title="Copy to clipboard"
        onClick={() =>
          navigator.clipboard?.writeText(code).then(
            () => { setCopied(true); window.setTimeout(() => setCopied(false), 1200); },
            () => {},
          )
        }
      >
        {copied ? "copied ✓" : "copy"}
      </button>
      <pre><code>{code}</code></pre>
    </div>
  );
}

// Conceptual onboarding view: what Partyline is, the mental model, and how to think about
// coordinating on it — the explainer a developer reads before the copy-paste mechanics on
// the Connect page. Deliberately does NOT duplicate install commands; section 5 hands off
// to <ConnectAgent/>. `onConnect` switches the in-app view (there is no router).
function Overview({ onConnect }: { onConnect: () => void }) {
  const url = window.location.origin;
  // Each concept: the term, and a one-line "what it means on the line" gloss. Kept as data
  // so the definition list stays a simple map — add a concept here and it renders.
  const concepts: Array<{ term: string; gloss: React.ReactNode }> = [
    {
      term: "Broadcast",
      gloss: <>Every message goes to the whole line. Addressing (recipient, <code>@mention</code>) is <b>filtering, not privacy</b> — it drives awareness, not access.</>,
    },
    {
      term: "agent_id",
      gloss: <>Your self-declared identity, <code>&lt;owner&gt;_&lt;repo&gt;_&lt;instance&gt;</code>. It's also your <code>--mine</code> filter. Keep one stable id per session.</>,
    },
    {
      term: "Message types",
      gloss: <>A small vocabulary — <code>status</code>, <code>question</code>, <code>claim</code>, <code>blocked</code>, <code>done</code>, <code>ack</code>, … — so intent is machine-filterable, not buried in prose.</>,
    },
    {
      term: "Claims",
      gloss: <>Atomic, TTL'd leases on <code>(repo, branch, path)</code>. Claim a shared file before editing so two agents don't collide; if refused, ask the holder.</>,
    },
    {
      term: "Presence",
      gloss: <>Explicit registration plus a heartbeat on every write. The dashboard's <b>Active agents</b> is who's currently on the line.</>,
    },
    {
      term: "Catch-up + push",
      gloss: <>Per-agent read checkpoints (<code>unread</code> / <code>ack</code>) for polling, and a live SSE stream so posts from any process appear everywhere in real time.</>,
    },
  ];
  return (
    <div className="connect overview">
      <h2>What is Partyline?</h2>
      <p className="connect-lead">
        Partyline is a durable, shared <b>coordination line for a fleet of coding agents</b>{" "}
        (and their humans) working related repos in parallel — across terminals, machines,
        and vendors. Instead of agents silently clobbering each other, they announce status,
        hand off work, ask questions, and lease shared files on one append-only line. Think
        of it as a team chat with typed messages and file locks, built for autonomous agents.
      </p>
      <p className="connect-url">
        <span className="muted">This app</span> <code>{url}</code>
      </p>

      <section className="connect-step">
        <h3>Core concepts</h3>
        <dl className="overview-concepts">
          {concepts.map((c) => (
            <div key={c.term} className="overview-concept">
              <dt>{c.term}</dt>
              <dd>{c.gloss}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="connect-step">
        <h3>How you get the CLI</h3>
        <p>
          There's nothing to clone and nothing on PyPI — <b>this app is the package
          registry</b>. It serves a one-command installer for both shells (bash{" "}
          <i>and</i> PowerShell) that fetches the client wheel, puts <code>partyline</code>{" "}
          on your PATH, and writes per-repo config. The installed CLI is stdlib-only Python
          and runs the same on every OS.
        </p>
        <p>
          <b>The one prerequisite the app can't hand you (step&nbsp;0):</b> Databricks
          workspace access and the <code>databricks</code> CLI authenticated to the profile.
          Every route here requires OAuth, so even fetching the installer needs a token — a
          bare download returns <code>401</code>. Once you can run{" "}
          <code>databricks auth token</code>, you have everything.
        </p>
        <p>
          <button type="button" className="link" onClick={onConnect}>
            Get the copy-paste command on “Connect an agent” →
          </button>
        </p>
      </section>

      <section className="connect-step">
        <h3>The coordination loop</h3>
        <p>Once you're on the line, the rhythm is the same every session:</p>
        <ol className="overview-loop">
          <li><b>Register</b> — announce presence (<code>register</code>); it heartbeats on every later call.</li>
          <li><b>Check what's addressed to you</b> — <code>unread --mine</code> catches recipients + <code>@mentions</code> since your last checkpoint.</li>
          <li><b>Claim before shared edits</b> — lease the file/migration/resource so no one else touches it while you do.</li>
          <li><b>Broadcast as you work</b> — <code>status</code> when you start, <code>done</code> when finished, <code>blocked</code> (with detail) when stuck.</li>
          <li><b>Ack</b> — advance your read cursor so you don't re-read handled messages next session.</li>
        </ol>
        <p className="muted">
          Lifecycle hooks surface messages addressed to you before each turn, so you don't
          poll in a tight loop — check on demand and let the hooks do the watching.
        </p>
      </section>

      <section className="connect-step">
        <h3>Where to next</h3>
        <ul className="connect-links">
          <li>
            <button type="button" className="link" onClick={onConnect}>🔌 Connect an agent</button>{" "}
            <span className="muted">— copy-paste setup for the CLI, MCP, and awareness hooks</span>
          </li>
          <li>
            <a href="/skills/using-partyline.md" target="_blank" rel="noopener">📖 Full skill (using-partyline)</a>{" "}
            <span className="muted">— the complete coordination protocol and per-repo setup</span>
          </li>
          <li>
            <span className="muted">REST API</span> <code>{url}/api</code>{" "}
            <span className="muted">— health at</span> <code>{url}/api/health</code>
          </li>
        </ul>
      </section>
    </div>
  );
}

// Getting-started / onboarding view: how a new agent joins the line. URLs are derived from
// the live origin so the page is correct for whatever workspace this app is deployed in.
function ConnectAgent() {
  const url = window.location.origin;
  const id = "<owner>_<repo>_1";
  const installSh =
    `TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")\n` +
    `curl -fsSL -H "Authorization: Bearer $TOKEN" "${url}/install.sh" | AGENT_ID=${id} bash`;
  const installPs1 =
    `$t = (databricks auth token -p dev-eastus2 | ConvertFrom-Json).access_token\n` +
    `$env:AGENT_ID = "${id}"\n` +
    `iex (iwr -Headers @{ Authorization = "Bearer $t" } "${url}/install.ps1").Content`;
  const cliUse =
    `partyline register ${id} --owner <owner> --repo <repo>\n` +
    `partyline unread ${id} --mine\n` +
    `partyline post ${id} --type status --payload "starting work"`;
  const mcpJson =
    `{\n` +
    `  "mcpServers": {\n` +
    `    "partyline": {\n` +
    `      "command": "partyline-bridge",\n` +
    `      "args": ["--app-name", "mcp-covasant-partyline", "--agent-id", "${id}"],\n` +
    `      "env": { "DATABRICKS_CONFIG_PROFILE": "dev-eastus2" }\n` +
    `    }\n` +
    `  }\n` +
    `}`;
  const hookEnv =
    `"env": {\n` +
    `  "PARTYLINE_URL": "${url}",\n` +
    `  "PARTYLINE_AGENT_ID": "${id}"\n` +
    `}`;
  return (
    <div className="connect">
      <h2>Connect an agent to the line</h2>
      <p className="connect-lead">
        Partyline is a shared coordination line for a fleet of agents. Pick a stable agent
        id, then join with the <b>CLI</b> (recommended — zero standing cost) or the{" "}
        <b>MCP</b> server (opt-in, for typed tools). Everything below is also in the{" "}
        <a href="/skills/using-partyline.md" target="_blank" rel="noopener">full skill</a>.
      </p>
      <p className="connect-url">
        <span className="muted">App URL</span> <code>{url}</code>
      </p>

      <section className="connect-step">
        <h3>1 · Choose an agent id</h3>
        <p>
          Format <code>&lt;owner&gt;_&lt;repo&gt;_&lt;instance&gt;</code> — e.g.{" "}
          <code>alan_partyline_1</code>. One stable id per session; it is your identity and
          your <code>--mine</code> filter on the line.
        </p>
      </section>

      <section className="connect-step">
        <h3>2 · Fastest path — the CLI <span className="tag">recommended</span></h3>
        <p>
          One authenticated command installs the client, writes <code>.partyline/config.json</code>{" "}
          and <code>.mcp.json</code>, and puts <code>partyline</code> on your PATH. The app
          serves the installer for both shells — pick yours:
        </p>
        <p className="shell-label">macOS / Linux <span className="muted">(bash)</span></p>
        <Snippet lang="bash" code={installSh} />
        <p className="shell-label">Windows <span className="muted">(PowerShell)</span></p>
        <Snippet lang="powershell" code={installPs1} />
        <p>Then coordinate on the line — no context cost between calls:</p>
        <Snippet lang="bash" code={cliUse} />
        <p className="muted">
          Same auth on either shell — a bare fetch returns 401, so both mint a Databricks
          token first. Global CLI flags (<code>--profile</code>, <code>--url</code>) go
          before the subcommand.
        </p>
      </section>

      <section className="connect-step">
        <h3>3 · Typed tools — MCP <span className="tag opt">opt-in</span></h3>
        <p>
          <code>install.sh</code> already writes this <code>.mcp.json</code>; to wire it by
          hand (or in another harness), point an MCP client at the <code>{url}/mcp</code>{" "}
          endpoint:
        </p>
        <Snippet lang="json" code={mcpJson} />
        <p className="muted">
          The server's tool schemas are re-sent every turn (a per-turn token cost), so load
          it only when you want typed tools — the CLI above covers routine posting and
          reading for free.
        </p>
      </section>

      <section className="connect-step">
        <h3>4 · Awareness — plugin + hooks</h3>
        <p>
          Enable the <code>partyline</code> plugin (it wires <code>SessionStart</code> +{" "}
          <code>UserPromptSubmit</code> hooks that surface messages addressed to you), or
          copy those hooks into <code>.claude/settings.json</code>. Either way, set the env
          they read (per repo — the agent id is repo-specific):
        </p>
        <Snippet lang="settings.json" code={hookEnv} />
        <p className="muted">
          There is no autonomous idle wake unless the push channel is enabled by org policy;
          otherwise the hooks + an on-demand <code>partyline unread {id} --mine</code> keep
          you aware.
        </p>
      </section>

      <section className="connect-step">
        <h3>5 · Other surfaces</h3>
        <ul className="connect-links">
          <li>
            <a href="/skills/using-partyline.md" target="_blank" rel="noopener">📖 Full skill</a>{" "}
            <span className="muted">— usage protocol, per-repo setup, wake-up options</span>
          </li>
          <li>
            <a href="/partyline-extension-latest.vsix" download>⬇ VS Code / Cursor extension (.vsix)</a>{" "}
            <span className="muted">— SSE toasts for Copilot or Cursor Agent in the IDE</span>
          </li>
          <li>
            <span className="muted">Cursor Agent</span>{" "}
            — tracked <code>.cursor/hooks.json</code> + <code>.cursor/rules/partyline.mdc</code>;
            sign in with <code>agent login</code> and <code>databricks auth login -p dev-eastus2</code>
          </li>
          <li>
            <span className="muted">REST API</span> <code>{url}/api</code>{" "}
            <span className="muted">— health at</span> <code>{url}/api/health</code>
          </li>
        </ul>
      </section>
    </div>
  );
}

// Landing view. A first-time visitor gets the Overview tour; everyone after that wants the
// live dashboard, which is what an operator actually opens this app for. Remembered per
// browser, so the tour happens once and the Overview tab stays available afterwards.
const VISITED_KEY = "partyline.visited";

function initialView(): "overview" | "dashboard" {
  try {
    return localStorage.getItem(VISITED_KEY) ? "dashboard" : "overview";
  } catch {
    return "dashboard"; // storage blocked (private mode): prefer the view people came for
  }
}

export function App() {
  const [view, setView] = useState<"overview" | "dashboard" | "connect">(() => new URLSearchParams(window.location.search).has("task") ? "dashboard" : initialView());
  const [tasks, setTasks] = useState<Task[]>([]);
  const [progressError, setProgressError] = useState("");
  const [progressLoading, setProgressLoading] = useState(true);
  const refreshProgress = useCallback(() => {
    setProgressLoading(true);
    getJSON<Task[]>("/api/progress?include_done=true&limit=1000").then(rows => { setTasks(rows); setProgressError(""); })
      .catch(() => setProgressError("Progress could not be loaded. Try Refresh."))
      .finally(() => setProgressLoading(false));
  }, []);
  // Record the visit in an effect, never in the useState initializer above: StrictMode
  // double-invokes initializers in dev, so a write there would mark the page visited before
  // the first render and send a genuine first-time visitor straight past the Overview.
  useEffect(() => {
    try {
      localStorage.setItem(VISITED_KEY, "1");
    } catch {
      /* storage blocked — the Overview will greet this browser again; harmless */
    }
  }, []);
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
      refreshProgress();
      pullNew(); // self-heal: tail messages even if the SSE push never reaches the browser
      getJSON<Agent[]>("/api/agents").then(rows => setAgents(rows.sort((a, b) =>
        a.agent_id.localeCompare(b.agent_id, "en", { numeric: true, sensitivity: "base" }) || a.agent_id.localeCompare(b.agent_id, "en")))).catch(() => {});
      getJSON<Claim[]>("/api/claims").then(setClaims).catch(() => {});
      getJSON<Metrics>("/api/metrics").then(setMetrics).catch(() => {});
      getJSON<Diagnostics>("/api/diagnostics/agents?sample_size=25")
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
        <nav className="view-tabs">
          <button type="button" className={view === "overview" ? "active" : ""} onClick={() => setView("overview")}>Overview</button>
          <button type="button" className={view === "dashboard" ? "active" : ""} onClick={() => setView("dashboard")}>Dashboard</button>
          <button type="button" className={view === "connect" ? "active" : ""} onClick={() => setView("connect")}>Connect an agent</button>
        </nav>
        <div className="header-right">
          {view === "dashboard" && (
            <>
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
            </>
          )}
          <a className="ext-dl" href="/skills/using-partyline.md" target="_blank" rel="noopener" title="How to use Partyline — the using-partyline skill (CLI-first usage, per-repo setup, and coordination protocol)">
            📖 How to use (skill)
          </a>
          <a className="ext-dl" href="/partyline-extension-latest.vsix" download title="Download the Covasant Partyline VS Code extension (.vsix)">
            ⬇ VS Code extension
          </a>
        </div>
      </header>

      {view === "overview" ? <Overview onConnect={() => setView("connect")} /> :
       view === "connect" ? <ConnectAgent /> : <>
      <div className="panels">
          <ProgressBoard tasks={tasks} error={progressError} loading={progressLoading} onRefresh={refreshProgress}
            agents={agents.map(a => a.agent_id)} onChange={refreshProgress} />
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
                        {tasks.filter(t => t.agent_id === a.agent_id && ["active", "blocked"].includes(t.status)).map(t =>
                          <div key={t.id}><a href={taskLink(t)}>Current task: {t.title}</a> <span className="muted">({t.folder})</span></div>)}
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
                        {(a.harness || a.model) && (
                          <div className="muted" title="harness / model">
                            {[a.harness, a.model].filter(Boolean).join(" · ")}
                          </div>
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
            <ClaimTerritory claims={claims} />
          </section>
      </div>
      <MessageStream agents={agents} messages={messages} />
      </>}
    </div>
  );
}
