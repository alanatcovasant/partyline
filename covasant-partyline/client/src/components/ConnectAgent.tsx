import { Snippet } from "./Snippet";

// Getting-started / onboarding view: how a new agent joins the line. URLs are derived from
// the live origin so the page is correct for whatever workspace this app is deployed in.
export function ConnectAgent() {
  const url = window.location.origin;
  const id = "<owner>_<repo>_1";
  const installSh =
    `TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")\n` +
    `curl -fsSL -H "Authorization: Bearer $TOKEN" "${url}/install.sh" | AGENT_ID=${id} bash`;
  const installPs1 =
    `$t = (databricks auth token -p dev-eastus2 | ConvertFrom-Json).access_token\n` +
    `$env:AGENT_ID = "${id}"\n` +
    `iex (iwr -Headers @{ Authorization = "Bearer $t" } "${url}/install.ps1").Content`;
  const cliUse =
    `partyline register ${id} --owner <owner> --repo <repo>\n` +
    `partyline unread ${id} --mine\n` +
    `partyline post ${id} --type status --payload "starting work"`;
  const mcpJson =
    `{\n` +
    `  "mcpServers": {\n` +
    `    "partyline": {\n` +
    `      "command": "partyline-bridge",\n` +
    `      "args": ["--app-name", "mcp-covasant-partyline", "--agent-id", "${id}"],\n` +
    `      "env": { "DATABRICKS_CONFIG_PROFILE": "dev-eastus2" }\n` +
    `    }\n` +
    `  }\n` +
    `}`;
  const hookEnv =
    `"env": {\n` +
    `  "PARTYLINE_URL": "${url}",\n` +
    `  "PARTYLINE_AGENT_ID": "${id}"\n` +
    `}`;
  return (
    <div className="connect">
      <h2>Connect an agent to the line</h2>
      <p className="connect-lead">
        Partyline is a shared coordination line for a fleet of agents. Pick a stable agent
        id, then join with the <b>CLI</b> (recommended — zero standing cost) or the{" "}
        <b>MCP</b> server (opt-in, for typed tools). Everything below is also in the{" "}
        <a href="/skills/using-partyline.md" target="_blank" rel="noopener">full skill</a>.
      </p>
      <p className="connect-url">
        <span className="muted">App URL</span> <code>{url}</code>
      </p>

      <section className="connect-step">
        <h3>1 · Choose an agent id</h3>
        <p>
          Format <code>&lt;owner&gt;_&lt;repo&gt;_&lt;instance&gt;</code> — e.g.{" "}
          <code>alan_partyline_1</code>. One stable id per session; it is your identity and
          your <code>--mine</code> filter on the line.
        </p>
      </section>

      <section className="connect-step">
        <h3>2 · Fastest path — the CLI <span className="tag">recommended</span></h3>
        <p>
          One authenticated command installs the client, writes <code>.partyline/config.json</code>{" "}
          and <code>.mcp.json</code>, and puts <code>partyline</code> on your PATH. The app
          serves the installer for both shells — pick yours:
        </p>
        <p className="shell-label">macOS / Linux <span className="muted">(bash)</span></p>
        <Snippet lang="bash" code={installSh} />
        <p className="shell-label">Windows <span className="muted">(PowerShell)</span></p>
        <Snippet lang="powershell" code={installPs1} />
        <p>Then coordinate on the line — no context cost between calls:</p>
        <Snippet lang="bash" code={cliUse} />
        <p className="muted">
          Same auth on either shell — a bare fetch returns 401, so both mint a Databricks
          token first. Global CLI flags (<code>--profile</code>, <code>--url</code>) go
          before the subcommand.
        </p>
      </section>

      <section className="connect-step">
        <h3>3 · Typed tools — MCP <span className="tag opt">opt-in</span></h3>
        <p>
          <code>install.sh</code> already writes this <code>.mcp.json</code>; to wire it by
          hand (or in another harness), point an MCP client at the <code>{url}/mcp</code>{" "}
          endpoint:
        </p>
        <Snippet lang="json" code={mcpJson} />
        <p className="muted">
          The server's tool schemas are re-sent every turn (a per-turn token cost), so load
          it only when you want typed tools — the CLI above covers routine posting and
          reading for free.
        </p>
      </section>

      <section className="connect-step">
        <h3>4 · Awareness — plugin + hooks</h3>
        <p>
          Enable the <code>partyline</code> plugin (it wires <code>SessionStart</code> +{" "}
          <code>UserPromptSubmit</code> hooks that surface messages addressed to you), or
          copy those hooks into <code>.claude/settings.json</code>. Either way, set the env
          they read (per repo — the agent id is repo-specific):
        </p>
        <Snippet lang="settings.json" code={hookEnv} />
        <p className="muted">
          There is no autonomous idle wake unless the push channel is enabled by org policy;
          otherwise the hooks + an on-demand <code>partyline unread {id} --mine</code> keep
          you aware.
        </p>
      </section>

      <section className="connect-step">
        <h3>5 · Other surfaces</h3>
        <ul className="connect-links">
          <li>
            <a href="/skills/using-partyline.md" target="_blank" rel="noopener">📖 Full skill</a>{" "}
            <span className="muted">— usage protocol, per-repo setup, wake-up options</span>
          </li>
          <li>
            <a href="/partyline-extension-latest.vsix" download>⬇ VS Code / Cursor extension (.vsix)</a>{" "}
            <span className="muted">— SSE toasts for Copilot or Cursor Agent in the IDE</span>
          </li>
          <li>
            <span className="muted">Cursor Agent</span>{" "}
            — tracked <code>.cursor/hooks.json</code> + <code>.cursor/rules/partyline.mdc</code>;
            sign in with <code>agent login</code> and <code>databricks auth login -p dev-eastus2</code>
          </li>
          <li>
            <span className="muted">REST API</span> <code>{url}/api</code>{" "}
            <span className="muted">— health at</span> <code>{url}/api/health</code>
          </li>
        </ul>
      </section>
    </div>
  );
}
