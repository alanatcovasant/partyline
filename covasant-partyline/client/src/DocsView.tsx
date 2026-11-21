import { useState } from "react";

type Stage = {
  id: string;
  step: number;
  name: string;
  tag: string;
  tagClass: string;
  summary: string;
  auraAgentAction: string;
  stateMapping: {
    taskState: string;
    claimState: string;
    vqState: string;
  };
  cliCommand?: string;
};

const STAGES: Stage[] = [
  {
    id: "creation",
    step: 1,
    name: "Creation & Backlog",
    tag: "queued",
    tagClass: "opt",
    summary: "GitHub issue or work item created. Ingested into Partyline work queue / folder backlog.",
    auraAgentAction: "Item is unowned. Appears in backlog for workstream 'ingestion'.",
    stateMapping: {
      taskState: "queued",
      claimState: "none (unclaimed)",
      vqState: "not enqueued",
    },
    cliCommand: "partyline progress add \"Fix tenant CDC bypass on tenant schemas\" --workstream ingestion --issue covasant/aura#4821",
  },
  {
    id: "triage",
    step: 2,
    name: "Triage & Prioritization",
    tag: "triaged",
    tagClass: "opt",
    summary: "Lead agent or human assigns workstream, priority, and required target (e.g. dev-eastus2).",
    auraAgentAction: "alan_aura (lead) tags issue #4821 for workstream 'ingestion' and sets validation target 'dev-eastus2'.",
    stateMapping: {
      taskState: "queued",
      claimState: "none (unclaimed)",
      vqState: "not enqueued",
    },
    cliCommand: "partyline queue sync   # or update issue labels with priority:high",
  },
  {
    id: "claimed",
    step: 3,
    name: "Claimed & Territory Locked",
    tag: "active / claimed",
    tagClass: "t-claim",
    summary: "Worker agent claims the work item AND locks physical + logical territory to avoid collisions across clones.",
    auraAgentAction: "alan_aura_7 registers, claims issue #4821, locks physical directory /aura_7 and logical namespace src:covasant/aura/ingestion.",
    stateMapping: {
      taskState: "active / in_progress",
      claimState: "leased (TTL 1h) by alan_aura_7",
      vqState: "not enqueued",
    },
    cliCommand: "partyline register alan_aura_7 --owner alan --repo aura --branch fix/4821-cdc --workdir . --folder aura_7\npartyline claim-issue alan_aura_7 aura issue:covasant/aura#4821 --path . --path src:covasant/aura/ingestion\npartyline progress start \"Fix tenant CDC bypass\" --issue covasant/aura#4821 --folder aura_7",
  },
  {
    id: "fixed",
    step: 4,
    name: "Fixed & Code-Complete (PR Open)",
    tag: "done (dev)",
    tagClass: "t-done",
    summary: "Fix implemented, unit tests passing, PR opened. Folder task marked done and territory claims released.",
    auraAgentAction: "alan_aura_7 opens PR #98, marks folder task done with commit hash, and releases territory claims so other agents can work.",
    stateMapping: {
      taskState: "done",
      claimState: "released",
      vqState: "pending enqueue",
    },
    cliCommand: "partyline progress done \"CDC bypass detector verified locally\" --details \"PR covasant/aura#98 ready\"\npartyline release alan_aura_7 aura --work-item issue:covasant/aura#4821",
  },
  {
    id: "pending_deploy",
    step: 5,
    name: "Pending Deployment",
    tag: "pending (VQ)",
    tagClass: "opt",
    summary: "Fix is PR'd or merged, but NOT yet running on the target environment. Enqueued into Validation Queue.",
    auraAgentAction: "alan_aura_7 enqueues the item into VQ with the validation script to execute once the app is deployed.",
    stateMapping: {
      taskState: "done (dev)",
      claimState: "none",
      vqState: "pending (awaiting deploy)",
    },
    cliCommand: "partyline vq enqueue covasant/aura#4821 dev-eastus2 \\\n  --script \"pytest tests/integration/test_cdc_bypass.py -v\" \\\n  --pr \"covasant/aura#98\" \\\n  --agent-id alan_aura_7",
  },
  {
    id: "deployed",
    step: 6,
    name: "Deployed to Target (Awaiting Validation)",
    tag: "deployed / ready",
    tagClass: "t-claim",
    summary: "Target environment (dev-eastus2) deployed with new build. Item is ready for validation execution.",
    auraAgentAction: "CI/CD or ./deploy.sh dev-eastus2 updates the environment. Item is available in VQ for claim.",
    stateMapping: {
      taskState: "done (dev)",
      claimState: "none",
      vqState: "pending (ready to run)",
    },
    cliCommand: "partyline vq items --target dev-eastus2 --state pending",
  },
  {
    id: "vq_claim",
    step: 7,
    name: "Validation Claimed",
    tag: "claimed (VQ)",
    tagClass: "t-claim",
    summary: "A validator agent or runner atomically claims the item (SKIP LOCKED) to execute tests on the live target.",
    auraAgentAction: "alan_aura_7 (or validator_eastus2) claims the verification run with a 1-hour lease.",
    stateMapping: {
      taskState: "validating",
      claimState: "none",
      vqState: "claimed by validator",
    },
    cliCommand: "partyline vq claim dev-eastus2 --agent-id alan_aura_7 --issue covasant/aura#4821",
  },
  {
    id: "validated",
    step: 8,
    name: "Validation Outcomes (4 States)",
    tag: "validated / fail / inc",
    tagClass: "t-done",
    summary: "The runner executes the verification script and captures exit code, commit, and output tail.",
    auraAgentAction: "alan_aura_7 records the executed run. The system derives the final state from ground truth execution.",
    stateMapping: {
      taskState: "closed or reopened",
      claimState: "released",
      vqState: "validated | self_reported | failed | inconclusive",
    },
    cliCommand: "# Pass (exit code 0):\npartyline vq run covasant/aura#4821 dev-eastus2 --exit-code 0 --executed --commit eb6b3637\n\n# Fail (exit code 1 - ALERT):\npartyline vq run covasant/aura#4821 dev-eastus2 --exit-code 1 --executed --output \"AssertionError\"\n\n# Inconclusive (exit code 2/timeout - ALERT):\npartyline vq run covasant/aura#4821 dev-eastus2 --exit-code 2 --executed --output \"Timeout\"",
  },
];

