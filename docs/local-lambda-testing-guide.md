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
- Test events from `lambda-test-events/*.json`
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

1. Pick a **test event** from `lambda-test-events/` — e.g. `sqs-event.json`.
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

Files in `lambda-test-events/*.json` are the payloads your Lambda receives when invoked. Each one matches an AWS trigger type:

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

Wrap that in the `Records[].body` shape and save to `lambda-test-events/sqs-event.json`.

Add new event files whenever you work on a Lambda with a different trigger. The Local Testing tab auto-discovers them.

---

## Setting up AWS resources for a Lambda

Some Lambdas expect S3 buckets, SQS queues, DynamoDB tables to exist. Edit `scripts/setup-emulator-aws-resources.sh` to add what your function needs:

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
| Invoke | `make lambda-invoke NAME=local-send-email EVENT=lambda-test-events/sqs-event.json` |
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
dependabot-vulnerability-tracker/
├── docker/
│   ├── floci-emulator.compose.yml        # Default Floci emulator
│   └── localstack-emulator.compose.yml   # LocalStack alternative
├── Makefile                              # CLI entry points (emulator, deploy, invoke)
├── config/
│   ├── wizard-config.json                # Repo paths + server settings
│   └── lambda-env-vars.json              # Lambda env-var overrides
├── lambda-test-events/                   # Sample event payloads per trigger type
├── scripts/
│   ├── setup-emulator-aws-resources.sh   # Create S3/SQS/DynamoDB on emulator
│   └── teardown-emulator-aws-resources.sh
├── wizard_server/
│   ├── server.py                         # HTTP server (port 8787)
│   ├── api.py                            # /api/analyze, /api/test-upgrade
│   ├── lambda_tester.py                  # NEW — /api/lambda/* endpoints
│   ├── repos.py                          # Lambda monorepo scanner
│   └── ...
├── vulnerability-dashboards/
│   └── lambda-dashboard.html             # The dashboard with Vulns + Local Testing tabs
└── docs/
    ├── testing-workflow.md               # Tier 1/2/3 testing strategy
    └── local-lambda-testing.md           # THIS FILE
