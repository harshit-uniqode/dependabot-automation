#!/usr/bin/env bash
# refresh-dashboard.sh
# Fetches live Dependabot alerts and regenerates vulnerability-tracker/dashboard.html
#
# Usage:
#   ./scripts/refresh-dashboard.sh                          # uses default repo
#   ./scripts/refresh-dashboard.sh owner/repo               # override repo
#   ./scripts/refresh-dashboard.sh owner/repo /path/to/package.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-}"
ARGS=""
if [ -n "$REPO" ]; then ARGS="$REPO"; fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Dependabot Dashboard Refresh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check gh auth
if ! gh auth status &>/dev/null; then
  echo "ERROR: gh CLI not authenticated. Run: gh auth login"
  exit 1
fi

# Run generator
python3 "$SCRIPT_DIR/generate-dashboard.py" $ARGS

# Open in browser
DASHBOARD="$SCRIPT_DIR/../vulnerability-tracker/dashboard.html"
if [ -f "$DASHBOARD" ]; then
  echo ""
  echo "  Opening dashboard in browser..."
  open "$DASHBOARD"
fi

echo ""
echo "  Done."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
