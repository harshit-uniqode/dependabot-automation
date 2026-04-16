# Dependency Classification Logic

Owner: Harshit | Repo: beaconstac_angular_portal | Last updated: 2026-04-16

This document records the exact logic used to classify Dependabot alerts as direct vs
sub-dependencies, assign priorities, and determine recommended actions.

---

## The core problem

GitHub's Dependabot API returns alerts for every vulnerable package in `package-lock.json`
(or `requirements.txt`). That file includes **both** packages you explicitly declared
(in `package.json` `dependencies`/`devDependencies`) **and** packages those packages
pulled in transitively.

Dependabot does not reliably distinguish between the two in its API response. The
`dependency.scope` field tells you `runtime` vs `development`, but not whether you
own the dependency directly.

This matters because:
- **Direct deps**: you can upgrade them yourself in `package.json`. One line change.
- **Sub-deps (transitive)**: you cannot pin them directly. You must upgrade the
  parent package that pulls them in, or add a `resolutions`/`overrides` block.

---

## Classification: Direct vs Sub-dependency

### Step 1 — Build the direct dep set

Read `bac-app/package.json` and collect all package names from these four fields:

```json
{
  "dependencies":         { ... },
  "devDependencies":      { ... },
  "peerDependencies":     { ... },
  "optionalDependencies": { ... }
}
```

This gives the **authoritative set** of packages the team explicitly manages.

Script location: `scripts/generate-dashboard.py`, function `load_direct_deps()`.

### Step 2 — Compare each alert's package name against the set

```python
dep_type = "direct" if pkg in direct_deps else "sub-dependency"
```

Simple membership check. Package names are exact strings — `lodash` and `lodash-es`
are separate entries.

### Current direct deps with vulnerabilities (as of 2026-04-16)

| Package | Vulnerability count | Severity |
|---------|-------------------|----------|
| `@angular/core` | 3 | HIGH |
| `@angular/compiler` | 3 | HIGH |
| `@angular/common` | 1 | HIGH |
| `lodash` | 3 | HIGH + MEDIUM |
| `lodash-es` | 3 | HIGH + MEDIUM |
| `storybook` | 2 | HIGH |
| `dompurify` | 4 | MEDIUM |

All 19 direct-dep alerts are fixable via `npm install <pkg>@<patched-version>` or
`ng update` (for Angular packages).

---

## Priority Assignment

Each alert gets a numeric priority (1 = highest urgency):

| Severity | Dep Type | Priority | Action |
|----------|----------|----------|--------|
| CRITICAL | direct | 1 | FIX NOW |
| CRITICAL | sub-dependency | 1 | FIX NOW |
| HIGH | direct | 2 | FIX - direct |
| HIGH | sub-dependency | 3 | FIX - sub-dep |
| MEDIUM | direct | 4 | FIX - medium direct |
| MEDIUM | sub-dependency | 5 | DEFER or ignore if dev-only |
| LOW | direct | 6 | BATCH end of quarter |
| LOW | sub-dependency | 6 | BATCH end of quarter |

Secondary sort within the same priority: CVSS score descending (higher = more urgent).

Rationale for CRITICAL being priority 1 regardless of dep type: a CRITICAL sub-dep
is still exploitable even if you don't own it directly — it will be included in your
build/runtime bundle.

---

## Scope: runtime vs development

The Dependabot API returns `dependency.scope`:
- `runtime` — package is in `dependencies` (ships to production)
- `development` — package is in `devDependencies` (CI/build only)

**Impact on risk:**
- `runtime` + `HIGH`/`CRITICAL` = highest real-world risk. Affects production users.
- `development` + any severity = lower risk. Vulnerability is only exploitable during
  local dev or CI builds, not in the deployed app.

The dashboard shows scope as a filter column and the "Prioritization note" in the
donut chart calls this out explicitly.

---

## Recommended action logic

