import { useState } from "react";
import { getJSON } from "../lib";
import { MESSAGE_TYPES, type SearchHit } from "../types";
import { MsgBody } from "./MessageDetails";

/** Search the full line history (live + archived) and pivot any hit to its whole
 *  conversation thread. Backed by /api/messages/query and /api/conversations/{id}. */
export function Search() {
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
