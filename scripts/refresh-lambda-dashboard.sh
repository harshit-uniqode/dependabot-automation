#!/usr/bin/env bash
# refresh-lambda-dashboard.sh
# Regenerates the Lambda Functions vulnerability dashboard.
#
# Usage: ./scripts/refresh-lambda-dashboard.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Dependabot Dashboard — Lambda Functions"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! gh auth status &>/dev/null; then
  echo "ERROR: gh CLI not authenticated. Run: gh auth login"
  exit 1
fi

python3 "$SCRIPT_DIR/generate_dashboard.py" mobstac-private/beaconstac_lambda_functions

DASHBOARD="$SCRIPT_DIR/../vulnerability-dashboards/lambda-dashboard.html"
if [ -f "$DASHBOARD" ]; then
  echo "  Opening dashboard in browser..."
  open "$DASHBOARD"
fi

echo "  Done."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
