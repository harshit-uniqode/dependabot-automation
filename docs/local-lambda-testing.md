# Local Lambda Testing — Dashboard Guide

Owner: Harshit (harshit.ky@uniqode.com) · Reviewer: Raghav · Status: Active

This document explains the **Local Testing** tab in the Lambda vulnerability dashboard — what it does, why it exists, and how to use it day-to-day when fixing Dependabot alerts.

---

## Why this exists

Fixing a Dependabot alert means bumping a dependency in a Lambda function. Before pushing the PR, you want to answer one question: **does the function still run with the new dependency?**

Until now, that question was answered by:
- Deploying to the `qa` stage and watching CloudWatch, or
- Mocking every AWS call and running Jest/pytest locally.

Both are slow. Deploy-to-qa takes 5-10 min per iteration, and the full UI test for email/QR flows means clicking through the dashboard UI. Mocking misses integration bugs (wrong IAM, wrong queue URL, serialization issues).

The local testing workflow gives a third option: **run the real Lambda handler against a local AWS emulator** in about 30 seconds per iteration, entirely from the dashboard — no terminal, no qa deploy.

---

## What gets tested

| Scenario | Local test covers it? |
|----------|----------------------|
| Handler runs at all with upgraded deps | **Yes** |
| Handler parses incoming event correctly | **Yes** |
| Handler writes to S3 / SQS / DynamoDB | **Yes** (against local emulator) |
| Handler calls SES to send email | **Yes** (emulator logs the call; no real email) |
| Handler pulls secret from Secrets Manager | **Yes** (if you seed a dummy secret first) |
| Handler behaves correctly under production traffic | No — still need qa deploy |
| Real-world SES deliverability, bounce handling | No — SES is mocked |

For Dependabot fixes (~95% of the time) this is enough. For anything touching auth flows, production data shapes, or real external APIs, you still do a qa deploy.

---

## Architecture — how the dashboard runs code

```
┌──────────────────────────────────────────────────────────────┐
│  lambda-dashboard.html (browser, Local Testing tab)          │
│  ┌───────────────────────────────────────────┐               │
│  │  "Build & Deploy" → fetch POST /api/...   │               │
│  └──────────────┬────────────────────────────┘               │
└─────────────────┼────────────────────────────────────────────┘
                  │ HTTP (localhost:8787)
                  ▼
┌──────────────────────────────────────────────────────────────┐
│  wizard_server (Python, stdlib HTTP server)                  │
│  • Scans Lambda repos, parses serverless.yml                 │
│  • Runs `npm install`, `pip install`, zip, awslocal          │
│  • Streams logs to job queue for the browser to poll         │
└──────────────┬───────────────────────────────────────────────┘
               │ subprocess
               ▼
┌──────────────────────────────────────────────────────────────┐
│  Floci emulator (Docker container, localhost:4566)           │
│  • Runs your Lambda in an isolated container                 │
│  • Mocks S3/SQS/DynamoDB/SNS/SES/Lambda                      │
│  • Free, MIT-licensed, zero auth                             │
└──────────────────────────────────────────────────────────────┘
```

**Nothing privileged in the browser.** The dashboard calls a whitelist of 7 endpoints on `wizard_server`. The server is the only thing that shells out. Close the browser — the server stays running. Stop the server — everything stops.

---

## Why Floci (and not LocalStack)

Both are AWS emulators. Both can run Lambda + SQS + S3 + DynamoDB locally. Here's the picking logic:

| Criterion | **Floci** (default) | LocalStack |
|-----------|---------------------|-----------|
| Cost | Free, MIT | Community free; Pro $24-39/mo |
| Auth token | None | Required for Pro features |
| Setup time | `docker compose up` | Sign up, get token, export env var |
| Covers S3/SQS/Lambda/DynamoDB/SNS/SES | Yes | Yes |
| Service coverage | ~10 AWS services | 80+ AWS services |
| Good enough for Dependabot validation | Yes | Yes (overkill) |
| Mature / well-documented | Less | More |

