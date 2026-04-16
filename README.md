# Dependabot Vulnerability Remediation — beaconstac_angular_portal

Owner: Harshit (harshit.ky@uniqode.com) | Reviewer: Raghav | Status: Active

This repo is the operational hub for tracking and resolving Dependabot security alerts
in `mobstac-private/beaconstac_angular_portal`. It contains:

- A live vulnerability dashboard (auto-generated HTML, open locally in browser)
- Local Lambda testing infrastructure (Floci emulator + SAM CLI)
- Scripts for classifying, triaging, and resolving alerts
- Documentation for the full fix-test-release workflow

---

## Quick start

### 1. Prerequisites

Install once on your machine:

```bash
# GitHub CLI — used to fetch live Dependabot alerts
brew install gh
gh auth login   # authenticate with your GitHub account (needs repo scope)

# Python 3 — used by the dashboard generator
python3 --version   # should be 3.9+

# For local Lambda testing (optional — needed only for Tier 2 testing)
brew install aws-sam-cli
brew install awscli
pip3 install awscli-local
# Docker Desktop must be running for emulator
```

### 2. Clone this repo

```bash
git clone https://github.com/harshit-uniqode/dependabot-automation.git
cd dependabot-automation
```

### 3. Clone the Angular portal (for package.json access)

```bash
# The dashboard generator reads package.json from this path to classify deps.
# If you cloned it elsewhere, update PACKAGE_JSON_PATH in scripts/generate-dashboard.py.
git clone git@github.com:mobstac-private/beaconstac_angular_portal.git \
  ~/Desktop/Work/beaconstac_angular_portal
```

---

## Vulnerability Dashboard

### Generate / refresh

```bash
./scripts/refresh-dashboard.sh
```

This command:
1. Checks `gh` CLI auth
2. Fetches all open Dependabot alerts from `mobstac-private/beaconstac_angular_portal`
3. Reads `bac-app/package.json` to classify each alert as direct vs sub-dependency
4. Assigns priority and recommended action per alert
5. Writes `vulnerability-tracker/dashboard.html`
6. Opens the dashboard in your default browser

You can also pass a different repo:

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

## Local Lambda Testing (Tier 2)

Use when a Lambda reads from SQS, writes to S3/DynamoDB, or uses multiple services.

```bash
# Start Floci emulator + create local AWS resources
make emulator-up

# Invoke Lambda with a test event
make test-local FUNCTION=MyFunction EVENT=test-events/sqs-event.json

# Check S3/SQS state after invocation
make verify-resources

# Stop emulator
make emulator-down
```

### One-time setup per Lambda

1. Copy `docker-compose.test.yml` and `env.json` into the Lambda repo
2. Edit `scripts/setup-local-resources.sh` — add your Lambda's S3 buckets, SQS queues, DynamoDB tables
3. Edit `env.json` — add your Lambda's environment variables
4. Add test events to `test-events/` matching your trigger type

Full details and Tier 1 (unit mocks) / Tier 3 (staging) instructions: [docs/testing-workflow.md](docs/testing-workflow.md)

---

## Project structure

```
.
├── README.md                             # This file
│
├── Makefile                              # make emulator-up / test-local / verify-resources / ...
├── docker-compose.test.yml               # Floci emulator (free, MIT)
├── docker-compose.localstack.yml         # LocalStack variant (needs auth token)
├── env.json                              # SAM CLI env var overrides for local runs
│
├── scripts/
│   ├── refresh-dashboard.sh              # ONE COMMAND: fetch alerts → regenerate dashboard → open
│   ├── generate-dashboard.py             # Dashboard generator (Python 3, no extra deps)
│   ├── analyze-changelog.sh              # Print changelog URL for a package upgrade
│   ├── run-test.sh                       # Invoke Lambda + verify S3/SQS output
│   ├── setup-local-resources.sh          # Create S3/SQS/DynamoDB on local emulator
│   └── teardown.sh                       # Delete all local emulator resources
│
├── test-events/
│   ├── sqs-event.json                    # SQS trigger template
│   ├── s3-event.json                     # S3 ObjectCreated trigger template
│   ├── api-gateway-event.json            # API Gateway POST trigger template
│   └── dynamodb-stream-event.json        # DynamoDB stream MODIFY event template
│
├── vulnerability-tracker/
│   ├── dashboard.html                    # Generated — open in browser (do not edit manually)
│   ├── hit-list.csv                      # Full alert list — update Status + Resolved At Commit
│   └── risk-register.csv                 # Risk-accepted items with Raghav approval date
│
├── docs/
│   ├── testing-workflow.md               # Full Tier 1/2/3 testing guide + decision tree
│   └── dependency-classification-logic.md # How direct vs sub-dep classification works
│
└── .github/
    └── workflows/
        └── test-dependency-upgrade.yml   # CI: runs integration tests on package.json PRs
```

---

## Key scripts reference

| Command | What it does |
|---------|-------------|
| `./scripts/refresh-dashboard.sh` | Fetch live alerts → regenerate dashboard → open browser |
| `./scripts/analyze-changelog.sh npm lodash 4.17.21 4.18.0` | Print changelog URL for review |
| `./scripts/run-test.sh MyFn test-events/sqs-event.json` | Invoke Lambda + verify output |
| `make emulator-up` | Start Floci + create AWS resources on localhost:4566 |
| `make test-local FUNCTION=X EVENT=Y` | Invoke Lambda via SAM CLI against local emulator |
| `make verify-resources` | List S3/SQS/DynamoDB contents on local emulator |
| `make emulator-down` | Stop emulator, clean up containers |
| `make depcheck` | Find unused npm packages in bac-app |

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
