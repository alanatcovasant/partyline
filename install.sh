#!/usr/bin/env bash
# Bootstrap the Covasant Partyline client into the CURRENT repo — no clone of the
# partyline repo required. Installs the CLI + MCP bridge, registers the live channel once
# per machine (user-scope MCP server + `claude-pl` launcher), and gives this clone an agent id.
#
# The app requires Databricks OAuth on EVERY route, so the fetch itself must be
# authenticated (a bare `curl <app-url>/install.sh` returns 401). Log in first if you never have:
#
#   databricks auth login --host https://adb-7405607474919121.1.azuredatabricks.net --profile dev-eastus2
#   TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
#   curl -fsSL -H "Authorization: Bearer $TOKEN" "<app-url>/install.sh" | OWNER=alan bash
#
# Optional env: OWNER (your handle), AGENT_ID (pin an id instead of auto-allocating
# <owner>_<repo>_<n>), PARTYLINE_PROFILE, PARTYLINE_WORKSPACE_HOST, PARTYLINE_APP_URL.
# Prereqs: python 3.10+, the `databricks` and `claude` CLIs, ideally `pipx`.
set -euo pipefail

APP_URL="${PARTYLINE_APP_URL:-https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com}"
PROFILE="${PARTYLINE_PROFILE:-dev-eastus2}"
# The app lives in this workspace (its id is the numeric suffix of APP_URL).
WORKSPACE_HOST="${PARTYLINE_WORKSPACE_HOST:-https://adb-7405607474919121.1.azuredatabricks.net}"
AGENT_ID="${AGENT_ID:-}"
OWNER="${OWNER:-${AGENT_ID%%_*}}"

mint_token() {
  databricks auth token -p "$PROFILE" 2>/dev/null \
    | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])" 2>/dev/null
}

echo "==> Minting Databricks token (profile: $PROFILE)"
TOKEN="$(mint_token || true)"
if [ -z "$TOKEN" ]; then
  echo "==> No valid login for profile '$PROFILE' — opening browser login ($WORKSPACE_HOST)"
  databricks auth login --host "$WORKSPACE_HOST" --profile "$PROFILE"
  TOKEN="$(mint_token || true)"
  [ -n "$TOKEN" ] || { echo "Login did not complete; re-run this installer." >&2; exit 1; }
fi

echo "==> Resolving latest client wheel"
# Databricks Apps require OAuth on every route, so the download is authenticated.
WHEEL="$(curl -fsSL -H "Authorization: Bearer $TOKEN" "$APP_URL/partyline-client-latest.txt")"

echo "==> Installing $WHEEL"
TMP="$(mktemp -d)"
curl -fsSL -H "Authorization: Bearer $TOKEN" "$APP_URL/$WHEEL" -o "$TMP/$WHEEL"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$TMP/$WHEEL"          # isolated; puts partyline* on PATH
else
  python -m pip install --user --force-reinstall "$TMP/$WHEEL"
fi
rm -rf "$TMP"

# A fresh pipx install may not be on this shell's PATH yet.
if ! command -v partyline >/dev/null 2>&1; then
  PIPX_BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
  export PATH="${PIPX_BIN:+$PIPX_BIN:}$HOME/.local/bin:$PATH"
fi

if command -v partyline >/dev/null 2>&1 && [ -n "$OWNER" ]; then
  echo "==> Registering the live channel (once per machine)"
  partyline setup --owner "$OWNER" --app-url "$APP_URL" \
    --databricks-profile "$PROFILE" --workspace-host "$WORKSPACE_HOST"
  if [ -n "$AGENT_ID" ]; then
    echo "==> Writing .partyline/config.json (agent_id: $AGENT_ID)"
    mkdir -p .partyline
    cat > .partyline/config.json <<JSON
{
  "url": "$APP_URL",
  "agentId": "$AGENT_ID",
  "databricksProfile": "$PROFILE"
}
JSON
  else
    echo "==> Allocating this clone's agent id"
    partyline init --owner "$OWNER"
  fi
else
  echo "==> Skipped channel setup + agent id (need OWNER, and 'partyline' on PATH)."
  echo "    After 'pipx ensurepath' and reopening your shell, run:"
  echo "      partyline setup --owner <handle> --app-url $APP_URL --databricks-profile $PROFILE --workspace-host $WORKSPACE_HOST"
  echo "      partyline init"
fi

# The live channel is a user-scope MCP server now (partyline setup), so it is NOT written
# here — a second definition would shadow it. Only the typed-tools bridge goes in .mcp.json.
if [ -f .mcp.json ]; then
  echo "==> .mcp.json exists — add the typed-tools 'partyline' server yourself if you want it:"
  echo '      "partyline": { "command": "partyline-bridge",'
  echo '                     "args": ["--app-name", "mcp-covasant-partyline"],'
  echo "                     \"env\": { \"DATABRICKS_CONFIG_PROFILE\": \"$PROFILE\" } }"
else
  echo "==> Writing .mcp.json"
  cat > .mcp.json <<JSON
{
  "mcpServers": {
    "partyline": {
      "command": "partyline-bridge",
      "args": ["--app-name", "mcp-covasant-partyline"],
      "env": { "DATABRICKS_CONFIG_PROFILE": "$PROFILE" }
    }
  }
}
JSON
fi

echo ""
echo "Done. Verify with:  partyline agents"
echo "Start Claude with the live channel using:  claude-pl"
echo "(If 'partyline' isn't found, run 'pipx ensurepath' and reopen your shell.)"
