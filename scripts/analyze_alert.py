#!/usr/bin/env python3
"""
analyze_alert.py
Standalone merge-safety scoring and AI analysis for Dependabot alerts.

Usage:
  # As module — import into generate-dashboard.py or other scripts
  from analyze_alert import score_alert, analyze_alerts_batch, enrich_alert_scores

  # As CLI — rescore a single alert or batch from JSON
  python3 scripts/analyze_alert.py --json alert.json
  python3 scripts/analyze_alert.py --batch alerts.json
  python3 scripts/analyze_alert.py --batch alerts.json --llm   # enrich via Claude API

Design:
  - Rule-based scoring is the fast default (zero API calls, deterministic)
  - LLM enrichment is opt-in: adds deeper reasoning per alert via Claude API
  - Prompt template is optimised for minimum tokens + maximum signal
  - All scoring logic lives here — generate-dashboard.py imports it
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# ─────────────────────────────────────────────────────────────
# SCORING WEIGHTS — single source of truth
# Merge-safety: start at 10, subtract penalties. Higher = safer.
# ─────────────────────────────────────────────────────────────

SCORE_WEIGHTS = {
    # Severity penalties
    "sev_critical":    8,
    "sev_high":        5,
    "sev_medium":      3,
    "sev_low":         1,

    # Runtime = production exposure
    "runtime_penalty": 2,

    # Direct dep = you own it
    "direct_penalty":  1,

    # Bump complexity
    "major_bump":      2,
    "minor_bump":      1,
    "patch_bump":      0,

    # CVSS severity boosters
    "cvss_9plus":      2,
    "cvss_8plus":      1,
}

# Grade thresholds — higher score = safer
GRADE_THRESHOLDS = [
    (8, "good",   "Safe to merge"),
    (5, "warn",   "Review first"),
    (3, "alert",  "Needs attention"),
    (0, "danger", "Block / fix first"),
]


# ─────────────────────────────────────────────────────────────
# RULE-BASED SCORER
# ─────────────────────────────────────────────────────────────

def score_alert(alert: dict) -> dict:
    """
    Compute merge-safety score for a single alert row.

    Args:
        alert: dict with keys: severity, scope, depType, bumpType, cvss,
               pkg, vulnRange, patched (+ optional: functionName, summary)

    Returns:
        dict with: score (int 0-10), grade (str), gradeLabel (str), reason (str)
    """
    w = SCORE_WEIGHTS
    penalty = 0

    sev = alert.get("severity", "MEDIUM")
    penalty += w.get(f"sev_{sev.lower()}", 1)

    if alert.get("scope") == "runtime":
        penalty += w["runtime_penalty"]

    if alert.get("depType") == "direct":
        penalty += w["direct_penalty"]

    bump = alert.get("bumpType", "unknown")
    penalty += w.get(f"{bump}_bump", 0)

    cvss = float(alert.get("cvss", 0))
    if cvss >= 9.0:
        penalty += w["cvss_9plus"]
    elif cvss >= 8.0:
        penalty += w["cvss_8plus"]

    score = max(0, 10 - penalty)

    # Determine grade
    grade = "danger"
    grade_label = "Block / fix first"
    for threshold, g, label in GRADE_THRESHOLDS:
        if score >= threshold:
            grade = g
            grade_label = label
            break

    reason = _build_reason(alert, sev, bump, score)

    return {
        "score": score,
        "grade": grade,
        "gradeLabel": grade_label,
        "reason": reason,
    }


def _build_reason(alert: dict, sev: str, bump: str, score: int) -> str:
    """Build concise human-readable reason string."""
    parts = []

    pkg = alert.get("pkg", "unknown")
    scope = alert.get("scope", "development")
    dep_type = alert.get("depType", "sub-dependency")

    dep_context = "ships in production" if scope == "runtime" else "build/CI only"
    own_context = "direct dep" if dep_type == "direct" else "transitive (sub-dep)"
    parts.append(f"{sev.lower()} severity, {dep_context}, {own_context}.")

    vuln_range = alert.get("vulnRange", "")
    patched = alert.get("patched", "")
    if patched:
        parts.append(f"Fix: upgrade to {patched}.")

    if bump == "major":
        parts.append("Major bump — breaking changes likely. Read migration guide.")
    elif bump == "minor":
        parts.append("Minor bump — check changelog, no breaking changes expected.")
    elif bump == "patch":
        parts.append("Patch only — security fix, no API changes. Low merge risk.")

    if scope == "runtime" and dep_type == "direct" and sev in ("CRITICAL", "HIGH"):
        parts.append("Runtime + direct + high severity — verify in staging before prod.")
    elif scope == "runtime" and dep_type == "sub-dependency":
        parts.append("Transitive — upgrade parent package or add resolution override.")
    elif scope == "development":
        parts.append("Dev-only — not exploitable at runtime. Batch-fix OK.")

    return " ".join(parts)


def enrich_alert_scores(rows: list[dict]) -> None:
    """Add aiScore, aiGrade, aiReason fields in-place to every row."""
    for r in rows:
        result = score_alert(r)
        r["aiScore"] = result["score"]
        r["aiGrade"] = result["grade"]
        r["aiReason"] = result["reason"]


# ─────────────────────────────────────────────────────────────
# LLM ENRICHMENT — Claude API (opt-in)
# ─────────────────────────────────────────────────────────────
# Prompt design principles:
#   1. Minimum tokens — structured input, structured output
#   2. No chat fluff — system prompt is direct instruction
#   3. Few-shot via schema, not examples (saves tokens)
#   4. One alert per call (parallelisable) or batch mode
#   5. JSON output only — no prose in response

_SYSTEM_PROMPT = """\
You are a dependency vulnerability analyst. Given a Dependabot alert, produce a merge-safety assessment.

