import { useState } from "react";
import type { Message } from "../types";

// Compact conversation-id chip: shows the first 8 chars, full id on hover, and copies the
// full id to the clipboard on click (agents paste it into get_conversation to read a thread).
export function ConversationId({ id }: { id: string }) {
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
export function MsgBody({ m }: { m: Message }) {
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
export function MsgFields({ m }: { m: Message }) {
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
