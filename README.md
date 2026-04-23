# Dependabot Vulnerability Remediation

Owner: Harshit (harshit.ky@uniqode.com) | Reviewer: Raghav | Status: Active

Operational hub for tracking + fixing Dependabot alerts across:
- `mobstac-private/beaconstac_angular_portal` (Angular)
- `mobstac-private/beaconstac_lambda_functions` (48 Lambdas — Node + Python)

What's in here:

- **Vulnerability dashboards** — auto-generated HTML, open in browser
  - `vulnerability-tracker/dashboard.html` — Angular repo alerts
  - `vulnerability-tracker/lambda-dashboard.html` — Lambda repo alerts + **Local Testing tab**
- **Wizard server** (`wizard_server/`) — Python HTTP server, backs the dashboards
- **Local Lambda testing** — Floci emulator, dashboard-driven deploy + invoke
- **Scripts** — classify/triage/resolve alerts
- **Docs** — `docs/testing-workflow.md`, `docs/local-lambda-testing.md`

---

## Quick start

### 1. Prerequisites

Install once on your machine:

```bash
# GitHub CLI — fetches live Dependabot alerts
brew install gh
gh auth login                   # needs repo scope

# Python 3 — used by dashboard generator + wizard server
python3 --version               # 3.9+

# AWS CLI
brew install awscli

# awslocal — pipx isolates it from system Python (PEP 668)
brew install pipx
pipx install awscli-local
pipx ensurepath                 # restart terminal after

# Verify
which aws awslocal              # both should resolve
docker ps                       # Docker Desktop must be running
```

SAM CLI is NO longer required — the Local Testing tab uses Floci + awslocal directly.

### 2. Clone this repo

```bash
git clone https://github.com/harshit-uniqode/dependabot-automation.git
cd dependabot-automation
```

### 3. Clone target repos (once per machine)

The wizard server + dashboards expect these paths (configured in `wizard-config.json`):

```bash
git clone git@github.com:mobstac-private/beaconstac_angular_portal.git \
  ~/Desktop/Work/beaconstac_angular_portal

git clone git@github.com:mobstac-private/beaconstac_lambda_functions.git \
  ~/Desktop/Work/beaconstac_lambda_functions
```

If you put them elsewhere, edit the `path` fields in `wizard-config.json`.

---

## Running the dashboards

### Start the wizard server (serves both dashboards + Local Testing API)

```bash
make wizard-server
# OR:
python3 -m wizard_server
```

Output:

```
  Dashboard:  http://127.0.0.1:8787/
  API:        http://127.0.0.1:8787/api/
  ✓ beaconstac_angular_portal (angular)
  ✓ beaconstac_lambda_functions (lambda_monorepo)
    48 functions, 1 with tests
```

Leave this terminal open. Visit:

- **Angular dashboard:** `http://127.0.0.1:8787/vulnerability-tracker/dashboard.html`
- **Lambda dashboard:** `http://127.0.0.1:8787/vulnerability-tracker/lambda-dashboard.html`

The Lambda dashboard has two tabs: **Vulnerabilities** (default) and **Local Testing**.

### Regenerating dashboard data

```bash
./scripts/refresh-dashboard.sh            # Angular
./scripts/refresh-lambda-dashboard.sh     # Lambda
```

These fetch live Dependabot alerts via `gh`, re-classify deps, and rewrite the HTML.

---

## Local Lambda testing (dashboard-driven)

Full guide: [docs/local-lambda-testing.md](docs/local-lambda-testing.md)

### Flow

1. **Open Lambda dashboard** → click **Local Testing** tab.
2. **Start emulator** — pick flavour (Floci default, LocalStack optional), click Start. First launch pulls a Docker image (~1 min).
3. **Pick Lambda** — repo dropdown → function dropdown → function metadata appears (runtime, handler, trigger, services).
4. **Pick test event** — from `test-events/*.json`.
5. **Build & Deploy** — wizard server runs `npm install && npm run compile` (or `pip install`), zips, uploads to Floci.
6. **Invoke** — output panel shows `StatusCode`, `FunctionError` (if any), handler output, and CloudWatch log tail.
7. **Stop** when done — emulator keeps running otherwise (eats ~400 MB RAM).

### CLI equivalents

Every dashboard action has a Makefile target:

| Dashboard | Terminal |
|-----------|----------|
| Start Floci | `make emulator-up` |
| Start LocalStack | `make emulator-up-localstack` |
| Stop emulator | `make emulator-down` |
| Build + deploy Node Lambda | `make lambda-deploy DIR=/path HANDLER=index.handler LANG=node` |
| Build + deploy Python Lambda | `make lambda-deploy DIR=/path HANDLER=handler.main LANG=python` |
| Invoke | `make lambda-invoke NAME=local-my-fn EVENT=test-events/sqs-event.json` |
| List deployed | `make lambda-list` |
| Tail logs | `make lambda-logs NAME=local-my-fn` |
| Delete | `make lambda-clean NAME=local-my-fn` |

