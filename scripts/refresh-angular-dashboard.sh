#!/usr/bin/env bash
# refresh-angular-dashboard.sh
# Regenerates the Angular Portal vulnerability dashboard.
#
# Usage: ./scripts/refresh-angular-dashboard.sh [owner/repo]
# Default repo slug: mobstac-private/beaconstac_angular_portal

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SLUG="${1:-mobstac-private/beaconstac_angular_portal}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Dependabot Dashboard — Angular Portal"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! gh auth status &>/dev/null; then
  echo "ERROR: gh CLI not authenticated. Run: gh auth login"
  exit 1
fi

python3 "$SCRIPT_DIR/generate_dashboard.py" "$REPO_SLUG"

DASHBOARD="$SCRIPT_DIR/../vulnerability-dashboards/angular-dashboard.html"
if [ -f "$DASHBOARD" ]; then
  echo "  Opening dashboard in browser..."
  open "$DASHBOARD"
fi

echo "  Done."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
