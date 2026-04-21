#!/usr/bin/env python3
"""
run_deep_analysis.py
Filtered, cached deep LLM analysis for Dependabot alerts.

Pipeline:
  fetch alerts (via generate-dashboard.fetch_alerts + classify)
    → apply filters (severity, depType, scope)
    → for each filtered alert:
        skip if cached (unless --force)
        call deep_analyze_with_llm()
        store result keyed by alertId
    → write cache to vulnerability-tracker/.deep-analysis-cache.json

Dashboard generation reads this cache and renders the structured analysis
in the expanded row. This script is the ONLY place we spend LLM tokens.

Usage:
  python3 scripts/run_deep_analysis.py \\
      --repo mobstac-private/beaconstac_lambda_functions \\
      --severity CRITICAL,HIGH \\
      --dep-type direct

  python3 scripts/run_deep_analysis.py --repo <r> --dry-run
  python3 scripts/run_deep_analysis.py --repo <r> --force    # ignore cache
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow sibling imports
_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR))

import importlib.util
_spec = importlib.util.spec_from_file_location("generate_dashboard", _SCRIPT_DIR / "generate-dashboard.py")
_gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gd)

from analyze_alert import deep_analyze_with_llm, load_deep_cache, save_deep_cache


_PROJECT_ROOT = Path(__file__).parent.parent
_DEFAULT_CACHE = _PROJECT_ROOT / "vulnerability-tracker" / ".deep-analysis-cache.json"


def _parse_args():
    p = argparse.ArgumentParser(description="Deep LLM analysis for filtered Dependabot alerts")
    p.add_argument("--repo", required=True, help="Repo slug (e.g. mobstac-private/beaconstac_lambda_functions)")
    p.add_argument("--severity", default="CRITICAL,HIGH", help="Comma-separated severities to include")
    p.add_argument("--dep-type", default="direct", choices=["direct", "sub-dependency", "all"])
    p.add_argument("--scope", default="all", choices=["runtime", "development", "all"])
    p.add_argument("--limit", type=int, default=50, help="Max alerts to analyse (safety cap)")
    p.add_argument("--cache", default=str(_DEFAULT_CACHE), help="Path to cache file")
    p.add_argument("--force", action="store_true", help="Re-analyse even if cached")
    p.add_argument("--dry-run", action="store_true", help="Show filtered set, don't call API")
    p.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    return p.parse_args()


def _filter_rows(rows: list[dict], severities: set[str], dep_type: str, scope: str) -> list[dict]:
    out = []
    for r in rows:
        if r.get("severity") not in severities:
            continue
        if dep_type != "all" and r.get("depType") != dep_type:
            continue
        if scope != "all" and r.get("scope") != scope:
            continue
        out.append(r)
    return out


def _needs_reanalysis(cached: dict, row: dict) -> bool:
    """Invalidate cache if patched version changed."""
    return cached.get("patched") != row.get("patched")


def main() -> None:
    args = _parse_args()

    cfg = _gd.REPO_CONFIGS.get(args.repo)
    if not cfg:
        print(f"ERROR: Unknown repo '{args.repo}'. Add to REPO_CONFIGS.", file=sys.stderr)
        sys.exit(1)

    severities = {s.strip().upper() for s in args.severity.split(",") if s.strip()}

    print(f"\nDeep Analysis Pipeline")
    print(f"  Repo:       {args.repo}")
    print(f"  Severity:   {sorted(severities)}")
    print(f"  Dep-type:   {args.dep_type}")
    print(f"  Scope:      {args.scope}")
    print(f"  Cache:      {args.cache}")
    print(f"  Dry-run:    {args.dry_run}")

    # Reuse the fetch + classify pipeline from generate-dashboard.py
    alerts = _gd.fetch_alerts(args.repo)
    direct_deps = _gd.load_direct_deps(cfg)
    rows = _gd.classify(alerts, direct_deps)

    filtered = _filter_rows(rows, severities, args.dep_type, args.scope)
    if args.limit and len(filtered) > args.limit:
        print(f"  Capping filtered set from {len(filtered)} to --limit={args.limit}")
        filtered = filtered[:args.limit]

    print(f"\n  Filtered: {len(filtered)} alerts")
    for r in filtered:
        print(f"    {r['alertId']:>8}  {r['severity']:<8}  {r['pkg']:<25}  "
              f"{r['depType']:<15}  {r.get('functionName','')}")

    if args.dry_run:
        est_in = len(filtered) * 350
        est_out = len(filtered) * 500
        print(f"\n  Estimated tokens: ~{est_in} in / ~{est_out} out")
        print(f"  Dry-run — no API calls made.")
        return

    cache = load_deep_cache(args.cache)
    analysed = 0
    skipped_cached = 0
    failed = 0

    for row in filtered:
        alert_id = row["alertId"]
        prior = cache.get(alert_id)
        if prior and not args.force and not _needs_reanalysis(prior, row):
            skipped_cached += 1
            continue

        print(f"  → Analysing {alert_id} {row['pkg']}...", flush=True)
        analysis = deep_analyze_with_llm(row, api_key=args.api_key)
        if analysis is None:
            failed += 1
            continue

        cache[alert_id] = {
            "pkg":        row["pkg"],
            "patched":    row.get("patched", ""),
            "analyzedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model":      "claude-sonnet-4-20250514",
            "analysis":   analysis,
        }
        analysed += 1

    Path(args.cache).parent.mkdir(parents=True, exist_ok=True)
    save_deep_cache(args.cache, cache)

    print(f"\n  Analysed: {analysed}")
    print(f"  Cached:   {skipped_cached}")
    print(f"  Failed:   {failed}")
    print(f"  Cache:    {args.cache}")


if __name__ == "__main__":
    main()
