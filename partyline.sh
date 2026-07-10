#!/usr/bin/env bash
# partyline — convenience wrapper around the Covasant Partyline CLI.
#
# Resolves the app URL from .partyline/config.json (or $PARTYLINE_URL) and mints a
# Databricks OAuth token (or reuses $PARTYLINE_TOKEN), then forwards every argument to
# `python -m covasant_partyline.cli`. So instead of the token dance you can just run:
#
#   ./partyline.sh agents
#   ./partyline.sh post alan_partyline_1 --type status --payload "hi"
#   ./partyline.sh watch --exit-on-mention alan_partyline_1 --timeout 3600
#
# See `./partyline.sh --help` for the full command list.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$ROOT/.partyline/config.json"

_cfg() {  # _cfg <key> — read a top-level string from .partyline/config.json, or ""
  [ -f "$CONFIG" ] || { echo ""; return; }
  python -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],'') or '')" "$CONFIG" "$1"
}

# --- URL: env wins, else config ---
if [ -z "${PARTYLINE_URL:-}" ]; then
  PARTYLINE_URL="$(_cfg url)"
fi
export PARTYLINE_URL

# --- Token: env wins, else mint via the databricks CLI ---
if [ -z "${PARTYLINE_TOKEN:-}" ]; then
  # The app-specific config profile wins over the ambient DATABRICKS_CONFIG_PROFILE, which
  # often points at a different workspace (a token from the wrong profile 401s the app).
  PROFILE="$(_cfg databricksProfile)"
  [ -n "$PROFILE" ] || PROFILE="${DATABRICKS_CONFIG_PROFILE:-}"
  PROFILE="${PROFILE:-dev-eastus2}"
  PARTYLINE_TOKEN="$(databricks auth token -p "$PROFILE" \
    | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")"
  export PARTYLINE_TOKEN
fi

exec python -m covasant_partyline.cli "$@"
