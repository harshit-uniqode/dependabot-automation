# INFO — File-by-file directory

A human-readable map of every non-generated file in this repository. When
you're looking for "where is the thing that does X?", start here.

> Conventions: shell scripts use dashes (`analyze-package-changelog.sh`);
> Python modules use underscores (`analyze_alert.py`); config files are
> descriptive prose (`floci-emulator.compose.yml`).

---

## Root

| File            | What it is                                                 |
| --------------- | ---------------------------------------------------------- |
| `README.md`     | Getting-started guide. Start here.                         |
| `INFO.md`       | This file — file-by-file index.                            |
| `LICENSE`       | MIT license.                                               |
| `Makefile`      | All CLI entry points. See table at top of the file.        |
| `.env.example`  | Template for optional env vars (copy to `.env`).           |
| `.gitignore`    | Python caches, OS junk, deep-analysis cache, emulator data.|

---

## `config/` — user-editable configuration

| File                  | What it is                                                                  |
| --------------------- | --------------------------------------------------------------------------- |
| `wizard-config.json`  | Server port + list of repos to track. Each dev edits paths to match their checkout. |
| `lambda-env-vars.json`| Env-var overrides passed to Lambdas when invoked by `scripts/invoke-and-verify-lambda.sh`. |

---

## `docker/` — Docker Compose files for AWS emulators

| File                               | What it is                                                    |
| ---------------------------------- | ------------------------------------------------------------- |
| `floci-emulator.compose.yml`       | **Default** emulator. Free, MIT-licensed, zero-auth. Covers S3/SQS/DynamoDB/Lambda/SNS/SES/API Gateway/KMS/CloudWatch. |
| `localstack-emulator.compose.yml`  | Fallback. Requires `LOCALSTACK_AUTH_TOKEN` for Pro features.  |

Why Floci by default: no signup, no auth token, and it covers every service
we need for Dependabot validation. LocalStack is the industry standard but
needs a paid tier for Lambda + SQS together.

---

## `wizard_server/` — Python HTTP server (port 8787)

The dashboards are served from here, and every dashboard button calls a
`/api/...` endpoint in this package.

| File               | What it is                                                                    |
| ------------------ | ----------------------------------------------------------------------------- |
| `__main__.py`      | CLI entry. `python3 -m wizard_server [--host H] [--port P]`                   |
| `__init__.py`      | Package marker.                                                               |
| `server.py`        | Stdlib HTTP server. Defines the router — all URL paths live here.             |
| `api.py`           | Handlers for `/api/analyze` (npm-why, pip-show) + `/api/test-upgrade` (launches a pipeline). |
| `lambda_tester.py` | Everything the **Local Testing** tab needs: start/stop emulator, list Lambdas, build+zip, deploy, invoke, poll job. |
| `jobs.py`          | `JobQueue` — serial `ThreadPoolExecutor`-backed async job runner with TTL + cancellation. |
| `pipelines.py`     | Upgrade pipeline: create branch → apply upgrade → `npm install`/`pip install` → build → tests → (optional) integration test on emulator. |
| `repos.py`         | On startup, scans each configured repo — detects type (angular/lambda_monorepo/…) and, for Lambda monorepos, introspects every `serverless.yml` to find runtime, handler, triggers, services, test command. |
| `config_schema.py` | Loads `config/wizard-config.json`, fills in defaults, validates repo paths exist. |

### Data flow (what happens when you click a button)

```
Browser (dashboard HTML)
  │
  └── fetch('/api/lambda/deploy', {repo, function})
        │
        ▼
  server.py  → routes to lambda_tester.deploy(fn_meta)
        │
        ▼
  lambda_tester.py  → spawns thread
        ├── build_zip()   (npm install, tsc, zip)
        ├── awslocal lambda create-function
        └── awslocal lambda wait function-active-v2
        │
  _jobs[id] updated with status/logs/result
        │
  Browser polls GET /api/lambda/job/<id>  until status==done
```

---

## `scripts/` — command-line helpers

### Dashboard generation

| File                                 | What it does                                             |
| ------------------------------------ | -------------------------------------------------------- |
| `generate_dashboard.py`              | The star of the show. Fetches Dependabot alerts via `gh api`, classifies direct vs sub-dependency, enriches with bump-type / changelog URL / git blame / merge-safety score, and writes a self-contained HTML file. |
| `refresh-lambda-dashboard.sh`        | Thin wrapper: `gh auth` check → `generate_dashboard.py` → open in browser. |
| `refresh-angular-dashboard.sh`       | Same, for the Angular repo.                              |