**Decision:** Default to Floci. For a Dependabot fix, we only care "does the handler run?" and "do its AWS calls succeed?" — both give the same answer. Zero friction wins.

**When to switch to LocalStack:** if you hit a Floci bug, or need a service Floci doesn't support (Kinesis streams, Step Functions, EventBridge rules). The flavour dropdown in the Local Testing tab lets you switch per session. Pro auth token goes in the `LOCALSTACK_AUTH_TOKEN` environment variable.

---

## Prerequisites (one-time setup)

### 1. Install the CLI tools

```bash
brew install awscli
pip3 install awscli-local
```

- `awscli` — standard AWS CLI
- `awscli-local` — provides the `awslocal` wrapper that auto-points at `localhost:4566`

### 2. Docker Desktop must be running

The emulator runs in Docker. If Docker isn't running you'll see a "Server unreachable" pill in the status bar.

### 3. Start the wizard server

From the repo root:

```bash
python3 -m wizard_server
```

You'll see:

```
  Dashboard:  http://127.0.0.1:8787/
  API:        http://127.0.0.1:8787/api/
```

Leave this terminal open. Go to **[http://127.0.0.1:8787/](http://127.0.0.1:8787/)** in your browser.

> **Why not open the HTML file directly?** The browser blocks `file://` pages from making API calls to `localhost` by default. Serving through the wizard server avoids this and means the dashboard works offline on your laptop without any extra config. The JS also has a fallback so if you do open the HTML directly, it still talks to `http://127.0.0.1:8787`.

---

## Using the Local Testing tab — step by step

### Step 1 — Open the tab

Click **"Local Testing"** at the top of the dashboard.

On first load the UI fetches:
- List of Lambda repos from `wizard-config.json`
- Functions per repo (parsed from each `serverless.yml`)
- Test events from `test-events/*.json`
- Current emulator status

### Step 2 — Start the emulator

1. Choose flavour: **Floci** (default) or LocalStack.
2. Click **Start emulator**.
3. First run pulls the Docker image (~60 MB). Log pane shows progress.
4. Wait for "✓ Emulator ready". The status pill turns green.

The emulator listens on `http://localhost:4566`. It stays up until you click Stop (or reboot the laptop).

### Step 3 — Pick a Lambda

1. **Repository** — e.g. `beaconstac_lambda_functions`.
2. **Function** — e.g. `beaconstac_email_service`. The dropdown shows `[nodejs]` or `[python]` per function.

Once selected, the metadata card shows: runtime, handler, trigger type, AWS services detected from `serverless.yml`.

### Step 4 — Build & Deploy

1. Click **Build & Deploy**.
2. The server runs:
   - **Node:** `npm install` → `npm run compile` (if the script exists) → zip `dist/` + `node_modules`
   - **Python:** `pip install -r requirements.txt -t .localtest_pkg` → copy handler files in → zip
3. Upload the zip to the emulator via `awslocal lambda create-function` (or `update-function-code` on re-deploy).
4. Wait for function to reach `Active` state.

Typical time: 15-60 seconds depending on dep size.

### Step 5 — Invoke

1. Pick a **test event** from `test-events/` — e.g. `sqs-event.json`.
2. Click **Invoke**.
3. Output panel shows:
   - `StatusCode` (200 = invocation succeeded)
   - `FunctionError` (present only if handler threw)
   - `output` — what the handler returned
   - `log tail` — CloudWatch log output, base64-decoded

Green border = success. Red border = handler threw.

### Step 6 — Iterate on the Dependabot fix

Every time you change the code or bump a dep:

1. Click **Build & Deploy** again (rebuilds and re-uploads).
2. Click **Invoke**.

No terminal. No qa push. Each iteration ~30 seconds.

### Step 7 — Stop the emulator

When you're done:

1. Click **Stop** in the top status bar.
2. Confirm. Floci container shuts down, Lambda sub-containers exit.

Docker consumes ~300-500 MB RAM while running. Always stop it when you're not using it.

---

## Test events — how they work

Files in `test-events/*.json` are the payloads your Lambda receives when invoked. Each one matches an AWS trigger type:

| File | Trigger | Edit when |
|------|---------|-----------|
| `sqs-event.json` | SQS batch (1+ messages) | Change the `body` field to match your queue's message shape |
| `s3-event.json` | S3 ObjectCreated | Update bucket name + object key |
| `api-gateway-event.json` | API Gateway HTTP | Update path, method, body |
| `dynamodb-stream-event.json` | DynamoDB stream MODIFY | Update table ARN and record shape |

**Example — `beaconstac_email_service`:** trigger is SQS. The handler parses `event.Records[*].body` as JSON. Sample event body from the function's own `test.json`:

```json
{
  "email_type": "template",
  "emails": ["user@example.com"],
  "template": "<h1>Hello</h1>",
  "subject": "Test",
  "email_source": "noreply@example.com",
  "sender_name": "Beaconstac"
}
```

Wrap that in the `Records[].body` shape and save to `test-events/sqs-event.json`.

Add new event files whenever you work on a Lambda with a different trigger. The Local Testing tab auto-discovers them.

---

## Setting up AWS resources for a Lambda

Some Lambdas expect S3 buckets, SQS queues, DynamoDB tables to exist. Edit `scripts/setup-local-resources.sh` to add what your function needs:

```bash
S3_BUCKETS=( "my-test-bucket" )
SQS_QUEUES=( "send-email-queue" )
```

Then either:
- Run `make setup-resources` manually, or
- Start the emulator — `make emulator-up` runs it automatically

For secrets used by the Lambda (e.g. Datadog API key for `beaconstac_email_service`):

```bash
awslocal secretsmanager create-secret \
  --name DatadogAPIKey-N3TCCx \
  --secret-string '{"apiKey":"dummy"}'
```

---

## Terminal equivalents (if the UI is broken or you prefer CLI)

Every dashboard button has a Makefile equivalent:

| Dashboard action | Terminal |
|------------------|----------|
| Start emulator (Floci) | `make emulator-up` |
| Start emulator (LocalStack) | `make emulator-up-localstack` |
| Stop emulator | `make emulator-down` |
| Build + deploy Node Lambda | `make lambda-deploy DIR=/path/to/fn HANDLER=index.sendEmail LANG=node` |
| Build + deploy Python Lambda | `make lambda-deploy DIR=/path/to/fn HANDLER=handler.lambda_handler LANG=python` |
| Invoke | `make lambda-invoke NAME=local-send-email EVENT=test-events/sqs-event.json` |
| List deployed | `make lambda-list` |
| Tail logs | `make lambda-logs NAME=local-send-email` |
| Delete a function | `make lambda-clean NAME=local-send-email` |

Function names are derived from the directory name: `beaconstac_email_service` → `local-beaconstac-email-service`.

---

## Known limitation — aws-sdk v2 + endpoint override

**What you'll see:** handler invokes successfully, then throws `InvalidClientTokenId` or `NetworkingError` when it tries to call SES/S3/etc.

**Why:** `aws-sdk` v2 (the `require('aws-sdk')` package) does NOT honor the `AWS_ENDPOINT_URL` environment variable. It needs the endpoint passed explicitly: `new AWS.SES({ endpoint: 'http://floci:4566' })`. `aws-sdk` v3 (`@aws-sdk/client-*` packages) does honor the env var.

**Why this is fine for Dependabot validation:** by the time you see this error, the handler has already:
1. Loaded all dependencies (proves the bump didn't break require chain).
2. Parsed the incoming event (proves event shape contract).
3. Built the AWS SDK request object (proves SDK surface compatibility).

That's ~95% of what can break from a dep upgrade. The last 5% (actual AWS behavior) always needs staging anyway.

**If you really need full AWS mocking:** migrate the Lambda to aws-sdk v3, or wrap the SDK client construction in a helper that reads `AWS_ENDPOINT_URL` and injects it. This is a code-level change, out of scope for Dependabot triage.

---

## Troubleshooting

### "Server unreachable" in the status pill

The wizard server isn't running. Open a terminal:

```bash
cd "/Users/harshitky/Desktop/Local Testing For lambda"
python3 -m wizard_server
```

Keep the terminal open, refresh the tab.

### Emulator won't start

1. Is Docker Desktop running? `docker ps` should work.
2. Is port 4566 free? `lsof -i :4566` — if something else is on it, stop that first.
3. Check the log pane in the dashboard — it streams `make emulator-up` output live.

### "awslocal: command not found"

```bash
pip3 install awscli-local
```

The server falls back to `aws --endpoint-url=http://localhost:4566` if `awslocal` isn't installed, but that requires `aws` CLI itself — install with `brew install awscli`.

### Deploy fails with "npm install failed" / "pip install failed"

Open a terminal and run the build manually in the function dir. It'll surface the real error (missing native deps, incompatible Node version, etc.).

### Invoke returns `FunctionError: "Unhandled"`

The handler threw. Check the `log tail` in the output panel — the full stack trace is there. This is what you want to see when testing — it means the local setup worked, now you know the bump broke something.

### Docker eating RAM after testing

Always click **Stop** when you're done. If the dashboard is closed, run `make emulator-down` from the repo root. To nuke everything:

```bash
docker ps -a | grep -E "lambda|floci|localstack" | awk '{print $1}' | xargs docker rm -f
```

---

## What's actually in the repo

```
Local Testing For lambda/
├── docker-compose.test.yml               # Floci emulator definition
├── docker-compose.localstack.yml         # LocalStack alternative
├── Makefile                              # All CLI commands (emulator, deploy, invoke)
├── env.json                              # SAM env overrides (legacy — SAM path)
├── test-events/                          # JSON payloads for different triggers
│   ├── sqs-event.json
│   ├── s3-event.json
│   ├── api-gateway-event.json
│   └── dynamodb-stream-event.json
├── scripts/
│   ├── setup-local-resources.sh          # Creates S3/SQS/DynamoDB on the emulator
│   └── teardown.sh                       # Removes them
├── wizard_server/
│   ├── server.py                         # HTTP server (port 8787)
│   ├── api.py                            # /api/analyze, /api/test-upgrade
│   ├── lambda_tester.py                  # NEW — /api/lambda/* endpoints
│   ├── repos.py                          # Lambda monorepo scanner
│   └── ...
├── vulnerability-tracker/
│   └── lambda-dashboard.html             # The dashboard with Vulns + Local Testing tabs
└── docs/
    ├── testing-workflow.md               # Tier 1/2/3 testing strategy
    └── local-lambda-testing.md           # THIS FILE
```

---

## Where to file bugs

If something's wrong with the Local Testing tab:

1. Check the `wizard_server` terminal output — errors print there.
2. Check the browser dev console — JS errors show there.
3. Drop a note to Harshit (harshit.ky@uniqode.com) or open a GitHub issue on this repo.

---

## Future improvements (not built yet)

- Per-Lambda `local-test.json` file in each function dir specifying custom build commands, required secrets, and default event template. Right now the server uses defaults derived from `serverless.yml`.
- Auto-seed Secrets Manager with dummy values so SES/Datadog functions work without manual `awslocal secretsmanager create-secret`.
- One-click "test this fix" button on each Dependabot alert row in the Vulnerabilities tab — switches to Local Testing tab with the right function pre-selected.
- LocalStack Pro hot-reload integration for Node Lambdas (`mountCode: true`) — removes the rebuild step entirely.
