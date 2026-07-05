#!/usr/bin/env bash
# One-command, fully-automated deploy: provisions the autoscale Lakebase (idempotent),
# builds the ops SPA, and deploys the bundle. No manual provisioning steps.
#
# Usage:  ./deploy.sh [profile] [target]
#         ./deploy.sh dev-eastus2 dev      # defaults
set -euo pipefail

PROFILE="${1:-dev-eastus2}"
TARGET="${2:-dev}"
# Must match ENDPOINT_NAME in resources/covasant_partyline_app.app.yml (config.env).
ENDPOINT_NAME="${ENDPOINT_NAME:-projects/partyline/branches/production/endpoints/primary}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> 1/3 Provisioning Lakebase ($ENDPOINT_NAME) on profile $PROFILE"
ENDPOINT_NAME="$ENDPOINT_NAME" DATABRICKS_CONFIG_PROFILE="$PROFILE" \
  python scripts/provision_lakebase.py

echo "==> 2/3 Building ops SPA"
( cd covasant-partyline/client && npm install && npm run build )

echo "==> 3/3 Deploying bundle (target=$TARGET)"
databricks bundle deploy -t "$TARGET" --profile "$PROFILE"

echo "==> Done. Start the app with:"
echo "    databricks bundle run covasant_partyline -t $TARGET --profile $PROFILE"
