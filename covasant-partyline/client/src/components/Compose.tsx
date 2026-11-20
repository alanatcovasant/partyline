import { useEffect, useState } from "react";
import { MESSAGE_TYPES, type Agent } from "../types";

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
export function Compose({ agents }: { agents: Agent[] }) {
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
