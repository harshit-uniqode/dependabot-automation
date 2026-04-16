#!/usr/bin/env python3
"""
generate-dashboard.py
Fetches live Dependabot alerts from a GitHub repo, classifies them,
and writes vulnerability-tracker/dashboard.html.

Usage:
  python3 scripts/generate-dashboard.py [owner/repo]

Requires: gh CLI authenticated with repo scope.
"""

import json
import subprocess
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
DEFAULT_REPO = "mobstac-private/beaconstac_angular_portal"
PACKAGE_JSON_PATH = "/Users/harshitky/Desktop/Work/beaconstac_angular_portal/bac-app/package.json"
OUTPUT_PATH = Path(__file__).parent.parent / "vulnerability-tracker" / "dashboard.html"

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

ACTION_MAP = {
    ("CRITICAL", "direct"):       "FIX NOW",
    ("CRITICAL", "sub-dependency"): "FIX NOW",
    ("HIGH",     "direct"):       "FIX - direct",
    ("HIGH",     "sub-dependency"): "FIX - sub-dep",
    ("MEDIUM",   "direct"):       "FIX - medium direct",
    ("MEDIUM",   "sub-dependency"): "DEFER or ignore if dev-only",
    ("LOW",      "direct"):       "BATCH end of quarter",
    ("LOW",      "sub-dependency"): "BATCH end of quarter",
}

PRIORITY_MAP = {
    ("CRITICAL", "direct"):         1,
    ("CRITICAL", "sub-dependency"): 1,
    ("HIGH",     "direct"):         2,
    ("HIGH",     "sub-dependency"): 3,
    ("MEDIUM",   "direct"):         4,
    ("MEDIUM",   "sub-dependency"): 5,
    ("LOW",      "direct"):         6,
    ("LOW",      "sub-dependency"): 6,
}


# ─────────────────────────────────────────────────────────────
# FETCH ALERTS
# ─────────────────────────────────────────────────────────────
def fetch_alerts(repo):
    print(f"  Fetching Dependabot alerts from {repo}...")
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/dependabot/alerts",
         "--paginate", "-q", ".[]"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"ERROR: gh api failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # gh --paginate with -q ".[]" emits one JSON object per line
    alerts = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    open_alerts = [a for a in alerts if a.get("state") == "open"]
    print(f"  Found {len(open_alerts)} open alerts (of {len(alerts)} total)")
    return open_alerts


# ─────────────────────────────────────────────────────────────
# LOAD DIRECT DEPS
# ─────────────────────────────────────────────────────────────
def load_direct_deps(pkg_json_path):
    try:
        with open(pkg_json_path) as f:
            pkg = json.load(f)
        deps = set()
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            deps.update(pkg.get(key, {}).keys())
        print(f"  Loaded {len(deps)} direct deps from package.json")
        return deps
    except FileNotFoundError:
        print(f"  WARNING: package.json not found at {pkg_json_path}, all deps will be 'sub-dependency'")
        return set()


# ─────────────────────────────────────────────────────────────
# CLASSIFY & SORT
# ─────────────────────────────────────────────────────────────
def classify(alerts, direct_deps):
    rows = []
    for a in alerts:
        alert_id = f"#{a['number']}"
        pkg = a["dependency"]["package"]["name"]
        ecosystem = a["dependency"]["package"]["ecosystem"]
        severity = (a.get("security_advisory") or {}).get("severity", "").upper()
        cvss_raw = ((a.get("security_advisory") or {}).get("cvss") or {}).get("score", 0) or 0
        cvss = round(float(cvss_raw), 1)
        scope = a["dependency"].get("scope") or "development"
        manifest = a["dependency"].get("manifest_path") or ""
        patched_versions = (a.get("security_vulnerability") or {}).get("first_patched_version") or {}
        patched = patched_versions.get("identifier", "") if isinstance(patched_versions, dict) else str(patched_versions)
        summary = (a.get("security_advisory") or {}).get("summary", "")[:120]

        dep_type = "direct" if pkg in direct_deps else "sub-dependency"
        action = ACTION_MAP.get((severity, dep_type), "REVIEW")
        priority = PRIORITY_MAP.get((severity, dep_type), 9)

        rows.append({
            "alertId": alert_id,
            "pkg": pkg,
            "eco": ecosystem,
            "severity": severity,
            "cvss": cvss,
            "scope": scope,
            "manifest": manifest,
            "depType": dep_type,
            "patched": patched,
            "summary": summary,
            "action": action,
            "priority": priority,
            "status": "To Do",
            "notes": "",
            "resolvedAt": "",
        })

    # Sort: priority asc, then CVSS desc
    rows.sort(key=lambda r: (r["priority"], -r["cvss"], r["pkg"]))
    return rows


# ─────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────
def compute_stats(rows):
    stats = {
        "total": len(rows),
        "critical": sum(1 for r in rows if r["severity"] == "CRITICAL"),
        "high":     sum(1 for r in rows if r["severity"] == "HIGH"),
        "medium":   sum(1 for r in rows if r["severity"] == "MEDIUM"),
        "low":      sum(1 for r in rows if r["severity"] == "LOW"),
        "direct":   sum(1 for r in rows if r["depType"] == "direct"),
        "sub":      sum(1 for r in rows if r["depType"] == "sub-dependency"),
        "runtime":  sum(1 for r in rows if r["scope"] == "runtime"),
        "dev":      sum(1 for r in rows if r["scope"] == "development"),
    }
    return stats