Rules:
- Score 0-10: 10 = zero risk to merge, 0 = must block
- Consider: severity, CVSS, scope (runtime vs dev), dep type (direct vs transitive), bump size, known exploit patterns
- Reason: 2-3 sentences max. Be specific about WHY this score. Mention the key risk factor.
- If patch bump + dev-only + low severity → score should be 8-10
- If critical + runtime + direct + major bump → score should be 0-2

Output JSON only, no markdown fences:
{"score": <int 0-10>, "grade": "<good|warn|alert|danger>", "reason": "<2-3 sentences>"}"""

_USER_TEMPLATE = """\
pkg: {pkg}
severity: {severity}
cvss: {cvss}
scope: {scope}
depType: {depType}
bumpType: {bumpType}
vulnRange: {vulnRange}
patched: {patched}
summary: {summary}"""


def build_llm_prompt(alert: dict) -> dict:
    """
    Build the messages payload for Claude API call.
    Returns dict with 'system' and 'messages' keys ready for API.

    Token budget: ~150 input tokens per alert (system cached across batch).
    Expected output: ~60-80 tokens.
    """
    user_msg = _USER_TEMPLATE.format(
        pkg=alert.get("pkg", ""),
        severity=alert.get("severity", ""),
        cvss=alert.get("cvss", 0),
        scope=alert.get("scope", ""),
        depType=alert.get("depType", ""),
        bumpType=alert.get("bumpType", ""),
        vulnRange=alert.get("vulnRange", ""),
        patched=alert.get("patched", ""),
        summary=alert.get("summary", "")[:120],
    )

    return {
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 150,
        "temperature": 0,
    }


def analyze_with_llm(alert: dict, api_key: str | None = None) -> dict:
    """
    Call Claude API for single alert analysis.
    Falls back to rule-based if no API key or on error.

    Requires: anthropic package installed, ANTHROPIC_API_KEY env var or api_key param.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return score_alert(alert)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        prompt = build_llm_prompt(alert)

        response = client.messages.create(
            model=prompt["model"],
            max_tokens=prompt["max_tokens"],
            temperature=prompt["temperature"],
            system=prompt["system"],
            messages=prompt["messages"],
        )

        text = response.content[0].text.strip()
        result = json.loads(text)

        # Validate
        score = max(0, min(10, int(result.get("score", 5))))
        grade = result.get("grade", "warn")
        if grade not in ("good", "warn", "alert", "danger"):
            grade = "warn"
        reason = result.get("reason", "")[:300]

        return {"score": score, "grade": grade, "gradeLabel": _grade_label(grade), "reason": reason}

    except Exception as e:
        print(f"  LLM analysis failed for {alert.get('pkg', '?')}: {e}", file=sys.stderr)
        return score_alert(alert)


def analyze_alerts_batch(
    alerts: list[dict],
    use_llm: bool = False,
    api_key: str | None = None,
) -> list[dict]:
    """
    Score a batch of alerts. Returns list of result dicts (same order).

    Args:
        alerts: list of alert row dicts
        use_llm: if True, call Claude API for each (slower, richer)
        api_key: optional, falls back to ANTHROPIC_API_KEY env var
    """
    results = []
    for alert in alerts:
        if use_llm:
            results.append(analyze_with_llm(alert, api_key))
        else:
            results.append(score_alert(alert))
    return results


def _grade_label(grade: str) -> str:
    for _, g, label in GRADE_THRESHOLDS:
        if g == grade:
            return label
    return grade


# ─────────────────────────────────────────────────────────────
# DEEP ANALYSIS — richer Claude prompt for high-risk alerts
# Used by run_deep_analysis.py, not by the default dashboard flow.
# Token budget: ~350 input / ~500 output per call.
# Output is a structured dict (summary, affected, risks, testing, flow)
# that renders as the "AI Safety Analysis" section in the dashboard.
# ─────────────────────────────────────────────────────────────