### Typical Dependabot fix iteration (30 seconds per loop)

```
1. Open lambda repo → bump the vulnerable package (npm install / edit requirements.txt)
2. Dashboard Local Testing tab → Build & Deploy
3. Click Invoke → read output
4. Fix handler if broken → Build & Deploy → Invoke
5. Repeat until green → push branch → PR
```

### Known limitation — aws-sdk v2

Handlers using `require('aws-sdk')` (v2) don't honor `AWS_ENDPOINT_URL` and will hit real AWS. They'll fail at the SES/S3/etc call with `InvalidClientTokenId`. That's fine for Dependabot validation — by that point you've already proven deps load, handler parses the event, and SDK builds the request. Full details in [docs/local-lambda-testing.md](docs/local-lambda-testing.md).

---

## Vulnerability Dashboard — Angular

### Generate / refresh

```bash
./scripts/refresh-dashboard.sh
```

This command:
1. Checks `gh` CLI auth
2. Fetches open Dependabot alerts from `mobstac-private/beaconstac_angular_portal`
3. Reads `bac-app/package.json` to classify alerts as direct vs sub-dependency
4. Assigns priority and recommended action per alert
5. Writes `vulnerability-tracker/dashboard.html`
6. Opens it in your default browser

Pass a different repo:

```bash
./scripts/refresh-dashboard.sh owner/other-repo
```

### What the dashboard shows

| Section | What it tells you |
|---------|------------------|
| Severity cards | Count of CRITICAL / HIGH / MEDIUM / LOW alerts |
| Progress bars | How many direct deps and sub-deps are resolved |
| Bar chart | Which packages have the most alerts (fix one = close N) |
| Donut chart | Runtime vs development scope split |
| Filter bar | Filter by severity, type, scope, status, or search |
| Alert table | Sortable full list — click row to expand details, click alert ID to open on GitHub |
| Package groups | All alerts grouped by package — shows leverage of batch fixes |

### Mark an alert as done

Click the **"To Do"** badge in the Status column to toggle it to **"Done"**.
Progress bars update in real time. (This is in-memory only — to persist, update
`vulnerability-tracker/hit-list.csv` manually.)

---

## Fix-Test-Release Loop

For each vulnerability in the hit list:

```
1. Pick highest-priority unresolved alert (Critical → High → Medium → Low)
2. Check if Dependabot already has a PR → use it; if not → create branch
3. Analyze changelog for breaking changes:
   ./scripts/analyze-changelog.sh npm <package> <from-version> <to-version>
4. Upgrade:
   npm install <package>@<patched-version>         # Node.js
   pip install <package>==<patched-version>        # Python
5. Test locally:
   make emulator-up
   make test-local FUNCTION=<name> EVENT=test-events/<trigger>.json
   make verify-resources
   make emulator-down
6. Push branch → create/use Dependabot PR → get Raghav review → merge
7. After deploy → check CloudWatch logs and error rates
8. Dismiss Dependabot alert on GitHub
9. Update hit-list.csv: set Status = Done, record commit hash
```

Full details: [docs/testing-workflow.md](docs/testing-workflow.md)

---

## Severity SLAs

| Severity | Fix within |
|----------|-----------|
| CRITICAL | 7 days |
| HIGH | 14 days |
| MEDIUM | 30 days |
| LOW | End of quarter |

If EPSS > 10%: treat as urgent regardless of CVSS score.

---

## Testing tiers

Full details: [docs/testing-workflow.md](docs/testing-workflow.md)

- **Tier 1 — Unit tests with mocks** (fastest, no Docker). `aws-sdk-mock` / `moto`. Good for pure logic.
- **Tier 2 — Local emulation** (dashboard flow). Floci/LocalStack + awslocal. Proves handler runs + deps load. **Primary Dependabot validation path.**
- **Tier 3 — Staging deploy**. For SES/Cognito/Step Functions/real traffic shapes. Required before merging runtime critical fixes.

---

## Project structure

