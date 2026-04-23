# Filtering Logic & Workflow

How `generate_dashboard.py` and the dashboard's in-browser filter system work.

---

## Overview

```
gh API (Dependabot alerts)
        │
        ▼
  fetch_alerts()          — raw alert objects, state == "open" only
        │
        ▼
  load_direct_deps()      — repo-specific: angular (package.json) | lambda (all fn dirs)
        │
        ▼
  classify()              — enrich each alert → dashboard row dict
    ├─ dep_type           — "direct" | "sub-dependency"  (via direct_deps set)
    ├─ bumpType           — "patch" | "minor" | "major"  (via _parse_bump_type)
    ├─ functionName       — lambda function dir from manifest_path
    ├─ changelogUrl       — npm view / PyPI JSON API (cached per package)
    └─ action / priority  — ACTION_MAP / PRIORITY_MAP lookups
        │
        ▼
  compute_stats()         — aggregate counts for header cards
        │
        ▼
  rows_to_js()            — embed as const RAW = [...] in the HTML
        │
        ▼
  build_html()            — single-file self-contained dashboard
```

---

## Direct vs Sub-dependency Classification

**Rule:** a package is `direct` if its name appears in the repo's known direct-dependency set.

For **Angular Portal** (`deps_mode = "angular"`):
- Reads `bac-app/package.json`
- Collects keys from `dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies`

For **Lambda Functions** (`deps_mode = "lambda_monorepo"`):
- Scans every subdirectory that contains `serverless.yml`
- Per function dir: reads `requirements.txt` (pip, normalised to lowercase) and `package.json` (npm)
- Union of all deps across all functions = the "direct" set
- This means: if _any_ function lists `axios` directly, all `axios` alerts are classified as direct

**Pip normalisation:** `pip` package names are lowercased and `-` → `_` to match Dependabot's reporting (e.g. `langchain-openai` in Dependabot = `langchain_openai` in requirements.txt).

---

## Version Bump Classification

```
vulnerable_version_range: ">= 1.0.0, < 1.15.0"
patched:                  "1.15.0"
```

Algorithm (`_parse_bump_type`):

1. Extract **lower bound** from range via `>= x.y.z` regex.
   If no lower bound (e.g. `< 2.0.0` only), use `0.0.0`.
2. Parse both versions to `[major, minor, patch]` integer tuples.
3. Compare:

| Condition               | bump_type |
|-------------------------|-----------|
| major differs           | `major`   |
| minor differs           | `minor`   |
| only patch differs      | `patch`   |
| parse error / no data   | `unknown` |

**Practical meaning for triage:**
- `patch` — typically safe to auto-merge; no API surface changes expected
- `minor` — review changelog; new features may change behaviour subtly
- `major` — expect breaking changes; plan upgrade carefully, check changelog

**Filter use case:** set Bump = "patch" to see all alerts fixable with a low-risk update.
These are the best candidates for automated dependency-update PRs.

---

## Changelog URL Resolution

Called once per unique `(ecosystem, package)` pair during generation; results are cached in `_changelog_cache`.

**npm packages:**
1. `npm view {pkg} repository.url` → strips `git+` prefix, `.git` suffix
2. If GitHub URL found → append `/releases`
3. Fallback → `https://www.npmjs.com/package/{pkg}?activeTab=versions`

**pip packages:**
1. `curl https://pypi.org/pypi/{pkg}/json` → parse `info.project_urls`
2. Look for keys: `Changelog`, then `Source`, then `Homepage`
3. If GitHub URL → append `/releases`
4. Fallback → `https://pypi.org/project/{pkg}/#history`

Timeout: 6 s per package. Network failures fall back to the registry URL silently.

---

## In-Browser Filter System

All filtering is pure JS, operating on the static `RAW` array embedded at generation time.
No server round-trips after page load.

### Filter dimensions

