#!/usr/bin/env bash
# Bootstrap the Covasant Partyline client into the CURRENT repo — no clone of the
# partyline repo required. Installs the CLI + MCP bridge and writes per-repo config.
#
# The app requires Databricks OAuth on EVERY route, so the fetch itself must be
# authenticated (a bare `curl <app-url>/install.sh` returns 401):
#
#   TOKEN=$(databricks auth token -p dev-eastus2 | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
#   curl -fsSL -H "Authorization: Bearer $TOKEN" "<app-url>/install.sh" | AGENT_ID=alan_myrepo_1 bash
#
# Prereqs: python 3.10+, the `databricks` CLI authenticated for the target profile,
# and ideally `pipx` (falls back to `pip install --user`).
set -euo pipefail

APP_URL="${PARTYLINE_APP_URL:-https://mcp-covasant-partyline-7405607474919121.1.azure.databricksapps.com}"
PROFILE="${PARTYLINE_PROFILE:-dev-eastus2}"
AGENT_ID="${AGENT_ID:-$(basename "$PWD")_1}"

echo "==> Minting Databricks token (profile: $PROFILE)"
TOKEN="$(databricks auth token -p "$PROFILE" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")"

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

echo "==> Writing .partyline/config.json (agent_id: $AGENT_ID)"
mkdir -p .partyline
cat > .partyline/config.json <<JSON
{
  "url": "$APP_URL",
  "agentId": "$AGENT_ID",
  "databricksProfile": "$PROFILE"
}
JSON

if [ -f .mcp.json ]; then
  echo "==> .mcp.json exists — add the 'partyline' server yourself:"
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
echo "(If 'partyline' isn't found, run 'pipx ensurepath' and reopen your shell.)"