# ─────────────────────────────────────────────────────────────
# JS DATA ARRAY
# ─────────────────────────────────────────────────────────────
def rows_to_js(rows):
    parts = []
    for r in rows:
        def esc(s):
            return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

        parts.append(
            f'  {{alertId:"{esc(r["alertId"])}",pkg:"{esc(r["pkg"])}",eco:"{esc(r["eco"])}",'
            f'severity:"{esc(r["severity"])}",cvss:{r["cvss"]},scope:"{esc(r["scope"])}",'
            f'manifest:"{esc(r["manifest"])}",depType:"{esc(r["depType"])}",'
            f'patched:"{esc(r["patched"])}",summary:"{esc(r["summary"])}",'
            f'action:"{esc(r["action"])}",priority:{r["priority"]},'
            f'status:"To Do",notes:"",resolvedAt:""}}'
        )
    return "[\n" + ",\n".join(parts) + "\n]"


# ─────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────
def build_html(repo, rows, stats, generated_at):
    repo_name = repo.split("/")[-1]
    js_data = rows_to_js(rows)
    runtime_pct = round(stats["runtime"] / stats["total"] * 100, 1) if stats["total"] else 0
    dev_pct = round(stats["dev"] / stats["total"] * 100, 1) if stats["total"] else 0

    # Direct dep runtime high/critical count for footnote
    direct_runtime_high = sum(
        1 for r in rows
        if r["depType"] == "direct" and r["scope"] == "runtime"
        and r["severity"] in ("CRITICAL", "HIGH")
    )
    direct_dev = sum(1 for r in rows if r["depType"] == "direct" and r["scope"] == "development")
    direct_runtime_medium = sum(
        1 for r in rows
        if r["depType"] == "direct" and r["scope"] == "runtime" and r["severity"] == "MEDIUM"
    )
    sub_high = sum(1 for r in rows if r["depType"] == "sub-dependency" and r["severity"] in ("CRITICAL", "HIGH"))
    sub_medium = sum(1 for r in rows if r["depType"] == "sub-dependency" and r["severity"] == "MEDIUM")
    sub_low = sum(1 for r in rows if r["depType"] == "sub-dependency" and r["severity"] == "LOW")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Dependabot Vulnerability Tracker — {repo_name}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg-primary:    #0d1117;
    --bg-secondary:  #161b22;
    --bg-tertiary:   #1c2128;
    --bg-hover:      #21262d;
    --border:        #30363d;
    --border-muted:  #21262d;
    --text-primary:  #e6edf3;
    --text-secondary:#8b949e;
    --text-muted:    #6e7681;
    --red:           #da3633;
    --red-bg:        #1f1414;
    --red-border:    #5a1e1e;
    --orange:        #d29922;
    --orange-bg:     #1d1810;
    --orange-border: #5a3e10;
    --yellow:        #e3b341;
    --yellow-bg:     #1c1a10;
    --yellow-border: #524010;
    --blue:          #388bfd;
    --blue-bg:       #0f1d2e;
    --blue-border:   #1a3a5c;
    --green:         #3fb950;
    --green-bg:      #0d1f12;
    --green-border:  #1a4023;
    --purple:        #bc8cff;
    --gray:          #484f58;
    --gray-bg:       #161b22;
    --radius-sm:     4px;
    --radius-md:     6px;
    --radius-lg:     8px;
    --shadow:        0 1px 3px rgba(0,0,0,.4), 0 4px 12px rgba(0,0,0,.3);
  }}

  html {{ scroll-behavior: smooth; }}

  body {{
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
  }}

  /* ── TOP BAR ── */
  .topbar {{
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    padding: 20px 32px;
    position: sticky;
    top: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .topbar-icon {{
    width: 36px; height: 36px;
    background: linear-gradient(135deg, var(--red), #ff6b6b);
    border-radius: var(--radius-md);
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; flex-shrink: 0;
  }}
  .topbar-text h1 {{
    font-size: 17px; font-weight: 600;
    color: var(--text-primary); letter-spacing: -0.01em;
  }}
  .topbar-text p {{ font-size: 12px; color: var(--text-secondary); margin-top: 2px; }}
  .topbar-text p span {{ color: var(--orange); font-weight: 600; }}
  .topbar-meta {{
    margin-left: auto; display: flex; align-items: center;
    gap: 8px; font-size: 12px; color: var(--text-muted);
  }}
  .live-dot {{
    width: 7px; height: 7px; background: var(--green);
    border-radius: 50%; animation: pulse 2s infinite;
  }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .4; }} }}

  /* ── MAIN LAYOUT ── */
  .container {{ max-width: 1400px; margin: 0 auto; padding: 28px 32px; }}

  /* ── SECTION LABEL ── */
  .section-label {{
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .08em; color: var(--text-muted); margin-bottom: 12px;
  }}

  /* ── SUMMARY CARDS ── */
  .cards-grid {{
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 12px; margin-bottom: 28px;
  }}
  @media (max-width: 900px) {{ .cards-grid {{ grid-template-columns: repeat(2, 1fr); }} }}

  .card {{
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: var(--radius-lg); padding: 20px;
    position: relative; overflow: hidden;
    transition: border-color .15s, transform .15s;
  }}
  .card:hover {{ transform: translateY(-1px); }}
  .card::before {{
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
  }}
  .card.critical::before {{ background: var(--red); }}
  .card.high::before     {{ background: var(--orange); }}
  .card.medium::before   {{ background: var(--yellow); }}
  .card.low::before      {{ background: var(--blue); }}
  .card.critical {{ border-color: var(--red-border); background: var(--red-bg); }}
  .card.high     {{ border-color: var(--orange-border); background: var(--orange-bg); }}
  .card.medium   {{ border-color: var(--yellow-border); background: var(--yellow-bg); }}
  .card.low      {{ border-color: var(--blue-border); background: var(--blue-bg); }}
  .card-count {{ font-size: 36px; font-weight: 700; line-height: 1; margin-bottom: 6px; font-variant-numeric: tabular-nums; }}
  .card.critical .card-count {{ color: var(--red); }}
  .card.high     .card-count {{ color: var(--orange); }}
  .card.medium   .card-count {{ color: var(--yellow); }}
  .card.low      .card-count {{ color: var(--blue); }}
  .card-label {{ font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; }}
  .card.critical .card-label {{ color: var(--red); }}
  .card.high     .card-label {{ color: var(--orange); }}
  .card.medium   .card-label {{ color: var(--yellow); }}
  .card.low      .card-label {{ color: var(--blue); }}
  .card-sub {{ font-size: 11px; color: var(--text-muted); margin-top: 4px; }}

  /* ── PROGRESS CARDS ── */
  .progress-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 28px; }}
  .progress-card {{ background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 20px; }}
  .progress-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }}
  .progress-title {{ font-size: 14px; font-weight: 600; color: var(--text-primary); }}
  .progress-stats {{ font-size: 12px; color: var(--text-muted); }}
  .progress-stats strong {{ color: var(--text-secondary); }}
  .progress-bar-wrap {{ height: 8px; background: var(--bg-tertiary); border-radius: 99px; overflow: hidden; }}
  .progress-bar-fill {{ height: 100%; border-radius: 99px; transition: width .4s ease; background: linear-gradient(90deg, var(--green), #2ea043); }}
  .progress-footnote {{ font-size: 11px; color: var(--text-muted); margin-top: 8px; display: flex; gap: 12px; flex-wrap: wrap; }}
  .progress-footnote span {{ display: flex; align-items: center; gap: 4px; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}

  /* ── CHARTS ── */
  .charts-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 28px; }}
  @media (max-width: 900px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
  .chart-card {{ background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 20px; }}
  .chart-title {{ font-size: 13px; font-weight: 600; color: var(--text-primary); margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
  .chart-title span {{ font-size: 11px; font-weight: 400; color: var(--text-muted); }}

  .bar-chart {{ width: 100%; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .bar-label {{ font-size: 12px; color: var(--text-secondary); width: 130px; flex-shrink: 0; text-align: right; font-family: "SFMono-Regular", Consolas, monospace; }}
  .bar-track {{ flex: 1; height: 18px; background: var(--bg-tertiary); border-radius: var(--radius-sm); overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: var(--radius-sm); display: flex; align-items: center; padding-left: 6px; font-size: 10px; font-weight: 700; color: #fff; min-width: 24px; }}
  .bar-fill.critical {{ background: var(--red); }}
  .bar-fill.high     {{ background: var(--orange); }}
  .bar-fill.medium   {{ background: var(--yellow); color: #0d1117; }}
  .bar-fill.mixed    {{ background: linear-gradient(90deg, var(--orange), var(--yellow)); }}
  .bar-count {{ font-size: 12px; font-weight: 700; color: var(--text-secondary); width: 24px; }}

  .donut-wrap {{ display: flex; flex-direction: column; align-items: center; gap: 20px; }}
  .donut-svg-wrap {{ position: relative; width: 160px; height: 160px; }}
  .donut-center-text {{ position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; pointer-events: none; }}
  .donut-center-big {{ font-size: 28px; font-weight: 700; color: var(--text-primary); line-height: 1; }}
  .donut-center-small {{ font-size: 11px; color: var(--text-muted); }}
  .donut-legend {{ display: flex; flex-direction: column; gap: 10px; width: 100%; }}
  .legend-item {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
  .legend-left {{ display: flex; align-items: center; gap: 8px; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  .legend-label {{ font-size: 12px; color: var(--text-secondary); }}
  .legend-val {{ font-size: 13px; font-weight: 700; color: var(--text-primary); }}
  .legend-pct {{ font-size: 11px; color: var(--text-muted); }}

  /* ── FILTERS ── */
  .filters-bar {{
    background: var(--bg-secondary); border: 1px solid var(--border);
    border-radius: var(--radius-lg); padding: 16px 20px; margin-bottom: 16px;
    display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
  }}
  .filter-group {{ display: flex; align-items: center; gap: 8px; }}
  .filter-group label {{ font-size: 12px; color: var(--text-muted); font-weight: 500; white-space: nowrap; }}
  select, .search-input {{
    background: var(--bg-tertiary); border: 1px solid var(--border);
    border-radius: var(--radius-md); color: var(--text-primary);
    font-size: 13px; padding: 6px 10px; outline: none;
    transition: border-color .15s; appearance: none; cursor: pointer;
  }}
  select {{
    padding-right: 28px;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%238b949e' d='M1 1l5 5 5-5'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 8px center;
  }}
  select:focus, .search-input:focus {{ border-color: var(--blue); }}
  .search-wrap {{ position: relative; flex: 1; min-width: 200px; }}
  .search-icon {{ position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--text-muted); font-size: 13px; pointer-events: none; }}
  .search-input {{ width: 100%; padding-left: 32px; cursor: text; }}
  .filter-count {{ margin-left: auto; font-size: 12px; color: var(--text-muted); white-space: nowrap; }}
  .filter-count strong {{ color: var(--text-primary); }}
  .btn-clear {{
    background: none; border: 1px solid var(--border); border-radius: var(--radius-md);
    color: var(--text-secondary); font-size: 12px; padding: 6px 12px; cursor: pointer;
    transition: background .15s, color .15s; white-space: nowrap;
  }}
  .btn-clear:hover {{ background: var(--bg-hover); color: var(--text-primary); }}

  /* ── TABLE ── */
  .table-wrap {{ background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; margin-bottom: 28px; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}

  thead th {{
    background: var(--bg-tertiary); border-bottom: 1px solid var(--border);
    padding: 11px 14px; text-align: left; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .06em; color: var(--text-muted);
    white-space: nowrap; user-select: none; position: sticky; top: 0;
    z-index: 10; cursor: pointer; transition: color .15s;
  }}
  thead th:hover {{ color: var(--text-primary); }}
  thead th.sorted {{ color: var(--blue); }}
  .sort-indicator {{ margin-left: 4px; opacity: .7; }}

  tbody tr {{ border-bottom: 1px solid var(--border-muted); transition: background .1s; cursor: pointer; }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:nth-child(even) {{ background: rgba(255,255,255,.018); }}
  tbody tr:hover {{ background: var(--bg-hover); }}
  tbody tr.expanded {{ background: var(--bg-tertiary); }}
  tbody td {{ padding: 10px 14px; vertical-align: middle; color: var(--text-primary); font-size: 13px; }}

  .td-id {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; color: var(--text-muted); }}
  .td-id a {{ color: var(--blue); text-decoration: none; display: inline-flex; align-items: center; gap: 4px; transition: color .15s; }}
  .td-id a:hover {{ color: #6cb6ff; text-decoration: underline; }}
  .td-pkg {{ font-weight: 600; font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }}

  tbody tr.done-row td {{ opacity: .55; }}
  tbody tr.done-row .td-pkg {{ text-decoration: line-through; text-decoration-color: var(--green); }}

  .badge {{ display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }}
  .badge.critical {{ background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }}
  .badge.high     {{ background: var(--orange-bg); color: var(--orange); border: 1px solid var(--orange-border); }}
  .badge.medium   {{ background: var(--yellow-bg); color: var(--yellow); border: 1px solid var(--yellow-border); }}
  .badge.low      {{ background: var(--blue-bg); color: var(--blue); border: 1px solid var(--blue-border); }}

  .type-badge {{ display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 500; white-space: nowrap; }}
  .type-badge.direct {{ background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }}
  .type-badge.sub    {{ background: var(--gray-bg); color: var(--text-muted); border: 1px solid var(--border); }}

  .status-badge {{ display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 500; white-space: nowrap; cursor: pointer; }}
  .status-badge.todo {{ background: var(--bg-tertiary); color: var(--text-muted); border: 1px solid var(--border); }}
  .status-badge.done {{ background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }}

  .cvss-pill {{ font-size: 12px; font-weight: 700; font-variant-numeric: tabular-nums; padding: 1px 6px; border-radius: var(--radius-sm); background: var(--bg-tertiary); color: var(--text-secondary); }}
  .cvss-pill.critical {{ color: var(--red); background: var(--red-bg); }}
  .cvss-pill.high     {{ color: var(--orange); background: var(--orange-bg); }}
  .cvss-pill.medium   {{ color: var(--yellow); background: var(--yellow-bg); }}

  .detail-row td {{ padding: 0; background: var(--bg-tertiary); }}
  .detail-content {{ padding: 16px 20px 20px; border-top: 1px solid var(--border); display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
  .detail-field label {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); display: block; margin-bottom: 4px; }}
  .detail-field p {{ font-size: 13px; color: var(--text-primary); }}
  .detail-field.full {{ grid-column: 1 / -1; }}
  .detail-field code {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; background: var(--bg-primary); padding: 2px 6px; border-radius: var(--radius-sm); color: var(--text-secondary); }}
  .action-chip {{ display: inline-block; padding: 3px 10px; border-radius: var(--radius-sm); font-size: 12px; font-weight: 600; background: var(--bg-primary); border: 1px solid var(--border); color: var(--text-secondary); }}
  .action-chip.urgent {{ background: var(--red-bg); color: var(--red); border-color: var(--red-border); }}

  .empty-state {{ padding: 60px 20px; text-align: center; color: var(--text-muted); }}
  .empty-state p {{ font-size: 14px; }}

  /* ── PACKAGE GROUPS ── */
  .groups-section {{ margin-bottom: 40px; }}
  .accordion-item {{ background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius-lg); margin-bottom: 8px; overflow: hidden; transition: border-color .15s; }}
  .accordion-item.open {{ border-color: #388bfd44; }}
  .accordion-header {{ display: flex; align-items: center; gap: 12px; padding: 14px 18px; cursor: pointer; user-select: none; transition: background .1s; }}
  .accordion-header:hover {{ background: var(--bg-hover); }}
  .accordion-pkg {{ font-family: "SFMono-Regular", Consolas, monospace; font-size: 13px; font-weight: 700; color: var(--text-primary); }}
  .accordion-count {{ background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: 99px; font-size: 11px; font-weight: 700; color: var(--text-secondary); padding: 1px 8px; }}
  .accordion-severity-row {{ display: flex; gap: 6px; flex-wrap: wrap; margin-left: 4px; }}
  .accordion-impact {{ margin-left: auto; font-size: 12px; color: var(--text-muted); white-space: nowrap; }}
  .accordion-impact strong {{ color: var(--text-secondary); }}
  .accordion-chevron {{ color: var(--text-muted); transition: transform .2s; font-size: 12px; flex-shrink: 0; }}
  .accordion-item.open .accordion-chevron {{ transform: rotate(180deg); }}
  .accordion-body {{ display: none; border-top: 1px solid var(--border-muted); padding: 12px 18px 16px; }}
  .accordion-item.open .accordion-body {{ display: block; }}
  .acc-alert-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .acc-alert {{ display: flex; align-items: flex-start; gap: 12px; padding: 10px 12px; border-radius: var(--radius-md); background: var(--bg-tertiary); border: 1px solid var(--border-muted); }}
  .acc-alert-id {{ font-family: monospace; font-size: 11px; color: var(--text-muted); flex-shrink: 0; width: 44px; }}
  .acc-alert-id a {{ color: var(--blue); text-decoration: none; }}
  .acc-alert-id a:hover {{ text-decoration: underline; }}
  .acc-alert-summary {{ font-size: 12px; color: var(--text-secondary); flex: 1; }}
  .acc-alert-patched {{ font-size: 11px; font-family: monospace; color: var(--text-muted); white-space: nowrap; }}

  /* ── REPO BADGE ── */
  .repo-link {{ font-size: 12px; color: var(--blue); text-decoration: none; display: inline-flex; align-items: center; gap: 4px; }}
  .repo-link:hover {{ text-decoration: underline; }}

  /* ── FOOTER ── */
  .footer {{ border-top: 1px solid var(--border); padding: 20px 32px; text-align: center; font-size: 12px; color: var(--text-muted); background: var(--bg-secondary); }}

  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: var(--bg-primary); }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 99px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: var(--gray); }}
</style>
</head>
<body>

<!-- TOP BAR -->
<div class="topbar">
  <div class="topbar-icon">&#9888;</div>
  <div class="topbar-text">
    <h1>Dependabot Vulnerability Tracker &mdash; {repo_name}</h1>
    <p>
      <span>{stats['total']} open alerts</span>
      &middot; Repo: <a href="https://github.com/{repo}" target="_blank" rel="noopener" class="repo-link">{repo} &#8599;</a>
      &middot; Generated: {generated_at}
    </p>
  </div>
  <div class="topbar-meta">
    <div class="live-dot"></div>
    <span>Live data</span>
  </div>
</div>

<div class="container">

  <!-- SUMMARY CARDS -->
  <div class="section-label">Severity Overview</div>
  <div class="cards-grid">
    <div class="card critical">
      <div class="card-count" id="cnt-critical">{stats['critical']}</div>
      <div class="card-label">Critical</div>
      <div class="card-sub">CVSS 9.0&ndash;10.0 &middot; Fix immediately</div>
    </div>
    <div class="card high">
      <div class="card-count" id="cnt-high">{stats['high']}</div>
      <div class="card-label">High</div>
      <div class="card-sub">CVSS 7.0&ndash;8.9 &middot; Fix this sprint</div>
    </div>
    <div class="card medium">
      <div class="card-count" id="cnt-medium">{stats['medium']}</div>
      <div class="card-label">Medium</div>
      <div class="card-sub">CVSS 4.0&ndash;6.9 &middot; Schedule fix</div>
    </div>
    <div class="card low">
      <div class="card-count" id="cnt-low">{stats['low']}</div>
      <div class="card-label">Low</div>
      <div class="card-sub">CVSS 0.1&ndash;3.9 &middot; Batch end-of-quarter</div>
    </div>
  </div>

  <!-- PROGRESS CARDS -->
  <div class="section-label">Remediation Progress</div>
  <div class="progress-grid">
    <div class="progress-card">
      <div class="progress-header">
        <div class="progress-title">Direct Dependencies</div>
        <div class="progress-stats"><strong id="direct-done">0</strong> / <strong id="direct-total">{stats['direct']}</strong> resolved</div>
      </div>
      <div class="progress-bar-wrap">
        <div class="progress-bar-fill" id="direct-bar" style="width:0%"></div>
      </div>
      <div class="progress-footnote">
        <span><span class="dot" style="background:var(--red)"></span> Runtime critical/high: {direct_runtime_high}</span>
        <span><span class="dot" style="background:var(--orange)"></span> Dev direct: {direct_dev}</span>
        <span><span class="dot" style="background:var(--yellow)"></span> Runtime medium: {direct_runtime_medium}</span>
      </div>
    </div>
    <div class="progress-card">
      <div class="progress-header">
        <div class="progress-title">Sub-Dependencies (Transitive)</div>
        <div class="progress-stats"><strong id="sub-done">0</strong> / <strong id="sub-total">{stats['sub']}</strong> resolved</div>
      </div>
      <div class="progress-bar-wrap">
        <div class="progress-bar-fill" id="sub-bar" style="width:0%"></div>
      </div>
      <div class="progress-footnote">
        <span><span class="dot" style="background:var(--orange)"></span> High: {sub_high}</span>
        <span><span class="dot" style="background:var(--yellow)"></span> Medium: {sub_medium}</span>
        <span><span class="dot" style="background:var(--blue)"></span> Low: {sub_low}</span>
      </div>
    </div>
  </div>

  <!-- CHARTS -->
  <div class="section-label">Analytics</div>
  <div class="charts-grid">
    <div class="chart-card">
      <div class="chart-title">Alerts by Package <span>(fix one package = close N alerts)</span></div>
      <div class="bar-chart" id="bar-chart"></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Runtime vs Development Scope</div>
      <div class="donut-wrap">
        <div class="donut-svg-wrap">
          <svg id="donut-svg" viewBox="0 0 160 160" width="160" height="160"></svg>
          <div class="donut-center-text">
            <div class="donut-center-big">{stats['total']}</div>
            <div class="donut-center-small">total alerts</div>
          </div>
        </div>
        <div class="donut-legend">
          <div class="legend-item">
            <div class="legend-left">
              <div class="legend-dot" style="background:var(--red)"></div>
              <div class="legend-label">Runtime</div>
            </div>
            <div style="display:flex;gap:8px;align-items:baseline">
              <div class="legend-val">{stats['runtime']}</div>
              <div class="legend-pct">{runtime_pct}%</div>
            </div>
          </div>
          <div class="legend-item">
            <div class="legend-left">
              <div class="legend-dot" style="background:var(--blue)"></div>
              <div class="legend-label">Development</div>
            </div>
            <div style="display:flex;gap:8px;align-items:baseline">
              <div class="legend-val">{stats['dev']}</div>
              <div class="legend-pct">{dev_pct}%</div>
            </div>
          </div>
          <div style="margin-top:8px;padding:10px;background:var(--bg-tertiary);border-radius:var(--radius-md);border:1px solid var(--border)">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">Prioritization note</div>
            <div style="font-size:12px;color:var(--text-secondary);">Runtime vulnerabilities affect production users. Dev-only alerts in CI/build tools carry lower real-world risk.</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- FILTERS -->
  <div class="filters-bar">
    <div class="filter-group">
      <label>Severity</label>
      <select id="filter-severity">
        <option value="">All severities</option>
        <option value="CRITICAL">CRITICAL</option>
        <option value="HIGH">HIGH</option>
        <option value="MEDIUM">MEDIUM</option>
        <option value="LOW">LOW</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Type</label>
      <select id="filter-type">
        <option value="">All types</option>
        <option value="direct">Direct</option>
        <option value="sub-dependency">Sub-dependency</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Status</label>
      <select id="filter-status">
        <option value="">All statuses</option>
        <option value="To Do">To Do</option>
        <option value="Done">Done</option>
      </select>
    </div>
    <div class="filter-group">
      <label>Scope</label>
      <select id="filter-scope">
        <option value="">All scopes</option>
        <option value="runtime">Runtime</option>
        <option value="development">Development</option>
      </select>
    </div>
    <div class="search-wrap">
      <span class="search-icon">&#128269;</span>
      <input type="text" class="search-input" id="search-input" placeholder="Search package, summary..." />
    </div>
    <div class="filter-count" id="filter-count">Showing <strong>{stats['total']}</strong> of {stats['total']} alerts</div>
    <button class="btn-clear" id="btn-clear">Clear filters</button>
  </div>

  <!-- TABLE -->
  <div class="table-wrap">
    <div class="table-scroll">
      <table id="alert-table">
        <thead>
          <tr>
            <th data-col="alertId" class="sorted">Alert <span class="sort-indicator">&#9650;</span></th>
            <th data-col="package">Package</th>
            <th data-col="severity">Severity</th>
            <th data-col="cvss">CVSS</th>
            <th data-col="depType">Type</th>
            <th data-col="scope">Scope</th>
            <th data-col="action">Action</th>
            <th data-col="status">Status</th>
          </tr>
        </thead>
        <tbody id="table-body"></tbody>
      </table>
    </div>
  </div>

  <!-- PACKAGE GROUPS -->
  <div class="groups-section">
    <div class="section-label" style="margin-bottom:16px;">Package Groups
      <span style="font-weight:400;text-transform:none;letter-spacing:0;color:var(--text-muted);font-size:11px;">
        &mdash; Fix one package to resolve multiple alerts
      </span>
    </div>
    <div id="accordion-container"></div>
  </div>

</div>

<div class="footer">
  Dependabot Vulnerability Tracker &mdash; <a href="https://github.com/{repo}" target="_blank" rel="noopener" style="color:var(--blue);text-decoration:none">{repo}</a>
  &middot; {stats['total']} open alerts &middot; Generated {generated_at} &middot; For internal use
</div>

<script>
const REPO = '{repo}';
const RAW = {js_data};

const TOTAL_DIRECT = {stats['direct']};
const TOTAL_SUB    = {stats['sub']};

let sortCol = 'priority';
let sortAsc = true;
let expandedRows = new Set();
let statusOverrides = {{}};
let openStates = {{}};

function getStatus(row) {{
  return statusOverrides[row.alertId] !== undefined ? statusOverrides[row.alertId] : row.status;
}}

function getFiltered() {{
  const sev    = document.getElementById('filter-severity').value;
  const type   = document.getElementById('filter-type').value;
  const status = document.getElementById('filter-status').value;
  const scope  = document.getElementById('filter-scope').value;
  const q      = document.getElementById('search-input').value.toLowerCase().trim();
  return RAW.filter(r => {{
    if (sev    && r.severity !== sev)          return false;
    if (type   && r.depType  !== type)         return false;
    if (scope  && r.scope    !== scope)        return false;
    if (status && getStatus(r) !== status)     return false;
    if (q && !r.pkg.toLowerCase().includes(q) && !r.summary.toLowerCase().includes(q)) return false;
    return true;
  }});
}}

function getSorted(data) {{
  return [...data].sort((a, b) => {{
    let av = a[sortCol], bv = b[sortCol];
    if (sortCol === 'status') {{ av = getStatus(a); bv = getStatus(b); }}
    if (typeof av === 'number' && typeof bv === 'number') return sortAsc ? av - bv : bv - av;
    av = String(av).toLowerCase(); bv = String(bv).toLowerCase();
    return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
}}

function sevClass(s) {{ return s.toLowerCase(); }}
function cvssClass(cvss, sev) {{
  if (sev === 'CRITICAL') return 'critical';
  if (cvss >= 7) return 'high';
  if (cvss >= 4) return 'medium';
  return '';
}}
function cvssDisplay(cvss) {{ return cvss > 0 ? cvss.toFixed(1) : 'N/A'; }}
function escHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function alertUrl(alertId) {{
  return `https://github.com/${{REPO}}/security/dependabot/${{alertId.replace('#','')}}`;
}}
const LINK_ICON = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`;

// ── TABLE ──
function renderTable() {{
  const filtered = getFiltered();
  const sorted   = getSorted(filtered);
  const tbody    = document.getElementById('table-body');
  document.getElementById('filter-count').innerHTML =
    `Showing <strong>${{filtered.length}}</strong> of ${{RAW.length}} alerts`;

  if (sorted.length === 0) {{
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><p>No alerts match the current filters.</p></div></td></tr>`;
    return;
  }}

  const rows = [];
  for (const r of sorted) {{
    const isDone     = getStatus(r) === 'Done';
    const isExpanded = expandedRows.has(r.alertId);
    const doneClass  = isDone ? ' done-row' : '';
    const expClass   = isExpanded ? ' expanded' : '';

    rows.push(`<tr class="data-row${{doneClass}}${{expClass}}" data-id="${{r.alertId}}">
      <td class="td-id"><a href="${{alertUrl(r.alertId)}}" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Open on GitHub">${{r.alertId}}${{LINK_ICON}}</a></td>
      <td class="td-pkg">${{escHtml(r.pkg)}}</td>
      <td><span class="badge ${{sevClass(r.severity)}}">${{r.severity}}</span></td>
      <td><span class="cvss-pill ${{cvssClass(r.cvss, r.severity)}}">${{cvssDisplay(r.cvss)}}</span></td>
      <td><span class="type-badge ${{r.depType === 'direct' ? 'direct' : 'sub'}}">${{r.depType === 'direct' ? 'direct' : 'sub-dep'}}</span></td>
      <td style="font-size:11px;color:var(--text-muted)">${{r.scope}}</td>
      <td style="font-size:12px;color:var(--text-secondary);max-width:180px;">${{escHtml(r.action)}}</td>
      <td><span class="status-badge ${{isDone ? 'done' : 'todo'}}" onclick="event.stopPropagation();toggleStatus('${{r.alertId}}')" title="Click to toggle">
        ${{isDone ? '&#10003; Done' : '&bull; To Do'}}
      </span></td>
    </tr>`);

    if (isExpanded) {{
      rows.push(`<tr class="detail-row" data-detail="${{r.alertId}}">
        <td colspan="8">
          <div class="detail-content">
            <div class="detail-field full"><label>Summary</label><p>${{escHtml(r.summary)}}</p></div>
            <div class="detail-field">
              <label>Alert ID</label>
              <p><code>${{r.alertId}}</code> &nbsp;
                <a href="${{alertUrl(r.alertId)}}" target="_blank" rel="noopener"
                   style="font-size:12px;color:var(--blue);text-decoration:none;display:inline-flex;align-items:center;gap:4px;vertical-align:middle"
                   onclick="event.stopPropagation()">View on GitHub ${{LINK_ICON}}</a>
              </p>
            </div>
            <div class="detail-field"><label>Package</label><p><code>${{escHtml(r.pkg)}}</code></p></div>
            <div class="detail-field"><label>Patched Version</label><p><code>${{escHtml(r.patched)}}</code></p></div>
            <div class="detail-field"><label>Scope</label><p>${{r.scope}}</p></div>
            <div class="detail-field"><label>Manifest</label><p><code>${{escHtml(r.manifest)}}</code></p></div>
            <div class="detail-field"><label>Priority</label><p>${{r.priority}}</p></div>
            <div class="detail-field"><label>Recommended Action</label>
              <p><span class="action-chip ${{r.action === 'FIX NOW' ? 'urgent' : ''}}">${{escHtml(r.action)}}</span></p>
            </div>
          </div>
        </td>
      </tr>`);
    }}
  }}

  tbody.innerHTML = rows.join('');
  tbody.querySelectorAll('tr.data-row').forEach(tr => {{
    tr.addEventListener('click', () => {{
      const id = tr.dataset.id;
      if (expandedRows.has(id)) expandedRows.delete(id);
      else expandedRows.add(id);
      renderTable();
    }});
  }});
}}

function toggleStatus(alertId) {{
  const current = statusOverrides[alertId] !== undefined
    ? statusOverrides[alertId]
    : RAW.find(r => r.alertId === alertId).status;
  statusOverrides[alertId] = current === 'Done' ? 'To Do' : 'Done';
  updateProgressCards();
  renderTable();
  renderAccordion();
}}

// ── SORT ──
document.querySelectorAll('thead th[data-col]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.col;
    if (sortCol === col) {{ sortAsc = !sortAsc; }}
    else {{ sortCol = col; sortAsc = true; }}
    document.querySelectorAll('thead th').forEach(t => {{
      t.classList.remove('sorted');
      const si = t.querySelector('.sort-indicator');
      if (si) si.remove();
    }});
    th.classList.add('sorted');
    const ind = document.createElement('span');
    ind.className = 'sort-indicator';
    ind.textContent = sortAsc ? ' ▲' : ' ▼';
    th.appendChild(ind);
    renderTable();
  }});
}});

// ── FILTERS ──
['filter-severity','filter-type','filter-status','filter-scope','search-input'].forEach(id => {{
  document.getElementById(id).addEventListener('input', () => {{ expandedRows.clear(); renderTable(); }});
}});
document.getElementById('btn-clear').addEventListener('click', () => {{
  ['filter-severity','filter-type','filter-status','filter-scope'].forEach(id => {{
    document.getElementById(id).value = '';
  }});
  document.getElementById('search-input').value = '';
  expandedRows.clear();
  renderTable();
}});

// ── BAR CHART ──
function renderBarChart() {{
  const pkgCounts = {{}};
  RAW.forEach(r => {{ pkgCounts[r.pkg] = (pkgCounts[r.pkg] || 0) + 1; }});
  const sorted = Object.entries(pkgCounts).sort((a,b) => b[1]-a[1]).slice(0, 12);
  const max = sorted[0][1];
  const container = document.getElementById('bar-chart');
  container.innerHTML = '';
  sorted.forEach(([pkg, count]) => {{
    const pct = Math.round((count / max) * 100);
    const pkgAlerts = RAW.filter(r => r.pkg === pkg);
    const topSev = pkgAlerts.some(r => r.severity === 'CRITICAL') ? 'critical'
                 : pkgAlerts.some(r => r.severity === 'HIGH') ? 'high'
                 : pkgAlerts.some(r => r.severity === 'MEDIUM') ? 'medium' : 'mixed';
    const row = document.createElement('div');
    row.className = 'bar-row';
    row.innerHTML = `
      <div class="bar-label" title="${{escHtml(pkg)}}">${{escHtml(pkg.length > 16 ? pkg.slice(0,15)+'…' : pkg)}}</div>
      <div class="bar-track"><div class="bar-fill ${{topSev}}" style="width:${{pct}}%">${{count > 3 ? count : ''}}</div></div>
      <div class="bar-count">${{count}}</div>`;
    container.appendChild(row);
  }});
}}

// ── DONUT CHART ──
function renderDonut() {{
  const runtime = RAW.filter(r => r.scope === 'runtime').length;
  const dev     = RAW.filter(r => r.scope === 'development').length;
  const total   = runtime + dev;
  const svg = document.getElementById('donut-svg');
  const cx = 80, cy = 80, r = 60, sw = 22;
  function makeArc(startFrac, endFrac, color) {{
    const startAngle = startFrac * 2 * Math.PI - Math.PI / 2;
    const endAngle   = endFrac   * 2 * Math.PI - Math.PI / 2;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const large = (endFrac - startFrac) > 0.5 ? 1 : 0;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', `M ${{x1}} ${{y1}} A ${{r}} ${{r}} 0 ${{large}} 1 ${{x2}} ${{y2}}`);
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', color);
    path.setAttribute('stroke-width', sw);
    path.setAttribute('stroke-linecap', 'round');
    svg.appendChild(path);
  }}
  const runtimeFrac = runtime / total;
  makeArc(0, runtimeFrac, '#da3633');
  makeArc(runtimeFrac + 0.01, 1, '#388bfd');
}}

// ── ACCORDION ──
function renderAccordion() {{
  const container = document.getElementById('accordion-container');
  const pkgMap = {{}};
  RAW.forEach(r => {{ (pkgMap[r.pkg] = pkgMap[r.pkg] || []).push(r); }});
  const sortedPkgs = Object.entries(pkgMap).sort((a, b) => b[1].length - a[1].length);
  container.innerHTML = '';

  for (const [pkg, alerts] of sortedPkgs) {{
    const doneCount = alerts.filter(r => getStatus(r) === 'Done').length;
    const sevOrder = {{CRITICAL:0,HIGH:1,MEDIUM:2,LOW:3}};
    const sevs = [...new Set(alerts.map(r => r.severity))].sort((a,b) => (sevOrder[a]||9)-(sevOrder[b]||9));
    const isOpen = openStates[pkg] || false;
    const pct = Math.round((doneCount / alerts.length) * 100);
    const miniBar = doneCount > 0
      ? `<div style="width:60px;height:4px;background:var(--bg-primary);border-radius:99px;overflow:hidden;display:inline-block;vertical-align:middle;margin-left:6px"><div style="height:100%;width:${{pct}}%;background:var(--green);border-radius:99px"></div></div>`
      : '';

    const item = document.createElement('div');
    item.className = `accordion-item${{isOpen ? ' open' : ''}}`;
    item.dataset.pkg = pkg;

    item.innerHTML = `
      <div class="accordion-header">
        <div class="accordion-pkg">${{escHtml(pkg)}}</div>
        <span class="accordion-count">${{alerts.length}} alert${{alerts.length>1?'s':''}}</span>
        <div class="accordion-severity-row">${{sevs.map(s=>`<span class="badge ${{sevClass(s)}}" style="font-size:10px;padding:1px 6px">${{s}}</span>`).join('')}}</div>
        <div class="accordion-impact"><strong>${{doneCount}}/${{alerts.length}}</strong> resolved${{miniBar}}</div>
        <div class="accordion-chevron">&#9660;</div>
      </div>
      <div class="accordion-body">
        <div class="acc-alert-list">
          ${{alerts.map(r => {{
            const isDone = getStatus(r) === 'Done';
            return `<div class="acc-alert" style="${{isDone ? 'opacity:.5' : ''}}">
              <div class="acc-alert-id"><a href="${{alertUrl(r.alertId)}}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${{r.alertId}}</a></div>
              <span class="badge ${{sevClass(r.severity)}}" style="font-size:10px;padding:1px 6px;flex-shrink:0">${{r.severity}}</span>
              <div class="acc-alert-summary" style="${{isDone ? 'text-decoration:line-through' : ''}}">${{escHtml(r.summary)}}</div>
              <div class="acc-alert-patched">&#8594; ${{escHtml(r.patched)}}</div>
            </div>`;
          }}).join('')}}
        </div>
      </div>`;

    item.querySelector('.accordion-header').addEventListener('click', () => {{
      const wasOpen = item.classList.contains('open');
      item.classList.toggle('open');
      openStates[pkg] = !wasOpen;
    }});
    container.appendChild(item);
  }}
}}

// ── PROGRESS ──
function updateProgressCards() {{
  const directs = RAW.filter(r => r.depType === 'direct');
  const subs    = RAW.filter(r => r.depType === 'sub-dependency');
  const directDone = directs.filter(r => getStatus(r) === 'Done').length;
  const subDone    = subs.filter(r => getStatus(r) === 'Done').length;
  document.getElementById('direct-done').textContent = directDone;
  document.getElementById('sub-done').textContent    = subDone;
  document.getElementById('direct-bar').style.width  = TOTAL_DIRECT ? `${{Math.round(directDone/TOTAL_DIRECT*100)}}%` : '0%';
  document.getElementById('sub-bar').style.width     = TOTAL_SUB    ? `${{Math.round(subDone/TOTAL_SUB*100)}}%`       : '0%';
}}

// ── INIT ──
renderBarChart();
renderDonut();
renderTable();
renderAccordion();
updateProgressCards();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    print(f"\nDependabot Dashboard Generator")
    print(f"  Repo: {repo}")

    alerts = fetch_alerts(repo)
    direct_deps = load_direct_deps(PACKAGE_JSON_PATH)
    rows = classify(alerts, direct_deps)
    stats = compute_stats(rows)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"  Stats: {stats['critical']} CRITICAL / {stats['high']} HIGH / {stats['medium']} MEDIUM / {stats['low']} LOW")
    print(f"  Direct: {stats['direct']} / Sub-dep: {stats['sub']}")

    html = build_html(repo, rows, stats, generated_at)
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    print(f"\n  Dashboard written to: {OUTPUT_PATH}")
    print(f"  Size: {len(html) // 1024} KB")


if __name__ == "__main__":
    main()
