import { Fragment, useEffect, useMemo, useState } from "react";

export type VqItem = {
  id: string;
  issue_ref: string;
  target: string;
  script?: string | null;
  proc?: string | null;
  pr?: string | null;
  enqueued_by: string;
  created_at: string;
  claimed_by?: string | null;
  last_exit_code?: number | null;
  last_executed?: boolean | null;
  last_commit?: string | null;
  last_run_at?: string | null;
  state: "pending" | "claimed" | "validated" | "self_reported" | "failed" | "inconclusive";
};

export type VqRun = {
  id: number;
  agent_id: string;
  exit_code: number;
  executed: boolean;
  commit?: string | null;
  output_tail?: string | null;
  created_at: string;
};

export type VqCounts = {
  total: number;
  validated: number;
  pending: number;
  inconclusive: number;
  failed: number;
  self_reported: number;
  claimed: number;
};

export function ValidationQueue({ onCountChange }: { onCountChange?: (counts: VqCounts) => void }) {
  const params = new URLSearchParams(window.location.search);
  const [items, setItems] = useState<VqItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [targetFilter, setTargetFilter] = useState(params.get("vq_target") || "");
  const [stateFilter, setStateFilter] = useState(params.get("vq_state") || "");
  const [query, setQuery] = useState("");
  const [selectedItem, setSelectedItem] = useState<{ issue_ref: string; target: string } | null>(null);
  const [itemDetails, setItemDetails] = useState<(VqItem & { runs?: VqRun[] }) | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const [copiedCmd, setCopiedCmd] = useState<string | null>(null);

  const [page, setPage] = useState(0);
  const pageSize = 25;

  useEffect(() => {
    setPage(0);
  }, [targetFilter, stateFilter, query]);

  const refreshItems = async () => {
    setLoading(true);
    setError("");
    try {
      const q = new URLSearchParams();
      q.set("limit", "1000");
      if (targetFilter) q.set("target", targetFilter);
      const res = await fetch(`/api/vq/items?${q.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      const data: VqItem[] = await res.json();
      setItems(data);
      const counts: VqCounts = {
        total: data.length,
        validated: 0,
        pending: 0,
        inconclusive: 0,
        failed: 0,
        self_reported: 0,
        claimed: 0,
      };
      for (const i of data) {
        if (i.state === "validated") counts.validated++;
        else if (i.state === "failed") counts.failed++;
        else if (i.state === "inconclusive") counts.inconclusive++;
        else if (i.state === "pending") counts.pending++;
        else if (i.state === "self_reported") counts.self_reported++;
        else if (i.state === "claimed") counts.claimed++;
      }
      onCountChange?.(counts);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refreshItems();
  }, [targetFilter]);

  const targets = useMemo(() => {
    const set = new Set<string>();
    items.forEach((it) => {
      if (it.target) set.add(it.target);
    });
    return Array.from(set).sort();
  }, [items]);

  const filteredItems = useMemo(() => {
    return items.filter((it) => {
      if (stateFilter && it.state !== stateFilter) return false;
      if (query && !it.issue_ref.toLowerCase().includes(query.toLowerCase()) && !it.enqueued_by.toLowerCase().includes(query.toLowerCase())) {
        return false;
      }
      return true;
    });
  }, [items, stateFilter, query]);

  const pageCount = Math.max(1, Math.ceil(filteredItems.length / pageSize));
  const curPage = Math.min(page, pageCount - 1);
  const pagedItems = useMemo(() => {
    return filteredItems.slice(curPage * pageSize, curPage * pageSize + pageSize);
  }, [filteredItems, curPage, pageSize]);

  useEffect(() => {
    if (!selectedItem) {
      setItemDetails(null);
      return;
    }
    let cancelled = false;
    setDetailsLoading(true);
    setDetailsError("");
    const q = new URLSearchParams({ issue_ref: selectedItem.issue_ref, target: selectedItem.target });
    fetch(`/api/vq/show?${q.toString()}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
        return r.json() as Promise<VqItem & { runs?: VqRun[] }>;
      })
      .then((data) => {
        if (!cancelled) setItemDetails(data);
      })
      .catch((e) => {
        if (!cancelled) setDetailsError(String(e));
      })
      .finally(() => {
        if (!cancelled) setDetailsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedItem]);

  function copyText(text: string, label: string) {
    navigator.clipboard.writeText(text);
    setCopiedCmd(label);
    setTimeout(() => setCopiedCmd(null), 2000);
  }

  function renderStateBadge(state: VqItem["state"], executed?: boolean | null) {
    switch (state) {
      case "validated":
        return (
          <span className="tag t-done" title="Passed executed check on target commit">
            ✓ Validated {executed && <span style={{ opacity: 0.85, fontSize: "0.85em" }}>(exec)</span>}
          </span>
        );
      case "self_reported":
        return <span className="tag t-warn" title="Reported pass, but not captured by runner execution">⚠ Self-reported</span>;
      case "failed":
        return <span className="tag t-blocked" title="Last run failed (exit code 1)">✕ Failed</span>;
      case "inconclusive":
        return <span className="tag t-claim" title="Last run inconclusive or timed out">? Inconclusive</span>;
      case "claimed":
        return <span className="tag" style={{ background: "#7c3aed" }} title="Currently claimed by validator">Claimed</span>;
      case "pending":
      default:
        return <span className="tag opt" title="Awaiting validation on deployed target">Pending</span>;
    }
  }

  const counts = useMemo(() => {
    const c: VqCounts = {
      total: items.length,
      validated: 0,
      self_reported: 0,
      failed: 0,
      inconclusive: 0,
      pending: 0,
      claimed: 0,
    };
    for (const i of items) {
      if (i.state === "validated") c.validated++;
      else if (i.state === "failed") c.failed++;
      else if (i.state === "inconclusive") c.inconclusive++;
      else if (i.state === "pending") c.pending++;
      else if (i.state === "self_reported") c.self_reported++;
      else if (i.state === "claimed") c.claimed++;
    }
    return c;
  }, [items]);

  return (
    <section className="vq-panel">
      <div className="section-header-row">
        <div>
          <h2>🧪 Validation Queue</h2>
          <p className="muted" style={{ margin: "0.2rem 0 0" }}>
            Code-complete fixes awaiting deployed verification. State is derived from executed runs, not typed claims.
          </p>
        </div>
        <div className="vq-counts-strip">
          <button
            type="button"
            className={`badge-pill ${stateFilter === "" ? "active" : ""}`}
            onClick={() => setStateFilter("")}
            title="Show all validation items"
          >
            <b>{counts.total}</b> total
          </button>
          <button
            type="button"
            className={`badge-pill pill-valid ${stateFilter === "validated" ? "active" : ""}`}
            onClick={() => setStateFilter(stateFilter === "validated" ? "" : "validated")}
            title="Filter to verified executed passes"
          >
            ✓ <b>{counts.validated}</b> valid
          </button>
          {counts.failed > 0 && (
            <button
              type="button"
              className={`badge-pill pill-fail ${stateFilter === "failed" ? "active" : ""}`}
              onClick={() => setStateFilter(stateFilter === "failed" ? "" : "failed")}
              title="Filter to failed runs (exit code 1)"
            >
              ✕ <b>{counts.failed}</b> failed
            </button>
          )}
          {counts.inconclusive > 0 && (
            <button
              type="button"
              className={`badge-pill pill-inconcl ${stateFilter === "inconclusive" ? "active" : ""}`}
              onClick={() => setStateFilter(stateFilter === "inconclusive" ? "" : "inconclusive")}
              title="Filter to inconclusive runs (exit code 2 / timeout)"
            >
              ? <b>{counts.inconclusive}</b> inconclusive
            </button>
          )}
          {counts.self_reported > 0 && (
            <button
              type="button"
              className={`badge-pill pill-selfrep ${stateFilter === "self_reported" ? "active" : ""}`}
              onClick={() => setStateFilter(stateFilter === "self_reported" ? "" : "self_reported")}
              title="Filter to self-reported passes (not runner-executed)"
            >
              ⚠ <b>{counts.self_reported}</b> self-reported
            </button>
          )}
          <button
            type="button"
            className={`badge-pill pill-pend ${stateFilter === "pending" ? "active" : ""}`}
            onClick={() => setStateFilter(stateFilter === "pending" ? "" : "pending")}
            title="Filter to pending items awaiting validation"
          >
            <b>{counts.pending}</b> pending
          </button>
          {counts.claimed > 0 && (
            <button
              type="button"
              className={`badge-pill pill-claimed ${stateFilter === "claimed" ? "active" : ""}`}
              onClick={() => setStateFilter(stateFilter === "claimed" ? "" : "claimed")}
              title="Filter to items currently claimed by an agent"
            >
              <b>{counts.claimed}</b> claimed
            </button>
          )}
        </div>
      </div>

      <div className="progress-filters">
        <label>
          Target:{" "}
          <select value={targetFilter} onChange={(e) => setTargetFilter(e.target.value)}>
            <option value="">All targets</option>
            {targets.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </label>
        <label>
          Status:{" "}
          <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
            <option value="">All states ({counts.total})</option>
            <option value="validated">✓ Validated ({counts.validated})</option>
            <option value="failed">✕ Failed ({counts.failed})</option>
            <option value="inconclusive">? Inconclusive ({counts.inconclusive})</option>
            <option value="self_reported">⚠ Self-reported ({counts.self_reported})</option>
            <option value="pending">Pending ({counts.pending})</option>
            <option value="claimed">Claimed ({counts.claimed})</option>
          </select>
        </label>
        <label>
          Search:{" "}
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Issue or author..."
            style={{ minWidth: "180px" }}
          />
        </label>
        <button type="button" onClick={refreshItems} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error && <p role="alert" style={{ color: "#dc2626" }}>{error}</p>}

      {!loading && !error && filteredItems.length === 0 && (
        <p className="muted" style={{ padding: "1rem 0" }}>
          No validation items match the filter. Enqueue an item with <code>partyline vq enqueue --issue owner/repo#N --target dev-eastus2</code>.
        </p>
      )}

      {filteredItems.length > 0 && (
        <>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "0.5rem 0", flexWrap: "wrap", gap: "0.5rem" }}>
            <span className="muted">
              Showing {curPage * pageSize + 1}–{Math.min(filteredItems.length, (curPage + 1) * pageSize)} of {filteredItems.length} items
            </span>
            {pageCount > 1 && (
              <div className="pager" style={{ margin: 0, padding: 0 }}>
                <button type="button" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={curPage === 0}>
                  ← Prev
                </button>
                <span className="muted">
                  Page {curPage + 1} of {pageCount}
                </span>
                <button type="button" onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))} disabled={curPage >= pageCount - 1}>
                  Next →
                </button>
              </div>
            )}
          </div>

          <div style={{ overflowX: "auto" }}>
            <table className="agent-grid vq-table">
              <thead>
                <tr>
                  <th>Issue / PR</th>
                  <th>Target</th>
                  <th>Status</th>
                  <th>Claimed By</th>
                  <th>Script / Procedure</th>
                  <th>Last Run / Commit</th>
                  <th style={{ minWidth: "140px" }}>Action</th>
                </tr>
              </thead>
              <tbody>
                {pagedItems.map((it) => {
                  const isSelected = selectedItem?.issue_ref === it.issue_ref && selectedItem?.target === it.target;
                  return (
                    <Fragment key={`${it.issue_ref}-${it.target}`}>
                      <tr className={isSelected ? "row-selected" : undefined}>
                        <td>
                          <b>{it.issue_ref}</b>
                          {it.pr && (
                            <div>
                              <a href={it.pr} target="_blank" rel="noopener noreferrer" className="muted" style={{ fontSize: "0.85em" }}>
                                PR link ↗
                              </a>
                            </div>
                          )}
                          <div className="muted" style={{ fontSize: "0.78em" }}>by {it.enqueued_by}</div>
                        </td>
                        <td>
                          <code className="mono">{it.target}</code>
                        </td>
                        <td>{renderStateBadge(it.state, it.last_executed)}</td>
                        <td>
                          {it.claimed_by ? (
                            <span style={{ color: "#7c3aed", fontWeight: 500 }}>{it.claimed_by}</span>
                          ) : (
                            <span className="muted">unclaimed</span>
                          )}
                        </td>
                        <td>
                          {it.script ? (
                            <div style={{ display: "flex", alignItems: "center", gap: "0.3rem" }}>
                              <code title={it.script} style={{ maxWidth: "20ch" }}>{it.script}</code>
                              <button
                                type="button"
                                className="btn-tiny"
                                onClick={() => copyText(it.script!, `script-${it.id}`)}
                                title="Copy script command"
                              >
                                {copiedCmd === `script-${it.id}` ? "✓" : "copy"}
                              </button>
                            </div>
                          ) : (
                            <span className="muted">—</span>
                          )}
                          {it.proc && (
                            <div style={{ fontSize: "0.8em" }}>
                              <span className="muted">proc: </span>
                              <span title={it.proc}>{it.proc.split("/").pop()}</span>
                            </div>
                          )}
                        </td>
                        <td>
                          {it.last_exit_code !== null && it.last_exit_code !== undefined ? (
                            <div>
                              <span className={`tag ${it.last_exit_code === 0 ? "t-done" : "t-blocked"}`} style={{ fontSize: "0.65rem", padding: "0 0.3rem" }}>
                                exit {it.last_exit_code}
                              </span>
                              {it.last_executed && <span className="muted" style={{ fontSize: "0.75em", marginLeft: "0.2rem" }}>(exec)</span>}
                            </div>
                          ) : (
                            <span className="muted">never run</span>
                          )}
                          {it.last_commit && (
                            <div className="muted mono" style={{ fontSize: "0.8em" }}>
                              {it.last_commit.substring(0, 8)}
                            </div>
                          )}
                        </td>
                        <td>
                          <div style={{ display: "flex", gap: "0.35rem", alignItems: "center" }}>
                            <button
                              type="button"
                              className={`btn-sm ${isSelected ? "active" : ""}`}
                              onClick={() => {
                                if (isSelected) setSelectedItem(null);
                                else setSelectedItem({ issue_ref: it.issue_ref, target: it.target });
                              }}
                              title="Toggle runs and execution evidence inline"
                            >
                              {isSelected ? "▲ Hide" : "▼ Runs"}
                            </button>
                            <button
                              type="button"
                              className="btn-tiny"
                              onClick={() => copyText(`partyline vq run --issue ${it.issue_ref} --target ${it.target}`, `run-${it.id}`)}
                              title="Copy CLI run command"
                            >
                              {copiedCmd === `run-${it.id}` ? "✓" : "run cmd"}
                            </button>
                          </div>
                        </td>
                      </tr>

                      {isSelected && (
                        <tr className="vq-inline-row">
                          <td colSpan={7} style={{ padding: "0.5rem 0.8rem", background: "rgba(127, 127, 127, 0.04)" }}>
                            <div className="vq-details-drawer" style={{ margin: "0.2rem 0" }}>
                              <div className="section-header-row" style={{ marginBottom: "0.5rem" }}>
                                <h3 style={{ margin: 0, fontSize: "0.95rem" }}>
                                  Evidence &amp; Runs: <code>{it.issue_ref}</code> on <code>{it.target}</code>
                                </h3>
                                <div style={{ display: "flex", gap: "0.4rem", alignItems: "center" }}>
                                  <button
                                    type="button"
                                    className="btn-tiny"
                                    onClick={() => copyText(`partyline vq run --issue ${it.issue_ref} --target ${it.target}`, `run-inner-${it.id}`)}
                                  >
                                    {copiedCmd === `run-inner-${it.id}` ? "✓ copied" : "copy run cmd"}
                                  </button>
                                  <button type="button" className="btn-sm" onClick={() => setSelectedItem(null)}>✕ Close</button>
                                </div>
                              </div>

                              {detailsLoading && <p className="muted" style={{ margin: "0.4rem 0" }}>Loading runs and evidence…</p>}
                              {detailsError && <p role="alert" style={{ color: "#dc2626" }}>{detailsError}</p>}

                              {itemDetails && (
                                <div>
                                  <div className="vq-details-summary">
                                    <div><b>Current State:</b> {renderStateBadge(itemDetails.state, itemDetails.last_executed)}</div>
                                    <div><b>Target:</b> <code className="mono">{itemDetails.target}</code></div>
                                    {itemDetails.script && (
                                      <div style={{ gridColumn: "1 / -1" }}>
                                        <b>Validation Command:</b>{" "}
                                        <code className="mono" style={{ display: "inline-block", background: "var(--bg)", padding: "0.2rem 0.5rem", borderRadius: "4px" }}>
                                          {itemDetails.script}
                                        </code>
                                      </div>
                                    )}
                                    {itemDetails.proc && <div><b>Procedure Doc:</b> <code>{itemDetails.proc}</code></div>}
                                    {itemDetails.pr && <div><b>PR:</b> <a href={itemDetails.pr} target="_blank" rel="noreferrer">{itemDetails.pr}</a></div>}
                                  </div>

                                  <h4 style={{ margin: "0.6rem 0 0.3rem" }}>Recorded Runs ({itemDetails.runs?.length || 0})</h4>
                                  {(!itemDetails.runs || itemDetails.runs.length === 0) && (
                                    <p className="muted" style={{ margin: "0.4rem 0" }}>
                                      No runs recorded yet. Run with <code>partyline vq run --issue {it.issue_ref} --target {it.target}</code>.
                                    </p>
                                  )}

                                  {itemDetails.runs?.map((r) => (
                                    <details key={r.id} className="vq-run-card" open={r.id === itemDetails.runs?.[0]?.id}>
                                      <summary className="vq-run-summary">
                                        <span className={`tag ${r.exit_code === 0 ? "t-done" : "t-blocked"}`}>
                                          Exit {r.exit_code} ({r.exit_code === 0 ? "PASS" : r.exit_code === 1 ? "FAIL" : "INCONCLUSIVE"})
                                        </span>
                                        <b>{r.agent_id}</b>
                                        <span className="muted">{r.executed ? "⚡ Executed by runner" : "✍ Self-reported"}</span>
                                        {r.commit && <span className="mono muted">@{r.commit.substring(0, 8)}</span>}
                                        <time className="muted" style={{ marginLeft: "auto" }}>{new Date(r.created_at).toLocaleString()}</time>
                                      </summary>
                                      <div className="vq-run-body">
                                        {r.output_tail ? (
                                          <pre className="vq-output-tail"><code>{r.output_tail}</code></pre>
                                        ) : (
                                          <p className="muted" style={{ margin: "0.4rem 0" }}>No output captured for this run.</p>
                                        )}
                                      </div>
                                    </details>
                                  ))}
                                </div>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>

          {pageCount > 1 && (
            <div className="pager" style={{ marginTop: "0.8rem" }}>
              <button type="button" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={curPage === 0}>
                ← Prev
              </button>
              <span className="muted">
                Page {curPage + 1} of {pageCount}
              </span>
              <button type="button" onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))} disabled={curPage >= pageCount - 1}>
                Next →
              </button>
            </div>
          )}
        </>
      )}

      <details style={{ marginTop: "1rem" }}>
        <summary className="muted" style={{ cursor: "pointer", fontSize: "0.85em" }}>
          💡 CLI cheat sheet for Validation Queue
        </summary>
        <div className="snippet" style={{ marginTop: "0.4rem" }}>
          <pre><code>{`# 1. Enqueue work awaiting validation on dev-eastus2:
partyline vq enqueue --issue owner/repo#123 --target dev-eastus2 --script "pytest tests/acceptance.py" --proc docs/validation/123.md

# 2. Claim next pending item atomically:
partyline vq next --target dev-eastus2

# 3. Execute validation script and capture exit code + deployed commit:
partyline vq run --issue owner/repo#123 --target dev-eastus2 --commit <sha>`}</code></pre>
        </div>
      </details>
    </section>
  );
}