```
.
├── README.md                                  # This file
├── wizard-config.json                         # Repos + server config
│
├── Makefile                                   # All commands (emulator, deploy, invoke, server)
├── docker-compose.test.yml                    # Floci emulator (free, MIT, default)
├── docker-compose.localstack.yml              # LocalStack variant (fallback)
├── env.json                                   # Legacy SAM CLI env overrides
│
├── wizard_server/                             # Python HTTP server (port 8787)
│   ├── server.py                              # Routes dashboards + API
│   ├── api.py                                 # /api/analyze /api/test-upgrade
│   ├── lambda_tester.py                       # /api/lambda/* — Local Testing backend
│   ├── repos.py                               # Lambda monorepo scanner
│   ├── pipelines.py                           # Upgrade job runner
│   ├── jobs.py                                # Async job queue
│   └── config_schema.py
│
├── scripts/
│   ├── refresh-dashboard.sh                   # Angular dashboard regen
│   ├── refresh-lambda-dashboard.sh            # Lambda dashboard regen
│   ├── generate-dashboard.py                  # Dashboard HTML generator
│   ├── analyze-changelog.sh                   # Print changelog URL for a package bump
│   ├── run-test.sh                            # Invoke Lambda + verify S3/SQS output
│   ├── setup-local-resources.sh               # Create S3/SQS/DynamoDB on emulator
│   └── teardown.sh                            # Delete emulator resources
│
├── test-events/                               # Event JSON payloads for invoke
│   ├── sqs-event.json                         # Generic SQS
│   ├── sqs-email-service.json                 # Tailored for beaconstac_email_service
│   ├── s3-event.json
│   ├── api-gateway-event.json
│   └── dynamodb-stream-event.json
│
├── vulnerability-tracker/
│   ├── dashboard.html                         # Angular — generated, do not edit
│   ├── lambda-dashboard.html                  # Lambda + Local Testing tab
│   ├── hit-list.csv                           # Alert status tracker
│   └── risk-register.csv                      # Accepted risks + Raghav approval date
│
├── docs/
│   ├── testing-workflow.md                    # Tier 1/2/3 testing strategy
│   ├── local-lambda-testing.md                # Local Testing tab — full guide
│   ├── filtering-workflow.md
│   └── dependency-classification-logic.md
│
└── .github/workflows/
    └── test-dependency-upgrade.yml            # CI: integration tests on package.json PRs
```

---

## Key commands reference

| Command | What it does |
|---------|-------------|
| `make wizard-server` | Start HTTP server (dashboards + Local Testing API) on :8787 |
| `./scripts/refresh-dashboard.sh` | Regen Angular dashboard from live alerts |
| `./scripts/refresh-lambda-dashboard.sh` | Regen Lambda dashboard from live alerts |
| `./scripts/analyze-changelog.sh npm lodash 4.17.21 4.18.0` | Print changelog URL for review |
| `make emulator-up` | Start Floci emulator at localhost:4566 |
| `make emulator-up-localstack` | Start LocalStack instead (needs `LOCALSTACK_AUTH_TOKEN` for Pro) |
| `make emulator-down` | Stop emulator + sub-containers |
| `make lambda-deploy DIR=X HANDLER=Y LANG=node\|python` | Build + zip + deploy Lambda to emulator |
| `make lambda-invoke NAME=X EVENT=Y` | Invoke deployed Lambda |
| `make lambda-list` | List deployed Lambdas |
| `make lambda-logs NAME=X` | Tail CloudWatch logs |
| `make lambda-clean NAME=X` | Delete deployed Lambda |
| `make depcheck PATH=/path` | Find unused npm packages |

---

## Troubleshooting (top 3)

**"Server unreachable" in Local Testing tab** — `make wizard-server` not running. Start it in a terminal, leave open.

**Emulator starts but Invoke times out with `LAMBDA_RUNTIME Failed to get next invocation`** — Lambda sub-container can't reach Floci's Runtime API. `docker-compose.test.yml` already has the fix (`FLOCI_SERVICES_LAMBDA_DOCKER_NETWORK`). If still broken, `docker network ls | grep default` and update env var to match your compose project name.

**Handler throws `InvalidClientTokenId` or `Missing credentials in config`** — aws-sdk v2 ignores `AWS_ENDPOINT_URL`. Expected for Dependabot validation (see Known Limitation above). For real mocking, migrate to aws-sdk v3 or wrap SDK client construction.

---

## Classification logic

How direct vs sub-dependency is determined: [docs/dependency-classification-logic.md](docs/dependency-classification-logic.md)

Summary:
- **Direct dep** — package name appears in `bac-app/package.json` under `dependencies`,
  `devDependencies`, `peerDependencies`, or `optionalDependencies`
- **Sub-dependency** — everything else in `package-lock.json` (transitive, pulled in by direct deps)

Direct deps are fixable with a single `npm install` line. Sub-deps require upgrading
the parent package (find it with `npm why <package>`) or adding an `overrides` block.

---

## Emulator coverage

| Testable locally with Floci | Needs staging deployment |
|-----------------------------|--------------------------|
| S3, SQS, DynamoDB | SES (real email delivery) |
| Lambda, SNS, KMS | Cognito |
| API Gateway, CloudWatch | Step Functions |
| IAM, CloudFormation | CloudFront |

---

## Contributing

1. Run `./scripts/refresh-dashboard.sh` before starting — work from live data
2. Follow the fix-test-release loop for each alert
3. Update `vulnerability-tracker/hit-list.csv` when an alert is resolved
4. For risk acceptance (can't fix, accepting risk), add a row to `vulnerability-tracker/risk-register.csv` with Raghav's approval date
5. Send `docs/testing-workflow.md` updates to Raghav for review before merging
