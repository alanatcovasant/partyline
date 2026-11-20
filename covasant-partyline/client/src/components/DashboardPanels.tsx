import { taskLink, type Task } from "../ProgressBoard";
import { fmtAge } from "../lib";
import { ONLINE_WITHIN_SECS, type Agent, type Claim, type Diagnostics, type Health, type Metrics } from "../types";
import { ClaimTerritory } from "./ClaimTerritory";

export function DiagnosticsPanel({ health, connected, lastEventAt, nowMs, metrics, diag, tasks, sseReconnects }: {
  health: Health | null;
  connected: boolean;
  lastEventAt: number | null;
  nowMs: number;
  metrics: Metrics | null;
  diag: Diagnostics | null;
  tasks: Task[];
  sseReconnects: number;
}) {
  return (
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
  );
}

export function MetricsPanel({ metrics }: { metrics: Metrics | null }) {
  return (
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
  );
}

export function AgentsPanel({ agents, nowMs }: { agents: Agent[]; nowMs: number }) {
  return (
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
  );
}

export function ClaimsPanel({ claims }: { claims: Claim[] }) {
  return (
    <section style={{ order: 2 }}>
      <h2>Active claims ({claims.length})</h2>
      <ClaimTerritory claims={claims} />
    </section>
  );
}
