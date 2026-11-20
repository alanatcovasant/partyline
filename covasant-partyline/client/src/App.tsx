import { useState } from "react";
import { ConnectAgent } from "./components/ConnectAgent";
import { AgentsPanel, ClaimsPanel, DiagnosticsPanel, MetricsPanel } from "./components/DashboardPanels";
import { MessageStream } from "./components/MessageStream";
import { Overview } from "./components/Overview";
import { ProgressBoard } from "./ProgressBoard";
import { initialView, usePartyline } from "./usePartyline";

export function App() {
  const [view, setView] = useState<"overview" | "dashboard" | "connect">(() => new URLSearchParams(window.location.search).has("task") ? "dashboard" : initialView());
  const {
    tasks, progressError, progressLoading, refreshProgress,
    messages, agents, claims, metrics, diag, health,
    connected, lastEventAt, sseReconnects, nowMs,
    pollMs, setPollMs,
  } = usePartyline();

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
          <DiagnosticsPanel health={health} connected={connected} lastEventAt={lastEventAt}
            nowMs={nowMs} metrics={metrics} diag={diag} tasks={tasks} sseReconnects={sseReconnects} />
          <MetricsPanel metrics={metrics} />
          <AgentsPanel agents={agents} nowMs={nowMs} />
          <ClaimsPanel claims={claims} />
      </div>
      <MessageStream agents={agents} messages={messages} />
      </>}
    </div>
  );
}
