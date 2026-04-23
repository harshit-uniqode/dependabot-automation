# Dependabot Vulnerability Tracker

A one-stop local tool to **triage Dependabot alerts, test upgrades, and invoke
AWS Lambdas locally** — all from a single browser dashboard.

It replaces three chores that used to live in three different places:

| Old way                                          | New way (this tool)                         |
| ------------------------------------------------ | ------------------------------------------- |
| Scroll GitHub's Dependabot UI                    | Scored, filterable dashboard at `:8787`     |
| Read 10 changelogs by hand before merging        | AI "safe-to-merge" verdict per alert        |
| Push to a branch and wait for CI to test a fix   | Deploy + invoke the Lambda locally, in 30 s |

Two dashboards — **Angular Portal** and **Lambda Functions**. The Lambda
dashboard has an extra **Local Testing** tab that deploys any of 48 Lambda
functions to a local AWS emulator (Floci) and invokes them with test events.

---

## Getting set up

### 1. Install prerequisites

Everything is mac-friendly (`brew` below); the same tools exist on Linux and
Windows — only the install command differs.

```bash
brew install docker gh jq awscli pipx
pipx install awscli-local && pipx ensurepath
```

| Tool        | Why you need it                                  |
| ----------- | ------------------------------------------------ |
| Docker      | Runs the Floci/LocalStack AWS emulator           |
| `gh`        | Auths against GitHub to pull Dependabot alerts   |
| `jq`        | Used by `make` targets to parse AWS CLI output   |
| `awscli`    | Talks to the local emulator at `:4566`           |
| `awslocal`  | Wrapper around `awscli` with `--endpoint-url` baked in |
| `pipx`      | Needed to install `awslocal` (PEP 668 blocks `pip3`) |

Authenticate `gh` once:

```bash
gh auth login           # pick GitHub.com, HTTPS, login with browser
```

### 2. Clone and configure

```bash
git clone <this-repo-url> dependabot-vulnerability-tracker
cd dependabot-vulnerability-tracker
```

Edit `config/wizard-config.json` to point at the repos you actually have
checked out on your machine. The defaults assume Uniqode's layout:

```json
{
  "repos": [
    { "name": "beaconstac_angular_portal",
      "path": "/path/to/beaconstac_angular_portal",
      "github": "<org>/beaconstac_angular_portal",
      "type": "angular" },
    { "name": "beaconstac_lambda_functions",
      "path": "/path/to/beaconstac_lambda_functions",
      "github": "<org>/beaconstac_lambda_functions",
      "type": "lambda_monorepo" }
  ]
}
```

### 3. (Optional) Set environment variables

```bash
cp .env.example .env
# edit .env if you want LLM-powered deep analysis or LocalStack Pro
```

### 4. Start the wizard server

```bash
make wizard-server
```

You will see:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Upgrade Wizard Server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Dashboard:  http://127.0.0.1:8787/
  API:        http://127.0.0.1:8787/api/