The `action` field in the hit-list and dashboard is derived from the priority table above:

```python
ACTION_MAP = {
    ("CRITICAL", "direct"):         "FIX NOW",
    ("CRITICAL", "sub-dependency"): "FIX NOW",
    ("HIGH",     "direct"):         "FIX - direct",
    ("HIGH",     "sub-dependency"): "FIX - sub-dep",
    ("MEDIUM",   "direct"):         "FIX - medium direct",
    ("MEDIUM",   "sub-dependency"): "DEFER or ignore if dev-only",
    ("LOW",      "direct"):         "BATCH end of quarter",
    ("LOW",      "sub-dependency"): "BATCH end of quarter",
}
```

**"DEFER or ignore if dev-only"** means: if scope=development AND sub-dependency AND
severity=MEDIUM or LOW, the practical risk is very low. Record it, but don't block a
sprint on it.

---

## How to fix sub-dependencies

Sub-deps cannot be pinned in `package.json` directly. Two approaches:

### Option A — Upgrade the parent package
Run `npm why <vulnerable-package>` to find which direct dep pulls it in. Upgrade that
direct dep to a version that uses a non-vulnerable transitive dep.

Example: `handlebars` is a sub-dep of `@storybook/*`. Upgrading storybook to v9.1.19+
pulls in `handlebars@4.7.9+` which patches the CRITICAL injection.

### Option B — npm overrides (last resort)
If upgrading the parent is not possible (breaking changes), force a minimum version:

```json
// package.json
{
  "overrides": {
    "handlebars": ">=4.7.9"
  }
}
```

Yarn equivalent: `resolutions`. This overrides the transitive dep version globally.
Use sparingly — it can break the parent package if the override is incompatible.

---

## Data pipeline: generate-dashboard.py

```
gh api repos/{owner}/{repo}/dependabot/alerts --paginate
         │
         ▼
Filter: state == "open"
         │
         ▼
For each alert:
  - Extract: number, package.name, severity, cvss.score, scope, manifest_path,
             first_patched_version, summary
  - Classify: pkg in direct_deps → "direct" else "sub-dependency"
  - Assign: action, priority from ACTION_MAP / PRIORITY_MAP
         │
         ▼
Sort: priority ASC, then CVSS DESC
         │
         ▼
compute_stats() → counts per severity, dep type, scope
         │
         ▼
build_html() → inline JS data array + full dashboard HTML
         │
         ▼
Write: vulnerability-tracker/dashboard.html
```

---

## Known edge cases

| Case | How handled |
|------|-------------|
| CVSS score missing (new advisories) | Stored as 0, displayed as "N/A" |
| `first_patched_version` not yet available | Stored as empty string |
| Package name has org scope (`@angular/core`) | Handled — exact string match against package.json keys |
| Alert for a package not in package.json at all | Classified as sub-dependency (conservative) |
| Multiple alerts for same package | Each alert is a separate row (different CVEs) |

---

## Refresh workflow

```bash
# One command — fetches live data, regenerates HTML, opens browser
./scripts/refresh-dashboard.sh

# Override repo (for other repos)
./scripts/refresh-dashboard.sh owner/other-repo
```

The script requires `gh` CLI authenticated with `repo` scope. It reads
`package.json` from the local filesystem path configured in `generate-dashboard.py`
(`PACKAGE_JSON_PATH`). Update that path if the repo is cloned elsewhere.

---

## Files

| File | Purpose |
|------|---------|
| `scripts/generate-dashboard.py` | Main generator — fetches, classifies, builds HTML |
| `scripts/refresh-dashboard.sh` | One-command wrapper — auth check + run + open browser |
| `vulnerability-tracker/dashboard.html` | Generated output — open directly in browser |
| `vulnerability-tracker/hit-list.csv` | Static snapshot — committed to git for record-keeping |
| `vulnerability-tracker/risk-register.csv` | Risk-accepted items with Raghav approval |
