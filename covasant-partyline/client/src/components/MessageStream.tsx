import { useEffect, useMemo, useState } from "react";
import { MESSAGE_TYPES, type Agent, type Message } from "../types";
import { ArchiveAllButton } from "./ArchiveAllButton";
import { Compose } from "./Compose";
import { ConversationId, MsgBody, MsgFields } from "./MessageDetails";
import { Search } from "./Search";

const PAGE_SIZE = 50;

/** The message feed: newest-first, with type + text filtering and pagination over the
 *  loaded window. (Whole-history search lives in <Search />; this filters what's loaded.) */
export function MessageStream({ agents, messages }: { agents: Agent[]; messages: Message[] }) {
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
