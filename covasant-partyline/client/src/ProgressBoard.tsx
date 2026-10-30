import { useEffect, useMemo, useState } from "react";

export type Task = {
  id: string; folder: string; title: string; status: string; agent_id: string;
  summary: string; workstream?: string; workdir?: string; branch?: string;
  issue_ref?: string; updated_at: string;
};
type Update = { operation_id: string; agent_id: string; status: string; summary: string; details?: string; created_at: string };

export function taskLink(task: Task) {
  return `?task=${encodeURIComponent(task.id)}&folder=${encodeURIComponent(task.folder)}`;
}

export function ProgressBoard({ tasks, error, loading, onRefresh, agents, onChange }:
  { tasks: Task[]; error: string; loading: boolean; onRefresh: () => void; agents: string[]; onChange: () => void }) {
  const params = new URLSearchParams(window.location.search);
  const [folder, setFolder] = useState(params.get("folder") || "");
  const [stream, setStream] = useState("");
  const [showDone, setShowDone] = useState(false);
  const [selected, setSelected] = useState(params.get("task") || "");
  const [history, setHistory] = useState<Update[]>([]);
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [agent, setAgent] = useState("");
  const [title, setTitle] = useState("");
  const [newFolder, setNewFolder] = useState(params.get("folder") || "");
  const [newStream, setNewStream] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const streams = useMemo(() => [...new Set(tasks.map(t => t.workstream).filter(Boolean))].sort(), [tasks]);
  const visible = tasks.filter(t => (!folder || t.folder.toLowerCase().includes(folder.toLowerCase())) &&
    (!stream || t.workstream === stream) && (showDone || !["done", "cancelled"].includes(t.status)));
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setHistoryLoading(true); setHistoryError(""); setHistory([]);
    fetch(`/api/progress/${encodeURIComponent(selected)}/history`).then(async r => {
      if (!r.ok) throw new Error(`History unavailable (${r.status})`);
      return r.json() as Promise<Update[]>;
    }).then(rows => { if (!cancelled) setHistory(rows); })
      .catch(e => { if (!cancelled) setHistoryError(String(e)); })
      .finally(() => { if (!cancelled) setHistoryLoading(false); });
    return () => { cancelled = true; };
  }, [selected, tasks]);
  async function addBacklog(event: React.FormEvent) {
    event.preventDefault(); setSaving(true); setSaveError("");
    try {
      const response = await fetch("/api/progress", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folder: newFolder, agent_id: agent, status: "queued", summary: title, workstream: newStream || null }) });
      if (!response.ok) throw new Error(await response.text());
      setTitle(""); onChange();
    } catch (e) { setSaveError(String(e)); }
    finally { setSaving(false); }
  }
  return <section id="progress" style={{ order: 0, gridColumn: "1 / -1" }}>
    <h2>Folder progress &amp; backlog</h2>
    <p className="muted">One current task per folder. Queue future work by workstream; completed tasks stay in history.</p>
    <div className="progress-filters">
      <label>Folder <input value={folder} onChange={e => setFolder(e.target.value)} placeholder="All folders" /></label>
      <label>Workstream <select value={stream} onChange={e => setStream(e.target.value)}><option value="">All workstreams</option>
        {streams.map(s => <option key={s} value={s}>{s}</option>)}</select></label>
      <label><input type="checkbox" checked={showDone} onChange={e => setShowDone(e.target.checked)} /> Include completed</label>
      <button onClick={onRefresh} disabled={loading}>Refresh</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {loading && !tasks.length && <p role="status">Loading progress…</p>}
    {!loading && !error && !visible.length && <p>No tasks match. Add backlog below or use <code>partyline progress start</code> in a working folder.</p>}
    <div style={{ overflowX: "auto" }}><table className="agent-grid"><thead><tr>
      <th>Folder / task</th><th>Status</th><th>Workstream</th><th>Agent / branch</th><th>Latest progress</th><th>Updated</th>
    </tr></thead><tbody>{visible.map(t => <tr key={t.id}>
      <td><a href={taskLink(t)} onClick={e => { e.preventDefault(); window.history.replaceState(null, "", taskLink(t)); setSelected(t.id); }}>
        {t.folder}: {t.title}</a>{t.issue_ref && <div className="muted">{t.issue_ref}</div>}</td>
      <td>{t.status}</td><td>{t.workstream || "—"}</td><td>{t.agent_id}<div className="muted">{t.branch || "—"}</div></td>
      <td>{t.summary}</td><td>{new Date(t.updated_at).toLocaleString()}</td>
    </tr>)}</tbody></table></div>
    {selected && <div>
      <h3>Task history <button onClick={() => setSelected("")}>Close</button></h3>
      {historyLoading && <p role="status">Loading history…</p>}
      {historyError && <p role="alert">{historyError}</p>}
      {!historyLoading && !historyError && !history.length && <p>No history found for this task.</p>}
      {history.map(h => <details key={h.operation_id}><summary>{new Date(h.created_at).toLocaleString()} · {h.agent_id} · {h.status}: {h.summary}</summary>
        <p style={{ whiteSpace: "pre-wrap" }}>{h.details || "No additional details."}</p></details>)}
    </div>}
    <details><summary>Add to backlog</summary><form onSubmit={addBacklog} className="progress-filters">
      <label>Folder <input required value={newFolder} onChange={e => setNewFolder(e.target.value)} placeholder="aura_1" /></label>
      <label>Task <input required maxLength={500} value={title} onChange={e => setTitle(e.target.value)} /></label>
      <label>Workstream <input value={newStream} onChange={e => setNewStream(e.target.value)} /></label>
      <label>Agent <select required value={agent} onChange={e => setAgent(e.target.value)}><option value="">Choose registered agent</option>
        {agents.map(a => <option key={a} value={a}>{a}</option>)}</select></label>
      <button disabled={saving}>{saving ? "Adding…" : "Add task"}</button>
      {saveError && <p role="alert">{saveError}</p>}
    </form></details>
  </section>;
}