```

---

## Running the dashboards

1. `make wizard-server` — start the server (port 8787).
2. Open one of the two dashboards in your browser:

   - **Angular Portal:** http://127.0.0.1:8787/vulnerability-dashboards/angular-dashboard.html
   - **Lambda Functions:** http://127.0.0.1:8787/vulnerability-dashboards/lambda-dashboard.html

3. If you see a "Dashboard not found" error, generate the HTML first
   (details in [Regenerating dashboards](#regenerating-dashboards) below).

---

## Local Lambda testing (dashboard-driven)

A typical Dependabot fix loop takes ~30 seconds per iteration:

1. Open the **Lambda Functions** dashboard → switch to the **Local Testing** tab.
2. Click **Start Emulator** — brings up Floci on `localhost:4566`.
3. Pick a repo (auto-populated from `wizard-config.json`), a function, and a
   test event from `lambda-test-events/`.
4. Click **Build & Deploy** — zips the function, `npm install` / `pip install`,
   and creates the Lambda on the emulator.
5. Click **Invoke** — runs the function with the selected event and prints the
   `StatusCode`, log tail, and output inline.
6. When done, **Stop Emulator** — frees CPU/memory (no autostop, by design).

See [`docs/local-lambda-testing-guide.md`](docs/local-lambda-testing-guide.md)
for internals (how Floci spawns the Lambda sub-container, how `handler` paths
are resolved, how to add a new test event type).

---

## Regenerating dashboards

The HTML dashboards are generated from live GitHub Dependabot alerts:

```bash
make refresh-lambda       # Lambda dashboard — live alerts + auto-open browser
make refresh-angular      # Angular dashboard
```

Under the hood these call `scripts/generate_dashboard.py`, which:

1. Shells out to `gh api` to pull open alerts.
2. Cross-references `package.json` / `requirements.txt` in the target repo to
   classify each alert as **direct** or **sub-dependency**.
3. Computes a **merge-safety score** (0-10) for each alert —
   see [`docs/dependency-classification-logic.md`](docs/dependency-classification-logic.md).
4. (Optional) Attaches cached deep LLM analysis from
   `vulnerability-dashboards/.deep-analysis-cache.json` if present.
5. Writes a self-contained HTML file.

To generate a richer AI analysis for high-severity alerts (uses ANTHROPIC_API_KEY):

```bash
python3 scripts/run_deep_analysis.py --repo <org>/beaconstac_lambda_functions
make refresh-lambda       # Re-open — row expansion shows "AI Safety Analysis"
```

---

## CLI equivalents (every dashboard action is also a `make` target)

| Dashboard action            | CLI equivalent                                                         |
| --------------------------- | ---------------------------------------------------------------------- |
| Start emulator              | `make emulator-up`                                                     |
| Stop emulator               | `make emulator-down`                                                   |
| Verify emulator resources   | `make verify-resources`                                                |
| Check emulator health       | `make emulator-health`                                                 |
| Deploy a Lambda             | `make lambda-deploy DIR=<path> HANDLER=<file.fn> LANG=<node\|python>`  |
| Invoke a deployed Lambda    | `make lambda-invoke NAME=<fn-name> EVENT=<path/to/event.json>`         |
| List deployed Lambdas       | `make lambda-list`                                                     |
| Tail Lambda logs            | `make lambda-logs NAME=<fn-name>`                                      |
| Delete deployed Lambda      | `make lambda-clean NAME=<fn-name>`                                     |
| Regenerate Lambda dashboard | `make refresh-lambda`                                                  |
| Regenerate Angular dashboard| `make refresh-angular`                                                 |

All targets live in the root [`Makefile`](Makefile).

---

## Project structure

See [`INFO.md`](INFO.md) for a file-by-file walkthrough.

```
dependabot-vulnerability-tracker/
├── README.md                     # You are here
├── INFO.md                       # File-by-file directory
├── LICENSE                       # MIT
├── Makefile                      # All CLI entry points
├── .env.example                  # Optional env vars template
│
├── config/                       # User-editable configuration
│   ├── wizard-config.json        # Repos to track + server settings
│   └── lambda-env-vars.json      # Env overrides when invoking Lambdas
│
├── docker/                       # Docker Compose files for emulators
│   ├── floci-emulator.compose.yml
│   └── localstack-emulator.compose.yml
│
├── wizard_server/                # Python HTTP server (port 8787)
│   ├── __main__.py               # python3 -m wizard_server entry
│   ├── server.py                 # HTTP routing + dashboard serving
│   ├── api.py                    # /api/analyze + /api/test-upgrade
│   ├── lambda_tester.py          # Local Testing tab backend
│   ├── jobs.py                   # Async job queue + state
│   ├── pipelines.py              # Upgrade + test pipelines
│   ├── repos.py                  # Repo type detection + scanning
│   └── config_schema.py          # Config loading + validation
│
├── scripts/                      # Command-line helpers
│   ├── generate_dashboard.py     # Build the HTML dashboards
│   ├── refresh-lambda-dashboard.sh
│   ├── refresh-angular-dashboard.sh
│   ├── analyze_alert.py          # Rule-based + LLM merge-safety scoring
│   ├── run_deep_analysis.py      # Cached deep LLM analysis
│   ├── analyze-package-changelog.sh    # Fetch npm/pip changelog URLs
│   ├── git_blame_lookup.py       # "Who last touched this function?"
│   ├── setup-emulator-aws-resources.sh # Create S3/SQS/DDB on emulator
│   ├── teardown-emulator-aws-resources.sh
│   └── invoke-and-verify-lambda.sh     # Invoke + assert S3/SQS side effects
│
├── lambda-test-events/           # Sample Lambda event payloads
│   ├── api-gateway-event.json
│   ├── dynamodb-stream-event.json
│   ├── s3-event.json
│   ├── sqs-event.json
│   └── sqs-email-service.json
│
├── vulnerability-dashboards/     # Generated HTML + audit CSVs
│   ├── angular-dashboard.html
│   ├── lambda-dashboard.html
│   ├── hit-list.csv              # Snapshot of open alerts (git-tracked)
│   └── risk-register.csv         # Risk-accepted items w/ approvers
│
└── docs/                         # Deep-dives
    ├── local-lambda-testing-guide.md
    ├── dashboard-filtering-workflow.md
    └── dependency-classification-logic.md
```

---

## Troubleshooting

**`make wizard-server` starts but one of the dashboards is missing**
Run `make refresh-angular` or `make refresh-lambda` to generate it.

**`gh auth login` — "no supported authentication methods"**
Make sure your browser is logged into the GitHub account that has access to
the private repos in `wizard-config.json`.

**"Emulator not running" on the Local Testing tab**
Click **Start Emulator**. Docker Desktop must be running.

**Deploy succeeds, Invoke hangs until 60s timeout**
Lambda sub-container can't reach Floci's Runtime API. The fix is in
`docker/floci-emulator.compose.yml` via `FLOCI_SERVICES_LAMBDA_DOCKER_NETWORK`.
If the network name doesn't match (you renamed the repo directory), run:

```bash
docker network ls | grep default
```

…and update that env var to match.

**`pipx install awscli-local` — "externally-managed-environment"**
That's PEP 668 blocking `pip3`. `pipx` is the right tool for CLI Python
packages on modern macOS/Ubuntu.

**Lambda invoke returns `InvalidClientTokenId`**
The handler is using `aws-sdk v2` which ignores `AWS_ENDPOINT_URL`. The SDK
is trying to hit real AWS and failing auth. For Dependabot validation this
is still useful — the handler loaded, parsed the event, and reached the
external-call boundary. Covered in detail in
[`docs/local-lambda-testing-guide.md`](docs/local-lambda-testing-guide.md).

---

## Known limitations

1. `aws-sdk` v2 does not honour `AWS_ENDPOINT_URL`; handlers using it will
   reach real AWS and fail with `InvalidClientTokenId`. Use v3 for full
   emulator coverage.
2. First `Invoke` is slow (~30 s) because Floci pulls
   `public.ecr.aws/lambda/nodejs:18`. Subsequent invocations use a warm pool.
3. `wizard-config.json` paths are absolute — each developer edits their own
   copy. Not committed-and-shared.

---

## License

MIT — see [`LICENSE`](LICENSE).