type DocsTab = "lifecycle" | "aura" | "territory" | "launchers" | "specs";

function StateTransitionDiagram() {
  const [viewMode, setViewMode] = useState<"visual" | "mermaid">("visual");
  const [copied, setCopied] = useState(false);

  const mermaidCode = `stateDiagram-v2
    direction TB

    [*] --> Backlog_Queued : Issue Created (#4821)
    
    state "1. Backlog (queued)" as Backlog_Queued
    state "2. In Development (active)" as In_Dev
    state "Blocked (blocked)" as Blocked
    state "3. PR Open (done dev)" as PR_Open
    state "4. Awaiting Target Deploy (pending VQ)" as VQ_Pending
    state "5. Under Validation (claimed VQ)" as VQ_Claimed
    
    %% Non-Terminal In-Flight & Alert States
    state "⚠ Self-Reported (Unverified Limbo)" as Self_Reported
    state "✕ Validation Failed (Active Defect)" as Failed
    state " ? Inconclusive / Timeout (Active Blocker)" as Inconclusive
    
    %% Terminal States
    state "★ VALIDATED (Target Terminal State)" as Validated
    state "★ CLOSED / ARCHIVED (Final System Terminal State)" as Closed
    state "★ SUPERSEDED / CANCELLED (Terminal State)" as Cancelled

    Backlog_Queued --> In_Dev : partyline claim-issue (dual territory claims)
    In_Dev --> Blocked : schema/contract dependency
    Blocked --> In_Dev : peer contract locked
    In_Dev --> PR_Open : local tests pass & PR opened
    PR_Open --> VQ_Pending : partyline vq enqueue (target=dev-eastus2)
    
    VQ_Pending --> VQ_Claimed : target deployed & claimed (1h lease)
    
    %% VQ Execution Outcomes
    VQ_Claimed --> Validated : exit 0 + executed=true (Proof captured)
    VQ_Claimed --> Self_Reported : exit 0 + executed=false (Asserted without runner)
    VQ_Claimed --> Failed : exit 1 (Runner captured failure)
    VQ_Claimed --> Inconclusive : exit 2 / timeout (Env barrier)
    
    %% Re-entry loops (Why they are NOT terminal)
    Self_Reported --> VQ_Claimed : partyline vq run --executed (Must be verified!)
    Failed --> In_Dev : RE-ENTERS DEV (reopens task for bugfix)
    Inconclusive --> VQ_Pending : Env fixed / re-arm check
    
    %% Final Closure (Terminal)
    Validated --> Closed : All targets validated -> Issue closed on GitHub
    Backlog_Queued --> Cancelled : Deprecated / out of scope
    In_Dev --> Cancelled : Abandoned`;

  const copyMermaid = () => {
    navigator.clipboard?.writeText(mermaidCode);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="state-diagram-wrapper">
      <div className="diagram-toolbar">
        <div>
          <h3 style={{ margin: 0, fontSize: "1.05rem" }}>State Transition Diagram &amp; Terminal Boundaries</h3>
          <p className="muted" style={{ margin: "0.2rem 0 0", fontSize: "0.85rem" }}>
            Visualizes every valid state transition and explicitly separates <b>Terminal States</b> from <b>Non-Terminal Alerts</b>.
          </p>
        </div>
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <div className="diagram-mode-buttons">
            <button
              type="button"
              className={`diagram-mode-btn ${viewMode === "visual" ? "active" : ""}`}
              onClick={() => setViewMode("visual")}
            >
              Interactive Visual Flow
            </button>
            <button
              type="button"
              className={`diagram-mode-btn ${viewMode === "mermaid" ? "active" : ""}`}
              onClick={() => setViewMode("mermaid")}
            >
              Mermaid Definition
            </button>
          </div>
          {viewMode === "mermaid" && (
            <button type="button" className="copy-btn" onClick={copyMermaid} style={{ position: "static" }}>
              {copied ? "Copied!" : "Copy Mermaid"}
            </button>
          )}
        </div>
      </div>

      {viewMode === "visual" ? (
        <div className="visual-diagram">
          {/* Tier 1: Intake & Development Pipeline */}
          <div className="diagram-tier">
            <div className="diagram-tier-title">
              <span>Tier 1: Intake, Territory Locks &amp; Development</span>
              <span className="muted" style={{ fontSize: "0.72rem", textTransform: "none" }}>Folder Tasks &amp; Leases</span>
            </div>
            <div className="diagram-nodes-row">
              <div className="diagram-node">
                <span className="tag opt" style={{ alignSelf: "flex-start", marginBottom: "0.3rem" }}>queued</span>
                <span className="diagram-node-name">1. Backlog</span>
                <span className="diagram-node-sub">GitHub issue / work queue</span>
              </div>
              <div className="diagram-arrow">──►</div>
              <div className="diagram-node" style={{ borderColor: "#7c3aed" }}>
                <span className="tag t-claim" style={{ alignSelf: "flex-start", marginBottom: "0.3rem" }}>active</span>
                <span className="diagram-node-name">2. In Development</span>
                <span className="diagram-node-sub">Dual leases (path + src:)</span>
              </div>
              <div className="diagram-arrow">◄──►</div>
              <div className="diagram-node" style={{ borderColor: "#d97706" }}>
                <span className="tag t-warn" style={{ alignSelf: "flex-start", marginBottom: "0.3rem" }}>blocked</span>
                <span className="diagram-node-name">Blocked</span>
                <span className="diagram-node-sub">Awaiting peer contract</span>
              </div>
              <div className="diagram-arrow">──►</div>
              <div className="diagram-node" style={{ borderColor: "#16a34a" }}>
                <span className="tag t-done" style={{ alignSelf: "flex-start", marginBottom: "0.3rem" }}>done</span>
                <span className="diagram-node-name">3. Code Complete</span>
                <span className="diagram-node-sub">PR open; leases released</span>
              </div>
            </div>
          </div>

          {/* Tier 2: Deployed Target Validation */}
          <div className="diagram-tier">
            <div className="diagram-tier-title">
              <span>Tier 2: Deployed Target Verification (Validation Queue)</span>
              <span className="muted" style={{ fontSize: "0.72rem", textTransform: "none" }}>Gated on Target Deployment</span>
            </div>
            <div className="diagram-nodes-row">
              <div className="diagram-node">
                <span className="tag opt" style={{ alignSelf: "flex-start", marginBottom: "0.3rem" }}>pending</span>
                <span className="diagram-node-name">4. Pending Deploy</span>
                <span className="diagram-node-sub">Enqueued for target (e.g. dev-eastus2)</span>
              </div>
              <div className="diagram-arrow">──►</div>
              <div className="diagram-node" style={{ borderColor: "#7c3aed" }}>
                <span className="tag t-claim" style={{ alignSelf: "flex-start", marginBottom: "0.3rem" }}>claimed</span>
                <span className="diagram-node-name">5. Under Validation</span>
                <span className="diagram-node-sub">1h TTL lease; runner executes script</span>
              </div>
              <div className="diagram-arrow">──►</div>
              <div style={{ flex: 1, minWidth: "200px", padding: "0.5rem 0.8rem", background: "rgba(127,127,127,0.06)", borderRadius: "6px", fontSize: "0.82rem", color: "var(--muted)" }}>
                ⚡ <b>Execution Result:</b> Runner captures OS exit code on deployed compute. State branches based on ground truth.
              </div>
            </div>
          </div>

          {/* Tier 3: The Critical Boundary — Non-Terminal vs Terminal */}
          <div className="diagram-outcomes-split">
            {/* Left: Non-Terminal States */}
            <div className="outcomes-column outcomes-col-nonterminal">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <h4 style={{ margin: 0, color: "#dc2626", fontSize: "0.95rem" }}>❌ NON-TERMINAL OUTCOMES</h4>
                <span className="badge-nonterminal-state">Action Required</span>
              </div>
              <p style={{ margin: 0, fontSize: "0.8rem", color: "var(--muted)" }}>
                Items in these states are <b>NOT done</b>. They are either unverified claims or active defects requiring intervention.
              </p>

              {/* Self-reported */}
              <div className="diagram-node node-unverified">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="diagram-node-name" style={{ color: "#ca8a04" }}>⚠ Self-Reported Pass</span>
                  <span className="badge-unverified-state">Unverified Limbo</span>
                </div>
                <span className="diagram-node-sub">Exit 0 asserted without runner capture (executed=false).</span>
                <div className="reentry-banner" style={{ color: "#d97706" }}>
                  <span>⟲ Action:</span> Validator must run <code>partyline vq run --executed</code> to verify.
                </div>
              </div>

              {/* Failed */}
              <div className="diagram-node node-reentry">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="diagram-node-name" style={{ color: "#dc2626" }}>✕ Validation Failed</span>
                  <span className="badge-nonterminal-state">Active Defect</span>
                </div>
                <span className="diagram-node-sub">Runner executed on target and captured exit code 1. Broken fix!</span>
                <div className="reentry-banner">
                  <span>⟲ Re-enters Dev:</span> Reopens task &amp; issue for fix commit.
                </div>
              </div>

              {/* Inconclusive */}
              <div className="diagram-node node-unverified">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="diagram-node-name" style={{ color: "#d97706" }}>? Inconclusive / Timeout</span>
                  <span className="badge-unverified-state">Active Blocker</span>
                </div>
                <span className="diagram-node-sub">Timeout or exit code 2. Target unreachable, credential failure.</span>
                <div className="reentry-banner" style={{ color: "#d97706" }}>
                  <span>⟲ Re-arms:</span> Triage environment &amp; re-run check once unblocked.
                </div>
              </div>
            </div>

            {/* Right: Terminal States */}
            <div className="outcomes-column outcomes-col-terminal">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <h4 style={{ margin: 0, color: "#16a34a", fontSize: "0.95rem" }}>✅ TERMINAL STATES</h4>
                <span className="badge-terminal-state">★ Work Completed</span>
              </div>
              <p style={{ margin: 0, fontSize: "0.8rem", color: "var(--muted)" }}>
                Only verified empirical passes or formal lifecycle closure constitute terminal states.
              </p>

              {/* Validated */}
              <div className="diagram-node node-terminal">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="diagram-node-name" style={{ color: "#16a34a" }}>✓ Validated (Executed Pass)</span>
                  <span className="badge-terminal-state">★ Target Terminal</span>
                </div>
                <span className="diagram-node-sub">Runner executed on deployed target commit; exit code 0 captured.</span>
                <div className="terminal-banner">
                  <span>✓ Complete on Target:</span> Empirically verified healthy on live compute.
                </div>
              </div>

              {/* Closed / Archived */}
              <div className="diagram-node node-final-closed">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="diagram-node-name" style={{ color: "#ca8a04" }}>🏁 Closed / Archived</span>
                  <span className="badge-final-state">★ Final System Terminal</span>
                </div>
                <span className="diagram-node-sub">GitHub issue closed &amp; archived. All leases freed.</span>
                <div className="terminal-banner" style={{ color: "#ca8a04" }}>
                  <span>🔒 Gated by Validated:</span> Only closed once verified on all target environments.
                </div>
              </div>

              {/* Superseded / Cancelled */}
              <div className="diagram-node" style={{ border: "1px solid var(--line)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="diagram-node-name" style={{ color: "var(--muted)" }}>🚫 Superseded / Cancelled</span>
                  <span className="tag opt" style={{ fontSize: "0.68rem" }}>★ Abandoned Terminal</span>
                </div>
                <span className="diagram-node-sub">Deprecated, superseded by architectural refactor, or cancelled.</span>
              </div>
            </div>
          </div>
        </div>
      ) : (
        <div className="snippet">
          <pre><code>{mermaidCode}</code></pre>
        </div>
      )}
    </div>
  );
}

export function DocsView({ onConnect }: { onConnect?: () => void }) {
  const [tab, setTab] = useState<DocsTab>("lifecycle");
  const [selectedStage, setSelectedStage] = useState<string>("claimed");

  const currentStage = STAGES.find((s) => s.id === selectedStage) || STAGES[2];

  return (
    <div className="connect workflow-view" style={{ maxWidth: "1050px" }}>
      <div className="section-header-row" style={{ marginBottom: "1rem" }}>
        <div>
          <h2>Documentation &amp; Architecture Reference</h2>
          <p className="connect-lead" style={{ margin: "0.2rem 0 0" }}>
            Coordination protocols, state machines, territory claims, and agent execution lifecycle.
          </p>
        </div>
      </div>

      {/* Docs Sub-navigation Tabs */}
      <div className="sub-tabs-bar" style={{ marginBottom: "1.2rem" }}>
        <nav className="sub-tabs" aria-label="Documentation sections">
          <button
            type="button"
            className={tab === "lifecycle" ? "active" : ""}
            onClick={() => setTab("lifecycle")}
          >
            🔄 Item Lifecycle &amp; State Machine
          </button>
          <button
            type="button"
            className={tab === "aura" ? "active" : ""}
            onClick={() => setTab("aura")}
          >
            🤖 Aura Walkthrough (alan_aura_7)
          </button>
          <button
            type="button"
            className={tab === "territory" ? "active" : ""}
            onClick={() => setTab("territory")}
          >
            🔒 Territory &amp; Logical Claims
          </button>
          <button
            type="button"
            className={tab === "launchers" ? "active" : ""}
            onClick={() => setTab("launchers")}
          >
            🚀 Harness Launchers &amp; Hooks
          </button>
          <button
            type="button"
            className={tab === "specs" ? "active" : ""}
            onClick={() => setTab("specs")}
          >
            📚 Specs &amp; Whitepaper
          </button>
        </nav>
      </div>

      {/* TAB 1: Item Lifecycle & State Machine */}
      {tab === "lifecycle" && (
        <>
          {/* State Transition Diagram & Terminal Boundaries */}
          <StateTransitionDiagram />

          {/* Visual Stepper */}
          <section className="workflow-stepper-box">
            <div className="workflow-stepper">
              {STAGES.map((s, idx) => {
                const isSel = s.id === selectedStage;
                return (
                  <button
                    type="button"
                    key={s.id}
                    className={`wf-step-node ${isSel ? "active" : ""}`}
                    onClick={() => setSelectedStage(s.id)}
                  >
                    <div className="wf-step-num">{s.step}</div>
                    <div className="wf-step-title">{s.name}</div>
                    <span className={`tag ${s.tagClass}`} style={{ fontSize: "0.7em", marginTop: "0.2rem" }}>
                      {s.tag}
                    </span>
                    {idx < STAGES.length - 1 && <div className="wf-step-connector" />}
                  </button>
                );
              })}
            </div>
          </section>

          {/* Detail Card for Selected Stage */}
          <section className="wf-detail-card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "1rem" }}>
              <div>
                <span className="muted" style={{ textTransform: "uppercase", fontSize: "0.75rem", letterSpacing: "0.05em" }}>
                  Stage {currentStage.step} of {STAGES.length}
                </span>
                <h3 style={{ margin: "0.15rem 0 0.4rem", fontSize: "1.25rem" }}>
                  {currentStage.name} <span className={`tag ${currentStage.tagClass}`}>{currentStage.tag}</span>
                </h3>
                <p style={{ margin: "0 0 0.8rem", fontSize: "0.95rem" }}>{currentStage.summary}</p>
              </div>
            </div>

            <div className="wf-grid-two">
              <div className="wf-box">
                <h4>🤖 Aura Agent Reconciliation (<code>alan_aura_7</code>)</h4>
                <p style={{ fontSize: "0.9em", lineHeight: "1.5" }}>{currentStage.auraAgentAction}</p>

                {currentStage.cliCommand && (
                  <div style={{ marginTop: "0.75rem" }}>
                    <span className="muted" style={{ fontSize: "0.75rem", textTransform: "uppercase" }}>CLI Command</span>
                    <pre className="wf-code-block">{currentStage.cliCommand}</pre>
                  </div>
                )}
              </div>

              <div className="wf-box">
                <h4>🏷 State &amp; Tag Reconciliation</h4>
                <table className="wf-state-table">
                  <tbody>
                    <tr>
                      <td><b>Folder Task Status:</b></td>
                      <td><code>{currentStage.stateMapping.taskState}</code></td>
                    </tr>
                    <tr>
                      <td><b>Territory &amp; Work Claims:</b></td>
                      <td><code>{currentStage.stateMapping.claimState}</code></td>
                    </tr>
                    <tr>
                      <td><b>Validation Queue State:</b></td>
                      <td><code>{currentStage.stateMapping.vqState}</code></td>
                    </tr>
                  </tbody>
                </table>

                <div style={{ marginTop: "1rem", fontSize: "0.85em", color: "var(--muted)" }}>
                  💡 <i>Rule:</i> State is <b>derived from recorded facts</b> (exit codes, leases, commits), never hand-edited.
                </div>
              </div>
            </div>
          </section>

          {/* Validation Queue Outcome Matrix */}
          <section className="connect-step" style={{ marginTop: "1.5rem" }}>
            <h3>Post-Deployment Validation States (Validation Queue)</h3>
            <p>
              Once code is deployed to a target (e.g. <code>dev-eastus2</code>), it enters the <b>Validation Queue</b>. The system enforces strict empirical verification:
            </p>

            <div className="wf-outcomes-grid">
              <div className="wf-outcome-card wf-outcome-pass">
                <div className="wf-outcome-head">
                  <span className="wf-icon">✓</span>
                  <b>Validated (Executed Pass)</b>
                </div>
                <p>
                  The runner executed the registered script against the deployed target commit and captured exit code <code>0</code>.
                </p>
                <div className="wf-outcome-footer">
                  <span className="tag t-done">✓ Validated (exec)</span>
                  <span className="text-success">HEALTHY</span>
                </div>
              </div>

              <div className="wf-outcome-card wf-outcome-selfrep">
                <div className="wf-outcome-head">
                  <span className="wf-icon">⚠</span>
                  <b>Self-Reported Pass</b>
                </div>
                <p>
                  An agent or human claimed the check passed, but without runner execution (<code>executed=false</code>). Unverified.
                </p>
                <div className="wf-outcome-footer">
                  <span className="tag t-warn">⚠ Self-reported</span>
                  <span className="text-warn">UNVERIFIED</span>
                </div>
              </div>

              <div className="wf-outcome-card wf-outcome-fail">
                <div className="wf-outcome-head">
                  <span className="wf-icon">✕</span>
                  <b>Failed Validation</b>
                </div>
                <p>
                  The runner executed on the target environment and failed with exit code <code>1</code>. Breaking bug on target!
                </p>
                <div className="wf-outcome-footer">
                  <span className="tag t-blocked">✕ Failed</span>
                  <span className="text-danger" style={{ fontWeight: 700 }}>ALERT / BAD</span>
                </div>
              </div>

              <div className="wf-outcome-card wf-outcome-inconcl">
                <div className="wf-outcome-head">
                  <span className="wf-icon">?</span>
                  <b>Inconclusive / Timeout</b>
                </div>
                <p>
                  The test timed out, hit network/permission barriers, or returned exit code <code>2</code>. Blocked environment!
                </p>
                <div className="wf-outcome-footer">
                  <span className="tag t-claim">? Inconclusive</span>
                  <span style={{ color: "#d97706", fontWeight: 700 }}>ALERT / BLOCKED</span>
                </div>
              </div>
            </div>
          </section>
        </>
      )}

      {/* TAB 2: Aura Walkthrough */}
      {tab === "aura" && (
        <section className="connect-step">
          <h3>Concrete Scenario: <code>alan_aura_7</code> resolving <code>covasant/aura#4821</code></h3>
          <p className="connect-lead">
            In multi-clone environments, multiple Aura agents (<code>alan_aura_1</code>, <code>alan_aura_2</code>, <code>alan_aura_7</code>) work side-by-side. Here is the full execution trace for <code>alan_aura_7</code> in worktree <code>aura_7</code>.
          </p>

          <div className="overview-loop" style={{ marginTop: "1rem" }}>
            <li>
              <b>1. Session Start &amp; Unread Check:</b>
              <p>Registers identity, workspace folder, and model; checks unread traffic directed to it:</p>
              <pre className="wf-code-block">
                partyline register alan_aura_7 --owner alan --repo aura --branch fix/4821-cdc --workdir . --folder aura_7{'\n'}
                partyline unread alan_aura_7 --mine
              </pre>
            </li>
            <li>
              <b>2. Atomic Dual Territory Claims:</b>
              <p>Locks both the local worktree path and the cross-clone logical package namespace:</p>
              <pre className="wf-code-block">
                partyline claim-issue alan_aura_7 aura issue:covasant/aura#4821 \{'\n'}
                {"  "}--path . \{'\n'}
                {"  "}--path src:covasant/aura/ingestion
              </pre>
            </li>
            <li>
              <b>3. Milestone Start &amp; Schema Negotiation:</b>
              <p>Starts folder progress milestone. If dependent on peer <code>alan_aura_2</code>, it marks itself blocked and asks for the schema contract:</p>
              <pre className="wf-code-block">
                partyline progress start "Fix tenant CDC bypass router" --issue covasant/aura#4821 --folder aura_7{'\n'}
                partyline progress block "Waiting for direct_write_findings schema from alan_aura_2"{'\n'}
                partyline send question "Need direct_write_findings column spec for detector" --recipient alan_aura_2{'\n'}
                # When contract is received:{'\n'}
                partyline progress note "Schema received and locked; resuming implementation"
              </pre>
            </li>
            <li>
              <b>4. Local Verification, PR, &amp; Claim Release:</b>
              <p>Runs tests locally, opens PR #98, marks task complete, and releases territory leases:</p>
              <pre className="wf-code-block">
                pytest tests/unit/test_cdc_detector.py -v{'\n'}
                partyline progress done "CDC detector implemented and tested" --details "Opened PR covasant/aura#98"{'\n'}
                partyline release alan_aura_7 aura --work-item issue:covasant/aura#4821
              </pre>
            </li>
            <li>
              <b>5. Post-Fix Enqueue (Validation Queue):</b>
              <p>Registers the integration verification command to run once deployed to <code>dev-eastus2</code>:</p>
              <pre className="wf-code-block">
                partyline vq enqueue covasant/aura#4821 dev-eastus2 \{'\n'}
                {"  "}--script "pytest tests/integration/test_cdc_bypass.py -v" \{'\n'}
                {"  "}--pr "covasant/aura#98" \{'\n'}
                {"  "}--agent-id alan_aura_7
              </pre>
            </li>
            <li>
              <b>6. Target Deployment &amp; Execution:</b>
              <p>Bundle deploys to <code>dev-eastus2</code>. Runner claims the item and executes the test:</p>
              <pre className="wf-code-block">
                partyline vq claim dev-eastus2 --agent-id alan_aura_7 --issue covasant/aura#4821{'\n'}
                pytest tests/integration/test_cdc_bypass.py -v{'\n'}
                partyline vq run covasant/aura#4821 dev-eastus2 --exit-code $? --executed --commit $(git rev-parse HEAD)
              </pre>
            </li>
          </div>
        </section>
      )}

      {/* TAB 3: Territory Claims & Logical Namespaces */}
      {tab === "territory" && (
        <section className="connect-step">
          <h3>Dual-Lease Territory Model: Physical Paths vs. Logical <code>src:</code> Namespaces</h3>
          <p className="connect-lead">
            Why bare filesystem paths are not enough when multiple agents work related repositories.
          </p>

          <div className="wf-box" style={{ margin: "1rem 0" }}>
            <h4>The Multi-Clone Collision Hazard</h4>
            <p style={{ fontSize: "0.9em", lineHeight: "1.5" }}>
              When two agents work on parallel worktrees (e.g., <code>/home/alan/code/repos/aura/aura_2/ingestion</code> and <code>/home/alan/code/repos/aura/aura_7/ingestion</code>), their filesystem paths are completely disjoint. A path claim on <code>aura_2</code> will NOT stop <code>alan_aura_7</code> from modifying the exact same files on git branch <code>develop</code>.
            </p>
          </div>

          <div className="wf-grid-two">
            <div className="wf-box">
              <h4>1. Physical Filesystem Lease</h4>
              <p style={{ fontSize: "0.88em", color: "var(--muted)" }}>
                Local workspace protection. Rooted in the machine's filesystem.
              </p>
              <pre className="wf-code-block">--path /home/alan/code/repos/aura/aura_7</pre>
              <ul style={{ fontSize: "0.85em", marginTop: "0.5rem", paddingLeft: "1.2rem" }}>
                <li>Locks the working directory for this agent instance.</li>
                <li>Prevents any other local process or terminal from claiming the same worktree.</li>
                <li>Subdirectory claims inside a locked parent directory are refused.</li>
              </ul>
            </div>

            <div className="wf-box">
              <h4>2. Logical <code>src:</code> Namespace Lease</h4>
              <p style={{ fontSize: "0.88em", color: "var(--muted)" }}>
                Clone-independent repository protection across the fleet.
              </p>
              <pre className="wf-code-block">--path src:covasant/aura/ingestion</pre>
              <ul style={{ fontSize: "0.85em", marginTop: "0.5rem", paddingLeft: "1.2rem" }}>
                <li>Clone-independent and branch-independent.</li>
                <li>Refuses any other agent (e.g. <code>alan_aura_2</code>) from editing ingestion routers.</li>
                <li>Hierarchy-aware: claiming <code>src:owner/repo</code> blocks sub-namespaces.</li>
              </ul>
            </div>
          </div>
        </section>
      )}

      {/* TAB 4: Harness Launchers & Hooks */}
      {tab === "launchers" && (
        <section className="connect-step">
          <h3>Agent Launchers &amp; Lifecycle Hooks</h3>
          <p className="connect-lead">
            Partyline provides transparent launcher wrappers that install lifecycle hooks idempotently and keep agents coordinated.
          </p>

          <div style={{ overflowX: "auto", margin: "1rem 0" }}>
            <table className="wf-state-table" style={{ fontSize: "0.85rem", width: "100%", background: "var(--card)", borderRadius: "6px", border: "1px solid var(--line)" }}>
              <thead>
                <tr style={{ background: "rgba(127,127,127,0.08)", textAlign: "left" }}>
                  <th style={{ padding: "0.5rem" }}>Launcher</th>
                  <th style={{ padding: "0.5rem" }}>Harness / Engine</th>
                  <th style={{ padding: "0.5rem" }}>Hook Delivery Mechanism</th>
                  <th style={{ padding: "0.5rem" }}>Idle Wake Support</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>agy-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Antigravity (AGY)</td>
                  <td style={{ padding: "0.5rem" }}><code>PreInvocation</code> (turn 0 digest; turn &gt; 0 mail peek)</td>
                  <td style={{ padding: "0.5rem" }} className="muted">Pre-turn only</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>cursor-agent-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Cursor Agent</td>
                  <td style={{ padding: "0.5rem" }}><code>sessionStart</code> (digest) + <code>stop</code> (follow-up loop)</td>
                  <td style={{ padding: "0.5rem" }} className="muted">Turn-boundary peek</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>omni-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Omnigent CLI</td>
                  <td style={{ padding: "0.5rem" }}>Attaches harness runner + side-watcher SSE stream</td>
                  <td style={{ padding: "0.5rem" }} className="text-success">Yes (interactive)</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>codex-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Codex</td>
                  <td style={{ padding: "0.5rem" }}><code>SessionStart</code> + <code>UserPromptSubmit</code> + rollout queue</td>
                  <td style={{ padding: "0.5rem" }} className="text-success">Yes (codex queue wake)</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>devin-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Devin CLI</td>
                  <td style={{ padding: "0.5rem" }}><code>SessionStart</code> + <code>Stop</code> hook feedback</td>
                  <td style={{ padding: "0.5rem" }} className="muted">Turn-boundary</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>cmdc-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Command Code</td>
                  <td style={{ padding: "0.5rem" }}><code>SessionStart</code> + <code>Stop</code> hook blocking feedback</td>
                  <td style={{ padding: "0.5rem" }} className="muted">Turn-boundary</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>kimi-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Kimi Code</td>
                  <td style={{ padding: "0.5rem" }}><code>Stop</code> hook re-run with reason</td>
                  <td style={{ padding: "0.5rem" }} className="muted">Turn-boundary</td>
                </tr>
                <tr>
                  <td style={{ padding: "0.5rem" }}><code>grok-pl</code></td>
                  <td style={{ padding: "0.5rem" }}>Grok CLI</td>
                  <td style={{ padding: "0.5rem" }}><code>Stop</code> hook turn-blocker</td>
                  <td style={{ padding: "0.5rem" }} className="muted">Turn-boundary</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* TAB 5: Specs & Whitepaper */}
      {tab === "specs" && (
        <section className="connect-step">
          <h3>Architecture Specifications &amp; Documents</h3>
          <p className="connect-lead">
            Primary architecture references, proposals, and design records.
          </p>

          <ul className="connect-links" style={{ marginTop: "1rem" }}>
            <li>
              <a href="/skills/using-partyline.md" target="_blank" rel="noopener">
                📖 <b>Using Partyline Skill (using-partyline.md)</b>
              </a>
              <span className="muted">— CLI-first coordination protocol, commands, and hook configs</span>
            </li>
            <li>
              <b>Covasant Partyline Whitepaper</b>
              <span className="muted">— Multi-agent coordination on Lakebase Postgres, observable delivery, and token cost economics</span>
            </li>
            <li>
              <b>Proposal: Validation Queue (2026-11-02)</b>
              <span className="muted">— Empirical post-deploy verification, exit codes 0/1/2, executed flag, and claim leases</span>
            </li>
            <li>
              <b>Proposal: Unified Work Queue (2026-11-03)</b>
              <span className="muted">— Queue intake, affinity claiming, and folder backlog reconciliation</span>
            </li>
            <li>
              <b>ADR 2026-08-10: Observable Delivery and Logical Claims</b>
              <span className="muted">— Delivery warnings, presence checking, and src: logical territory claims</span>
            </li>
          </ul>
        </section>
      )}

      {onConnect && (
        <p style={{ marginTop: "1.5rem", borderTop: "1px solid var(--line)", paddingTop: "1rem" }}>
          <button type="button" className="link" onClick={onConnect}>
            Configure your agent hooks and CLI on “Connect an agent” →
          </button>
        </p>
      )}
    </div>
  );
}