```

---

## How the testing actually works — surface + deep view

### Surface view (2-minute mental model)

When you click **Test in Floci**, three actors cooperate:

1. **Your Lambda code** on disk — handler + `requirements.txt` / `package.json`.
2. **Wizard server** (local Python HTTP server on :8787) — packages code, uploads to emulator, seeds resources.
3. **Floci emulator** (Docker container on :4566) — runs AWS services (Lambda, SQS, S3, DynamoDB, SES, etc.) locally. Your Lambda runs inside a **sub-container** Floci spawns.

Three-button click flow in the modal:
- **1. Build & Deploy** → wizard installs deps + zips code + calls `awslocal lambda create-function` → Floci pulls runtime image → creates function.
- **2. Seed Resources** → wizard parses `serverless.yml` + handler source → creates the S3 buckets, SQS queues, DDB tables, SES identities the Lambda expects → uploads any `tests/fixtures/*` to the first detected bucket. Idempotent — safe to click multiple times.
- **3. Invoke** → wizard calls `awslocal lambda invoke --payload <event.json>` → Floci spins Lambda sub-container, runs handler → returns statusCode + response body.

Your handler code runs **unchanged** — same Python/Node code as qa/prod. Only difference: AWS SDK calls go to `http://floci:4566` (Floci) instead of `https://sqs.us-east-1.amazonaws.com` (real AWS).

### Deep view — what happens under the hood

#### Step 1: dep packaging (the trickiest part)

For Python Lambdas with native-extension deps (`cryptography`, `pydantic-core`, `orjson`), you **cannot** just `pip install` on your Mac and zip the result. Why:

- `cryptography` ships a compiled `_rust.abi3.so` file per platform (Mac-ARM64, Linux-x86_64, Linux-ARM64).
- Lambda runs on Linux. The Mac `.so` has a different ELF header → `Runtime.ImportModuleError: invalid ELF header` at handler import.

Wizard solves this by running pip **inside** a Lambda runtime Docker container:

```
docker run --rm --platform linux/<host-arch> \
  -v $FN_DIR:/var/task \
  public.ecr.aws/lambda/python:3.9 \
  pip install -r /var/task/requirements.txt -t /var/task/.localtest_pkg
```

This is the same trick `serverless-python-requirements` does with `dockerizePip: true`. The deps written to `.localtest_pkg/` are now Linux-native.

**Arch gotcha:** Floci Lambda sub-containers run **host-native arch**, not x86_64. On Apple Silicon, `uname -m` = `arm64` → Floci Lambda container = `aarch64` → packaged deps must be Linux ARM64. Wizard detects this with `platform.machine()` and chooses `linux/arm64` vs `linux/amd64` automatically.

Wrong-arch `.so` on ARM CPU gives a **misleading error**: `cannot open shared object file: No such file or directory` — even though the file is physically present. That's Linux's dynamic loader refusing to load cross-arch ELF.

#### Step 2: the zip

Wizard zips `.localtest_pkg/` contents at zip root (not nested). Lambda runtime expects handler importable from `/var/task`, so `/var/task/handler.py` + `/var/task/cryptography/...` must sit at root. Typical zip is 20-40 MB for Python Lambdas with `cryptography` + `boto3` + `requests`.

For Node Lambdas, wizard runs `npm install`, zips `node_modules/` + source, handling TypeScript `dist/` output.

#### Step 3: upload to Floci

```
awslocal lambda create-function \
  --function-name local-<dir-name-hyphenated> \
  --runtime python3.9 \
  --handler handler.lambda_handler \
  --zip-file fileb://function.zip \
  --timeout 60 \
  --memory-size 256 \
  --environment Variables={AWS_ENDPOINT_URL=http://floci:4566,STAGE=local,...}
```

`AWS_ENDPOINT_URL=http://floci:4566` is the magic that makes `boto3.client('sqs')` inside the Lambda hit Floci's SQS instead of real AWS.

#### Step 4: invoke

```
awslocal lambda invoke \
  --function-name local-forms-response-sync-service \
  --payload fileb://./lambda-test-events/sqs-email-service.json \
  --cli-binary-format raw-in-base64-out \
  --log-type Tail \
  ./.localtest_out/<job_id>.json
```

Floci spawns a fresh Lambda sub-container from the Python runtime image, mounts the zip contents as `/var/task`, starts the Python process with `handler.lambda_handler` as entrypoint, injects the event payload via Lambda Runtime API. The handler runs, returns a dict, Floci writes it to `.localtest_out/<job_id>.json`, wizard reads it + shows it in the UI.

#### Step 5: interpretation

The UI pass/fail banner reads three fields:

| Field | Interpretation |
|-------|---------------|
| `job.status === 'done'` | Wizard subprocess succeeded (no timeout, no subprocess error) |
| `result.status_code === 200` | Lambda responded (AWS says statusCode 200 on successful invocation — 500/503 means Lambda itself crashed) |
| `result.function_error === null` | Handler did not raise unhandled exception |

All three → ✅ PASS. Any one missing → ❌ FAIL with reason.

### Common failure modes + diagnosis

| Symptom | Root cause | Where to look |
|---------|-----------|---------------|
| `invalid ELF header` | Host-arch `.so` in zip | `file .localtest_pkg/<pkg>/*.so` — check arch |
| `cannot open shared object file` (file exists) | Wrong arch `.so` on ARM/x86 CPU mismatch | `uname -m` vs `file` on `.so` |
| `content digest sha256:X not found` | Floci cached stale image digest | Restart Floci: `docker restart lambda-test-emulator` |
| Invoke hangs 120s → `timed out` | Floci killed Lambda at its own 60s timeout | `docker logs lambda-test-emulator \| grep ImportModuleError` — usually init error |
| `Runtime.ImportModuleError: No module named 'X'` | Missing from `requirements.txt` | Add it, redeploy |
| Deploy succeeds, invoke returns `500` with traceback | Handler bug | The traceback is in the invoke result — read it |
| `function not found: beaconstac_lambda_functions/...` | Dashboard sent wrong function name | See `feedback_local_testing_deploy_name_mismatch.md` memory — pass dir name verbatim |
| Deploy hangs at "Zipping..." for minutes | Output zip `function.zip` gets included in its own zip → recursive self-zip. Happened 2026-04-24 with pdf_compressor → 105GB zip | Delete `function.zip` from function dir; redeploy. Fixed in `_zip_dir` by skipping the output path from walk |
| Build timeout at 5 min | Large TS repos (pdf_compressor: 300MB node_modules, 28K files) take 3-5 min to zip | Poll timeout bumped to 15 min; granular zip progress log shows file count + size every 2s |
| `String value length (X) exceeds maximum` on deploy | Zip > Floci's 100MB JSON inline limit | Deploy auto-switches to S3 upload path for zips > 50MB. If manual: `awslocal s3 cp function.zip s3://floci-lambda-code/k.zip` then `awslocal lambda create-function --code S3Bucket=floci-lambda-code,S3Key=k.zip` |
| `Handler file 'dist/index' not found in deployment package` | TS Lambda not compiled; no npm compile/build script | Wizard now runs `npx tsc` as fallback when tsconfig.json exists |
| `NoSuchBucket` / `QueueDoesNotExist` in handler error | S3 bucket/SQS queue not seeded in Floci | Click **Seed Resources** button after Deploy. Parses serverless.yml + source to detect resources |
| `MissingRequiredParameter: Key` | Test event doesn't match handler's expected payload shape | Pick a different event from dropdown, or create custom `lambda-test-events/*.json` matching handler signature |

### Why we use Floci (not LocalStack direct, not SAM)

| Tool | Lambda | SQS/SNS | S3 | DynamoDB | SES | Cost | Notes |
|------|--------|---------|-----|----------|-----|------|-------|
| **Floci** | ✅ | ✅ | ✅ | ✅ | ✅ | Free | LocalStack fork, single Docker container |
| **LocalStack Free** | ❌ | ✅ | ✅ | ✅ | ✅ | Free | Lambda requires Pro tier |
| **LocalStack Pro** | ✅ | ✅ | ✅ | ✅ | ✅ | ~$35/mo | Full parity with AWS |
| **SAM local** | ✅ | ❌ | ❌ | ❌ | ❌ | Free | Lambda only — you wire external emulators yourself |
| **Docker Compose mock** | DIY | ❌ | ❌ | ❌ | ❌ | Free | Completely manual |

**Why Floci wins for our use case:**

1. **Lambda is the core requirement.** We're testing Lambda handlers after a dep bump. SAM local does Lambda but nothing else — every `boto3.client('sqs').send_message(...)` inside the handler would need a separate SQS mock. Floci gives us all of them in one container.

2. **It's free and zero-config.** We don't need a real AWS account, no `localstack` auth tokens, no `~/.localstack` config. One Docker container, one `FLOCI_SERVICES_LAMBDA_DOCKER_NETWORK` env var, done.

3. **It uses the real Lambda runtime image.** Floci runs your handler inside `public.ecr.aws/lambda/python:3.9` — the same image AWS uses. If the handler boots there, it'll boot in production. SAM local also uses real runtime images; pure mock solutions don't.

4. **The `awslocal` CLI is identical to `aws`.** All commands (`lambda create-function`, `sqs send-message`, `s3 cp`) work unchanged. The wrapper just routes to `http://localhost:4566`. No extra SDK config needed in test scripts.

5. **SES is emulated (no real emails sent).** The handler can call `ses.send_email()` and it won't actually send. Floci logs the call. For email Lambda testing this is critical — SES in real AWS requires domain verification, sandbox mode, etc.

**When to switch to LocalStack Pro:**
If you hit a Floci compatibility bug, or need a service Floci doesn't fully support (Kinesis streams, Step Functions, EventBridge rules, Cognito). The Local Testing tab already has a flavour dropdown for this.

Floci has rough edges (the arch gotcha, the image-digest caching) but the alternative of deploying to qa every time is 10x slower.

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
- **Reactive S3 Sync**: deploy Lambda via `Code.S3Bucket='local-code'` + `S3Key`. On code change, re-upload to S3 key → Floci automatically drains warm containers. Faster iteration than redeploying each time. Tracked in `feedback_lambda_native_deps_arch.md` memory.
- **Ephemeral mode toggle** in UI: checkbox "Cold-start every invoke" → sets `FLOCI_SERVICES_LAMBDA_EPHEMERAL=true`. Use when debugging intermittent warm-container state issues.
- **Remote debugger attach** (bigger lift): bake `debugpy` / `--inspect-brk` into Lambda runtime image, expose port, add VS Code launch.json. See reference_localstack_youtube_learnings.md memory for the pattern.

---

## Floci configuration reference

Our `docker/floci-emulator.compose.yml` sets these env vars (full reference in `reference_floci_complete.md` memory):

| Var | Value | Why |
|-----|-------|-----|
| `FLOCI_SERVICES_LAMBDA_DOCKER_NETWORK` | `dependabotvulnerabilitytracker_default` | Lambda sub-containers join this network to reach Floci's Runtime API. Derived from Docker Compose project name (dir name lowercased). |
| `FLOCI_HOSTNAME` | `floci` | Lambda containers resolve `http://floci:4566` for AWS SDK calls — matches the service alias on the network. Without this, `AWS_ENDPOINT_URL=http://floci:4566` inside Lambda wouldn't resolve. |
| `FLOCI_STORAGE_MODE` | `hybrid` | In-memory for hot path, periodic flush to `/app/data` so Lambda code / S3 / DDB survive Floci restarts. Default (in shipped yml) is `memory` — we override. |
| `FLOCI_SERVICES_LAMBDA_CONTAINER_IDLE_TIMEOUT_SECONDS` | `300` | Warm pool reaper — keeps containers for 5 min of inactivity. Matches AWS production default. |
| `FLOCI_SERVICES_LAMBDA_EPHEMERAL` | `false` | Warm pool enabled. Set `true` in CI or for guaranteed fresh code per invoke. |

**Hard-coded Floci timeouts we can't configure:**
- 5-min image pull timeout on first Lambda runtime image pull
- +2s grace on top of function timeout
- 30s Runtime API long-poll

---

## References

### Direct documentation
- Floci repo (canonical): https://github.com/floci-io/floci
- Floci docs site: https://floci.io/floci/
- Floci Lambda service: https://floci.io/floci/services/lambda/
- Floci application.yml reference: https://floci.io/floci/configuration/application-yml/
- LocalStack (for comparison): https://docs.localstack.cloud/user-guide/aws/lambda/
- Serverless Python Requirements (pattern we mirror for Docker pip): https://www.serverless.com/plugins/serverless-python-requirements

### Educational videos (referenced for architecture decisions)
The original Floci integration was informed by these LocalStack videos — patterns like containerized dep builds, hot reloading, SNS→Lambda testing, and debugger attach were extracted and adapted for Floci. Verbatim transcripts not available (YouTube blocks page-body fetch), but video titles + our extracted patterns are documented in the `reference_localstack_youtube_learnings.md` memory.

- "How to hot reload your Lambda functions locally with LocalStack" — https://youtu.be/BKjPOhQE2hg
  - We borrowed: the idea of matching Lambda runtime for builds, Docker socket mounting pattern.
- "TestMu AI-LocalStack Integration" — https://youtu.be/DFS3CnB-Z0k
  - We borrowed: `AWS_ENDPOINT_URL=http://floci:4566` env pattern.
- "How to test AWS Lambda, SNS using localstack" — https://youtu.be/uLt5Ah1bUUk
  - Future borrow: SNS→Lambda integration test patterns.
- "Smarter Serverless Dev with LocalStack VS Code Toolkit & Lambda Debugging" — https://youtu.be/51EAwBDdgio
  - Future borrow: VS Code extension + debugger attach patterns (not yet implemented).

### Internal memory references
- `reference_floci_complete.md` — full Floci env var / internals / gotchas reference
- `reference_localstack_youtube_learnings.md` — patterns from videos, what we can port
- `feedback_lambda_native_deps_arch.md` — why we build deps in Docker with matching arch
- `feedback_local_testing_deploy_name_mismatch.md` — dir-name-verbatim contract
- `feedback_loud_passfail_banners.md` — UI pass/fail banner pattern
- `reference_floci_diagnostics.md` — quick debugging commands
