import { useCallback, useEffect, useRef, useState } from "react";
import { getJSON } from "./lib";
import type { Agent, Claim, Diagnostics, Health, Message, Metrics } from "./types";
import type { Task } from "./ProgressBoard";

// Landing view. A first-time visitor gets the Overview tour; everyone after that wants the
// live dashboard, which is what an operator actually opens this app for. Remembered per
// browser, so the tour happens once and the Overview tab stays available afterwards.
export const VISITED_KEY = "partyline.visited";

export function initialView(): "overview" | "dashboard" {
  try {
    return localStorage.getItem(VISITED_KEY) ? "dashboard" : "overview";
  } catch {
    return "dashboard"; // storage blocked (private mode): prefer the view people came for
  }
}

/** All live dashboard state: the message feed (SSE push + poll catch-up), agents,
 *  claims, metrics, diagnostics, health, and the operator's poll-interval preference.
 *  Split out of App so the component file only wires views together. */
export function usePartyline() {
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

  return {
    tasks, progressError, progressLoading, refreshProgress,
    messages, agents, claims, metrics, diag, health,
    connected, lastEventAt, sseReconnects, nowMs,
    pollMs, setPollMs,
  };
}
