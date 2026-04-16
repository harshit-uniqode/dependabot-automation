#!/usr/bin/env bash
# analyze-changelog.sh
# Fetches changelog between two versions of a package and prints it.
# Then you paste this into Claude to check for breaking changes.
#
# Usage (npm):    ./scripts/analyze-changelog.sh npm axios 0.27.2 1.6.8
# Usage (pip):    ./scripts/analyze-changelog.sh pip requests 2.28.0 2.31.0

set -euo pipefail

ECOSYSTEM="${1:-}"  # npm or pip
PACKAGE="${2:-}"
FROM_VERSION="${3:-}"
TO_VERSION="${4:-}"

if [[ -z "$ECOSYSTEM" || -z "$PACKAGE" || -z "$FROM_VERSION" || -z "$TO_VERSION" ]]; then
  echo "Usage: $0 <npm|pip> <package-name> <from-version> <to-version>"
  echo ""
  echo "Examples:"
  echo "  $0 npm axios 0.27.2 1.6.8"
  echo "  $0 pip requests 2.28.0 2.31.0"
  exit 1
fi

echo "=== Changelog analysis: $PACKAGE $FROM_VERSION → $TO_VERSION ==="
echo ""

if [[ "$ECOSYSTEM" == "npm" ]]; then
  # Get npm package info
  REPO_URL=$(npm info "$PACKAGE" repository.url 2>/dev/null | sed 's/git+//;s/\.git$//')
  echo "Package: $PACKAGE"
  echo "Repo:    $REPO_URL"
  echo ""
  echo "Changelog/Releases URL:"
  echo "  $REPO_URL/releases"
  echo "  $REPO_URL/blob/main/CHANGELOG.md"
  echo ""
  echo "npm changelog:"
  npm info "$PACKAGE" version changelog 2>/dev/null || true
  echo ""
  echo "--- Copy the changelog content from the URL above, then run: ---"
  echo "  Ask Claude: 'Does upgrading $PACKAGE from $FROM_VERSION to $TO_VERSION break"
  echo "  anything in my Lambda? Here is the changelog: [paste here]'"

elif [[ "$ECOSYSTEM" == "pip" ]]; then
  # Get PyPI package info
  PYPI_URL="https://pypi.org/pypi/$PACKAGE/json"
  echo "Package: $PACKAGE"
  echo "PyPI:    https://pypi.org/project/$PACKAGE/#history"
  echo ""
  REPO_URL=$(curl -s "$PYPI_URL" | python3 -c "
import sys, json
d = json.load(sys.stdin)
urls = d.get('info', {}).get('project_urls', {})
print(urls.get('Source', '') or urls.get('Homepage', '') or 'Not found')
" 2>/dev/null)
  echo "Source repo: $REPO_URL"
  echo ""
  echo "--- Copy the changelog content from the repo above, then run: ---"
  echo "  Ask Claude: 'Does upgrading $PACKAGE from $FROM_VERSION to $TO_VERSION break"
  echo "  anything in my Lambda? Here is the changelog: [paste here]'"

else
  echo "ERROR: ecosystem must be 'npm' or 'pip'"
  exit 1
fi