_DEEP_SYSTEM_PROMPT = """\
You are a senior engineer reviewing a Dependabot alert before upgrading a production dependency. Your job is to help the engineer upgrade safely.

Given an alert, produce a structured analysis with these sections:

1. VULNERABILITY SUMMARY (2 sentences) — what the CVE actually is, in plain terms
2. AFFECTED CODE PATHS (2-3 bullets) — common usage patterns of this package that are at risk
3. MIGRATION RISKS (2-3 bullets) — breaking changes likely in this bump size, what API surface to check
4. WHAT TO TEST (3-4 bullets) — concrete test cases the engineer should run before merging
5. UPGRADE FLOW (3-5 numbered steps) — recommended sequence: backup, update, smoke test, deploy

Be concrete. Name specific APIs, functions, or config when possible. If the package is very common (lodash, requests, aiohttp), lean on your knowledge of its typical usage.

Output valid JSON only, no markdown fences, no commentary:
{"summary": "...", "affected": ["...", "..."], "risks": ["...", "..."], "testing": ["...", "...", "..."], "flow": ["1. ...", "2. ..."]}"""

_DEEP_USER_TEMPLATE = """\
pkg: {pkg}
severity: {severity}
cvss: {cvss}
scope: {scope}
depType: {depType}
bumpType: {bumpType}
vulnRange: {vulnRange}
patched: {patched}
manifest: {manifest}
summary: {summary}"""

_DEEP_MODEL = "claude-sonnet-4-20250514"
_DEEP_MAX_TOKENS = 800


def build_deep_llm_prompt(alert: dict) -> dict:
    """Build Claude API payload for deep analysis of a single alert."""
    user_msg = _DEEP_USER_TEMPLATE.format(
        pkg=alert.get("pkg", ""),
        severity=alert.get("severity", ""),
        cvss=alert.get("cvss", 0),
        scope=alert.get("scope", ""),
        depType=alert.get("depType", ""),
        bumpType=alert.get("bumpType", ""),
        vulnRange=alert.get("vulnRange", ""),
        patched=alert.get("patched", ""),
        manifest=alert.get("manifest", ""),
        summary=(alert.get("summary", "") or "")[:200],
    )
    return {
        "system": _DEEP_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
        "model": _DEEP_MODEL,
        "max_tokens": _DEEP_MAX_TOKENS,
        "temperature": 0,
    }


def deep_analyze_with_llm(alert: dict, api_key: str | None = None) -> dict | None:
    """
    Call Claude API for structured deep analysis of a single alert.
    Returns dict with keys: summary, affected, risks, testing, flow — or None on failure.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("  Deep analysis skipped: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        prompt = build_deep_llm_prompt(alert)

        response = client.messages.create(
            model=prompt["model"],
            max_tokens=prompt["max_tokens"],
            temperature=prompt["temperature"],
            system=prompt["system"],
            messages=prompt["messages"],
        )

        text = response.content[0].text.strip()
        # Strip markdown fences defensively
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
        result = json.loads(text)

        required = ("summary", "affected", "risks", "testing", "flow")
        for key_name in required:
            if key_name not in result:
                print(f"  Deep analysis for {alert.get('pkg', '?')} missing key: {key_name}", file=sys.stderr)
                return None
        return result

    except Exception as e:
        print(f"  Deep analysis failed for {alert.get('pkg', '?')}: {e}", file=sys.stderr)
        return None


def load_deep_cache(path: str) -> dict:
    """Load deep-analysis cache from disk. Empty dict if missing."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_deep_cache(path: str, cache: dict) -> None:
    """Atomic write of deep-analysis cache."""
    import tempfile
    dir_ = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tmp:
        json.dump(cache, tmp, indent=2, sort_keys=True)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="Score Dependabot alerts for merge safety")
    parser.add_argument("--json", help="Single alert JSON file")
    parser.add_argument("--batch", help="JSON file with array of alerts")
    parser.add_argument("--llm", action="store_true", help="Use Claude API for enrichment")
    parser.add_argument("--deep", action="store_true", help="Use deep-analysis prompt (structured output)")
    parser.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    args = parser.parse_args()

    if not args.json and not args.batch:
        parser.print_help()
        sys.exit(1)

    if args.json:
        alert = json.loads(open(args.json).read())
        if args.deep:
            result = deep_analyze_with_llm(alert, args.api_key)
        elif args.llm:
            result = analyze_with_llm(alert, args.api_key)
        else:
            result = score_alert(alert)
        print(json.dumps(result, indent=2))

    elif args.batch:
        alerts = json.loads(open(args.batch).read())
        if args.deep:
            results = [deep_analyze_with_llm(a, args.api_key) for a in alerts]
        else:
            results = analyze_alerts_batch(alerts, use_llm=args.llm, api_key=args.api_key)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    _cli()
