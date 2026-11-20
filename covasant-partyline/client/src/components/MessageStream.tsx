import { useEffect, useMemo, useState } from "react";
import { getJSON } from "../lib";
import { conversationThread, filterMessages } from "../streamFilter";
import { MESSAGE_TYPES, type Agent, type Message, type SearchHit } from "../types";
import { ArchiveAllButton } from "./ArchiveAllButton";
import { Compose } from "./Compose";
import { ConversationId, MsgBody, MsgFields } from "./MessageDetails";
import { Search } from "./Search";

const PAGE_SIZE = 50;

/** The message feed: newest-first, with type + text filtering and pagination over the
 *  loaded window. (Whole-history search lives in <Search />; this filters what's loaded.)
 *  Messages with a conversation_id offer a "thread →" affordance that swaps the flat
 *  stream for that conversation's full thread, fetched from the same
 *  /api/conversations/{id} endpoint Search uses. */
export function MessageStream({ agents, messages }: { agents: Agent[]; messages: Message[] }) {
  const [ftype, setFtype] = useState("all");
  const [fq, setFq] = useState("");
  const [page, setPage] = useState(0);
  const [thread, setThread] = useState<SearchHit[] | null>(null);

  const filtered = useMemo(() => filterMessages(messages, { ftype, fq }), [messages, ftype, fq]);

  async function openThread(cid: string) {
    try {
      // Full history for the conversation — the stream window only retains the newest
      // 500 rows by id, so an older thread would render partially without this fetch.
      setThread(await getJSON<SearchHit[]>(`/api/conversations/${cid}`));
    } catch {
      setThread(conversationThread(messages, cid));
    }
  }

  // Snap back to the first (newest) page whenever the filter changes.
  useEffect(() => setPage(0), [ftype, fq]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const cur = Math.min(page, pageCount - 1);
  const shown = filtered.slice(cur * PAGE_SIZE, cur * PAGE_SIZE + PAGE_SIZE);

  const threadRow = (m: SearchHit) => (
    <li key={`${m.source ?? "live"}-${m.id}`}>
      <span className={`tag t-${m.message_type}`}>{m.message_type}</span>
      <b>{m.agent_id}</b>
      {m.recipient && <span className="muted"> → {m.recipient}</span>}
      {m.source === "archive" && <span className="muted"> (archived)</span>}
      <MsgBody m={m} />
      <MsgFields m={m} />
      <span className="msg-ids muted">
        <span title={`message id ${m.id}`}>#{m.id}</span>
        {m.conversation_id && <ConversationId id={m.conversation_id} />}
      </span>
      <time title={new Date(m.created_at).toString()}>{new Date(m.created_at).toLocaleString()}</time>
    </li>
  );

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
      {thread ? (
        <div className="search-thread">
          <button type="button" className="ghost" onClick={() => setThread(null)}>← back to stream</button>
          <ul>{thread.map(threadRow)}{!thread.length && <li className="muted">Empty thread.</li>}</ul>
        </div>
      ) : (
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
              {m.conversation_id && (
                <button
                  type="button"
                  className="link"
                  onClick={() => openThread(m.conversation_id!)}
                  title={`View the full thread for conversation ${m.conversation_id}`}
                >
                  thread →
                </button>
              )}
            </span>
            <time title={new Date(m.created_at).toString()}>{new Date(m.created_at).toLocaleString()}</time>
          </li>
        ))}
        {!filtered.length && <li className="muted">No messages match.</li>}
      </ul>
      )}
      {!thread && pageCount > 1 && (
        <div className="pager">
          <button type="button" disabled={cur === 0} onClick={() => setPage(cur - 1)}>← newer</button>
          <span className="muted">page {cur + 1} / {pageCount}</span>
          <button type="button" disabled={cur >= pageCount - 1} onClick={() => setPage(cur + 1)}>older →</button>
        </div>
      )}
    </section>
  );
}
