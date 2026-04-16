# Lambda Local Testing Workflow

Owner: Harshit | Reviewer: Raghav | Status: Draft (pending Raghav approval)

---

## Prerequisites

Install these once on your machine:

```bash
# Docker Desktop — must be running
open https://www.docker.com/products/docker-desktop/

# AWS SAM CLI
brew install aws-sam-cli
sam --version

# AWS CLI
brew install awscli
aws --version

# awslocal (wraps AWS CLI to point at localhost:4566 automatically)
pip install awscli-local
awslocal --version

# depcheck (for finding unused npm deps)
npm install -g depcheck
```

---

## Tier 1 — Unit Tests with Mocks (fastest, no Docker)

Use when: Lambda has pure business logic with no AWS calls, OR you want a fast first pass.

### Node.js (Jest + aws-sdk-mock)

```bash
npm install --save-dev jest aws-sdk-mock
```

```js
// lambda.test.js
const AWSMock = require('aws-sdk-mock');
const { handler } = require('./index');

describe('handler', () => {
  beforeEach(() => {
    AWSMock.mock('S3', 'putObject', (params, callback) => {
      callback(null, { ETag: 'test-etag' });
    });
  });

  afterEach(() => AWSMock.restore('S3'));

  it('should process event and write to S3', async () => {
    const result = await handler({ key: 'value' });
    expect(result.statusCode).toBe(200);
  });
});
```

### Python (Moto)

```bash
pip install moto[s3,sqs] pytest
```

```python
# test_lambda.py
import boto3
import pytest
from moto import mock_aws
from handler import lambda_handler

@mock_aws
def test_handler_writes_to_s3():
    # Set up fake S3 bucket
    s3 = boto3.client('s3', region_name='us-east-1')
    s3.create_bucket(Bucket='test-bucket')

    # Run Lambda
    event = {'key': 'value'}
    result = lambda_handler(event, None)

    # Verify
    assert result['statusCode'] == 200
    objects = s3.list_objects(Bucket='test-bucket')
    assert len(objects.get('Contents', [])) > 0
```

---

## Tier 2 — Full Local Emulation with Floci (recommended for most Lambdas)

Use when: Lambda reads from SQS, writes to S3/DynamoDB, or uses multiple services.

### One-time setup for a Lambda

1. Copy `docker-compose.test.yml` and `env.json` from this repo into your Lambda repo.
2. Edit `scripts/setup-local-resources.sh` — add your Lambda's S3 buckets, SQS queues, DynamoDB tables.
3. Edit `env.json` — add your Lambda's environment variables.
4. Add test events to `test-events/` for your trigger type.

### Run a test

```bash
# Start emulator + create resources
make emulator-up

# Invoke Lambda with a test event
make test-local FUNCTION=MyFunction EVENT=test-events/sqs-event.json

# Or use the verification script (also checks S3/SQS output)
./scripts/run-test.sh MyFunction test-events/sqs-event.json output-key/ test-queue

# Check what's in S3/SQS after the test
make verify-resources

# Stop emulator
make emulator-down
```

### env.json — how it works

`env.json` overrides environment variables when SAM CLI runs your Lambda locally. The key override is pointing AWS SDK calls to localhost instead of real AWS:

```json
{
  "MyFunction": {
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_DEFAULT_REGION": "us-east-1",
    "S3_BUCKET": "test-bucket",
    "SQS_QUEUE_URL": "http://host.docker.internal:4566/000000000000/test-queue",
    "DYNAMODB_TABLE": "test-table"
  }
}
```

Replace `MyFunction` with your Lambda's logical resource name from `template.yml`.

---

## Tier 3 — Staging Deployment (for SES, Cognito, Step Functions)

Use when: Lambda uses services not supported by Floci (SES, Cognito, Route53, CloudFront).

1. Create a branch for the dependency upgrade.
2. Deploy to staging AWS account via CI/CD or manually.
3. Run a manual smoke test.
4. If passes → merge PR.
5. If fails → document, discuss risk acceptance with Raghav.

---

## The Fix-Test-Release Loop

For each vulnerability in the hit list:

```
1. Pick highest-priority unresolved alert (Critical → High → Medium → Low)
2. Check if Dependabot already has a PR → if yes, use it; if no, create branch
3. Analyze changelog:
   ./scripts/analyze-changelog.sh npm <package> <from-version> <to-version>
   Paste output into Claude: "Does this upgrade break anything in my Lambda?"
4. Upgrade:
   npm install <package>@latest          # Node.js
   pip install <package>==<latest>       # Python
5. Test locally:
   make emulator-up
   make test-local FUNCTION=<name> EVENT=test-events/<trigger>.json
   make verify-resources
   make emulator-down
6. If pass → push branch, create/use Dependabot PR, get review, merge
7. After deploy → check CloudWatch logs, error rates
8. Dismiss Dependabot alert
9. Update hit-list.csv status to "Done", record commit hash
```

---

## Decision tree for each upgrade

```
No breaking changes → proceed to test
Minor breaking changes (< 2h to fix) → fix code, then test
Major breaking changes (> 2h) → flag for risk acceptance with Raghav
Service not testable locally (SES, Cognito) → deploy to staging
Staging not feasible → risk acceptance (document + get Raghav approval)
```

---

## Tool selection reference

| Tool | Use for | Free? |
|------|---------|-------|
| Floci | Full integration tests (S3/SQS/DynamoDB/Lambda/SNS) | Yes (MIT) |
| LocalStack Base | Same + 80+ services + mature | $24-39/mo |
| Moto | Python unit tests with AWS mocks | Yes |
| Jest + aws-sdk-mock | Node.js unit tests with AWS mocks | Yes |
| SAM CLI | Local Lambda invocation | Yes (AWS) |

---

## Services covered by Floci vs needing staging

| Testable with Floci | Needs Staging |
|---------------------|---------------|
| S3, SQS, DynamoDB | SES (real email) |
| Lambda, SNS, KMS | Cognito |
| API Gateway, CloudWatch | Step Functions |
| IAM, CloudFormation | CloudFront |

---

## Severity SLAs

| Severity | Fix within |
|----------|-----------|
| CRITICAL | 7 days |
| HIGH | 14 days |
| MEDIUM | 30 days |
| LOW | End of quarter |

If EPSS > 10%: treat as urgent regardless of CVSS.