### Alert scoring & analysis

| File                                 | What it does                                             |
| ------------------------------------ | -------------------------------------------------------- |
| `analyze_alert.py`                   | Library + CLI. Contains the single source of truth for merge-safety scoring weights (`SCORE_WEIGHTS`), the rule-based scorer (`score_alert`), and Claude API integration for deep analysis. Imported by `generate_dashboard.py` and `run_deep_analysis.py`. |
| `run_deep_analysis.py`               | Filtered + cached wrapper. For each alert above a filter threshold, calls Claude to produce structured analysis (summary / affected code / risks / testing / flow). Writes cache to `vulnerability-dashboards/.deep-analysis-cache.json`. |
| `analyze-package-changelog.sh`       | Prints changelog URL for a npm/pip package bump. Used by the dashboard's "changelog" link and for manual triage. |
| `git_blame_lookup.py`                | Returns last-commit author for a subdirectory in a local git repo. Used to show "who last touched this Lambda" in the expanded alert row. |

### Local Lambda testing

| File                                 | What it does                                             |
| ------------------------------------ | -------------------------------------------------------- |
| `setup-emulator-aws-resources.sh`    | Creates test S3 buckets, SQS queues, DynamoDB tables, SNS topics on the emulator. Edit the arrays inside to match your Lambda's actual resources. Runs automatically on `make emulator-up`. |
| `teardown-emulator-aws-resources.sh` | The inverse — removes all test resources.                |
| `invoke-and-verify-lambda.sh`        | Invokes a deployed Lambda and optionally asserts S3/SQS side effects. Useful for end-to-end test scripts. |

---

## `lambda-test-events/` — sample event payloads

Each JSON is a canonical AWS event shape your Lambda would receive when
triggered by that service. Pick the right one from the dashboard's **Local
Testing** tab, or pass with `EVENT=lambda-test-events/<file>.json`.

| File                          | Mirrors a real…                                     |
| ----------------------------- | --------------------------------------------------- |
| `api-gateway-event.json`      | HTTP request through API Gateway                    |
| `dynamodb-stream-event.json`  | DynamoDB stream record (INSERT/MODIFY/REMOVE)       |
| `s3-event.json`               | S3 `ObjectCreated:Put` notification                 |
| `sqs-event.json`              | Generic SQS message batch                           |
| `sqs-email-service.json`      | SQS event tailored for `beaconstac_email_service`   |

To add a new trigger type, drop a JSON file into this folder — the dashboard
auto-discovers it via `lambda_tester.list_events()`.

---

## `vulnerability-dashboards/` — generated HTML + audit CSVs

| File                     | What it is                                                      |
| ------------------------ | --------------------------------------------------------------- |
| `angular-dashboard.html` | Generated. Open in browser. Regenerate with `make refresh-angular`. |
| `lambda-dashboard.html`  | Generated. Contains the extra **Local Testing** tab.            |
| `hit-list.csv`           | Static snapshot of currently-open alerts — committed to git for audit trail. |
| `risk-register.csv`      | Alerts we've accepted as risk (with approver name + date) — also committed. |

The `.deep-analysis-cache.json` that sometimes appears here is
git-ignored — it's LLM-generated and can always be rebuilt.

---

## `docs/` — deep dives

Short, topic-focused docs. Pick the one that matches what you're trying to
understand.

| File                                    | When to read it                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------ |
| `local-lambda-testing-guide.md`         | Using the **Local Testing** tab or the CLI flow. Covers Floci internals, handler-path resolution, adding new test events. |
| `dashboard-filtering-workflow.md`       | Using the dashboard — filters, scoring columns, risk-register, onboarding a new repo. |
| `dependency-classification-logic.md`    | How direct-vs-sub-dependency is detected, how the merge-safety score is calculated, how the data pipeline works. |

---

## Files NOT included

- `.localtest_out/` — Lambda invoke output artefacts (git-ignored).
- `.localtest_pkg/` — Python build scratch directory (git-ignored).
- `localstack-data/` — LocalStack persistence volume (git-ignored).
- `function.zip` — built Lambda artefact, created per-deploy (git-ignored).
- `__pycache__/` — Python bytecode (git-ignored).
