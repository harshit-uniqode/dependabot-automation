#!/usr/bin/env bash
# refresh-dashboard.sh
# Regenerates the Angular Portal vulnerability dashboard.
#
# Usage:
#   ./scripts/refresh-dashboard.sh

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

python3 "$SCRIPT_DIR/generate-dashboard.py" mobstac-private/beaconstac_lambda_functions

DASHBOARD="$SCRIPT_DIR/../vulnerability-tracker/lambda-dashboard.html"
if [ -f "$DASHBOARD" ]; then
  echo "  Opening dashboard in browser..."
  open "$DASHBOARD"
fi

echo "  Done."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
