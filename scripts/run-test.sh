#!/usr/bin/env bash
# run-test.sh
# Invokes a Lambda locally and verifies the expected output.
# Usage: ./scripts/run-test.sh <FunctionName> <event-file> [expected-s3-key] [expected-sqs-queue]
#
# Examples:
#   ./scripts/run-test.sh ProcessSQSHandler test-events/sqs-event.json
#   ./scripts/run-test.sh UploadProcessor test-events/s3-event.json output-prefix/ test-queue

set -euo pipefail

FUNCTION="${1:-}"
EVENT="${2:-}"
EXPECTED_S3_KEY="${3:-}"
EXPECTED_SQS_QUEUE="${4:-}"

ENDPOINT="http://localhost:4566"
REGION="us-east-1"
AWS="aws --endpoint-url=$ENDPOINT --region $REGION --no-cli-pager"

if [[ -z "$FUNCTION" || -z "$EVENT" ]]; then
  echo "Usage: $0 <FunctionName> <event-file> [expected-s3-key] [expected-sqs-queue]"
  exit 1
fi

if [[ ! -f "$EVENT" ]]; then
  echo "ERROR: Event file '$EVENT' not found"
  exit 1
fi

echo "=== Running Lambda: $FUNCTION ==="
echo "Event: $EVENT"
echo ""

# Build Lambda
echo "Building..."
sam build 2>&1 | tail -3

# Invoke and capture output
echo ""
echo "Invoking..."
OUTPUT=$(sam local invoke "$FUNCTION" \
  --event "$EVENT" \
  --env-vars env.json \
  --docker-network host \
  2>&1)

echo "$OUTPUT"

# Check for errors
if echo "$OUTPUT" | grep -qi "errorMessage\|Traceback\|Error:"; then
  echo ""
  echo "FAIL — Lambda returned an error. See output above."
  exit 1
fi

# Verify S3 output if expected key was given
if [[ -n "$EXPECTED_S3_KEY" ]]; then
  echo ""
  echo "Checking S3 for key: $EXPECTED_S3_KEY"
  if $AWS s3 ls "s3://test-bucket/$EXPECTED_S3_KEY" 2>/dev/null; then
    echo "PASS — S3 object found."
  else
    echo "FAIL — Expected S3 key '$EXPECTED_S3_KEY' not found in test-bucket."
    exit 1
  fi
fi

# Verify SQS output if expected queue was given
if [[ -n "$EXPECTED_SQS_QUEUE" ]]; then
  echo ""
  echo "Checking SQS queue: $EXPECTED_SQS_QUEUE"
  QUEUE_URL="$ENDPOINT/000000000000/$EXPECTED_SQS_QUEUE"
  MSG=$($AWS sqs receive-message --queue-url "$QUEUE_URL" --max-number-of-messages 1 2>/dev/null)
  if echo "$MSG" | grep -q "Body"; then
    echo "PASS — Message found in SQS queue."
    echo "$MSG" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Messages'][0]['Body'])" 2>/dev/null || true
  else
    echo "FAIL — No message found in SQS queue '$EXPECTED_SQS_QUEUE'."
    exit 1
  fi
fi

echo ""
echo "=== Test passed for $FUNCTION ==="