| Filter ID         | Field matched      | Notes                                        |
|-------------------|--------------------|----------------------------------------------|
| `filter-severity` | `r.severity`       | CRITICAL / HIGH / MEDIUM / LOW               |
| `filter-type`     | `r.depType`        | direct / sub-dependency                      |
| `filter-bump`     | `r.bumpType`       | patch / minor / major                        |
| `filter-eco`      | `r.eco`            | npm / pip                                    |
| `filter-status`   | `getStatus(r)`     | To Do / Done — reads session-local overrides |
| `filter-scope`    | `r.scope`          | runtime / development                        |
| `search-input`    | pkg + function + summary | case-insensitive substring           |

All filters AND-combine: every active filter must match for a row to show.

### Status overrides

Clicking a row's status badge toggles it in a session-local `statusOverrides` map:
```js
statusOverrides[alertId] = current === 'Done' ? 'To Do' : 'Done'
```
This is in-memory only — refreshing the page resets all statuses to "To Do".
For persistent tracking, export to the `vulnerability-dashboards/risk-register.csv`.

### Sorting

Clicking a column header sets `sortCol` and toggles `sortAsc`. All columns support both directions.
Numeric columns (`cvss`, `priority`) use numeric comparison; all others use `localeCompare`.
Default sort: `priority` ascending, then `-cvss` descending, then package name.

---

## Adding a New Repo

1. Add an entry to `REPO_CONFIGS` in `generate_dashboard.py`:

```python
"owner/repo-name": {
    "label":      "My Repo",
    "local_path": "/path/to/local/clone",
    "deps_mode":  "lambda_monorepo",   # or "angular" or "unknown"
    "output":     _PROJECT_ROOT / "vulnerability-dashboards" / "myrepo-dashboard.html",
},
```

2. Add a refresh script:

```bash
cp scripts/refresh-lambda-dashboard.sh scripts/refresh-myrepo-dashboard.sh
# Edit the repo slug and output file references
```

3. `deps_mode` options:
   - `"angular"` — reads `{local_path}/{package_dir}/package.json`
   - `"lambda_monorepo"` — scans subdirs with `serverless.yml`
   - `"unknown"` — no direct-dep loading; all alerts classified as sub-dependency

---

## Operational Workflow

```
1. [Weekly / on Dependabot alert] Run refresh script
   ./scripts/refresh-angular-dashboard.sh           # angular portal
   ./scripts/refresh-lambda-dashboard.sh    # lambda functions

2. [Triage] Open dashboard, filter by:
   - Bump = "patch" → easy wins, safe auto-merge candidates
   - Type = "direct" + Severity = CRITICAL/HIGH → must fix this sprint
   - Ecosystem = "pip" → lambda Python deps (check function column)

3. [Investigate] Click any row to expand:
   - Vulnerable range shows exactly which versions are affected
   - Changelog link opens GitHub releases or PyPI history
   - Function name shows which Lambda function is impacted

4. [Remediate] Use analyze-changelog.sh for deep analysis:
   ./scripts/analyze-changelog.sh npm axios 1.0.0 1.15.0

5. [Track] Toggle row status to Done as fixes land.
   Persist decisions in vulnerability-dashboards/risk-register.csv.
```

---

## Key Files

| Path | Purpose |
|------|---------|
| `scripts/generate_dashboard.py` | Main generator — fetch, classify, build HTML |
| `scripts/refresh-angular-dashboard.sh` | Regenerate Angular Portal dashboard |
| `scripts/refresh-lambda-dashboard.sh` | Regenerate Lambda Functions dashboard |
| `scripts/analyze-changelog.sh` | Deep changelog analysis helper |
| `vulnerability-dashboards/angular-dashboard.html` | Angular Portal dashboard (generated) |
| `vulnerability-dashboards/lambda-dashboard.html` | Lambda Functions dashboard (generated) |
| `vulnerability-dashboards/risk-register.csv` | Persistent tracking for audit trail |
| `wizard-config.json` | Wizard server config (repos + upgrade jobs) |
