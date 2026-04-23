#!/usr/bin/env bash
# invoke-and-verify-lambda.sh
# Invokes a Lambda deployed to the local emulator and optionally verifies
# that the function wrote to S3 or produced an SQS message.
#
# Prerequisite: the function is already deployed (via dashboard or
# `make lambda-deploy`). This script only INVOKES and VERIFIES.
#
# Usage:
#   ./scripts/invoke-and-verify-lambda.sh <fn-name> <event-file> [expected-s3-key] [expected-sqs-queue]
#
# Examples:
#   ./scripts/invoke-and-verify-lambda.sh local-beaconstac-email-service lambda-test-events/sqs-event.json
#   ./scripts/invoke-and-verify-lambda.sh local-upload-processor lambda-test-events/s3-event.json out/ test-queue

set -euo pipefail

FUNCTION="${1:-}"
EVENT="${2:-}"
EXPECTED_S3_KEY="${3:-}"
EXPECTED_SQS_QUEUE="${4:-}"

if [[ -z "$FUNCTION" || -z "$EVENT" ]]; then
  echo "Usage: $0 <fn-name> <event-file> [expected-s3-key] [expected-sqs-queue]"
  exit 1
fi

if [[ ! -f "$EVENT" ]]; then
  echo "ERROR: Event file '$EVENT' not found"
  exit 1
fi

if command -v awslocal >/dev/null 2>&1; then
  AWS_CMD=(awslocal)
else
  AWS_CMD=(aws --endpoint-url=http://localhost:4566 --region us-east-1 --no-cli-pager)
fi

OUT_FILE="/tmp/${FUNCTION}-out.json"

echo "=== Invoking $FUNCTION with $EVENT ==="
INVOKE_META=$("${AWS_CMD[@]}" lambda invoke \
  --function-name "$FUNCTION" \
  --payload "fileb://$EVENT" \
  --cli-binary-format raw-in-base64-out \
  --log-type Tail \
  "$OUT_FILE")

echo "$INVOKE_META" | jq -r '.LogResult // ""' | base64 -d 2>/dev/null || true

echo ""
echo "── Output ──"
cat "$OUT_FILE"; echo

FN_ERROR=$(echo "$INVOKE_META" | jq -r '.FunctionError // ""')
if [[ -n "$FN_ERROR" ]]; then
  echo ""
  echo "FAIL — Lambda returned an error: $FN_ERROR"
  exit 1
fi

# Verify S3 key if expected
if [[ -n "$EXPECTED_S3_KEY" ]]; then
  echo ""
  echo "Checking S3 for key: $EXPECTED_S3_KEY"
  if "${AWS_CMD[@]}" s3 ls "s3://test-bucket/$EXPECTED_S3_KEY" >/dev/null 2>&1; then
    echo "PASS — S3 object found."
  else
    echo "FAIL — Expected S3 key '$EXPECTED_S3_KEY' not found in test-bucket."
    exit 1
  fi
fi

# Verify SQS message if expected
if [[ -n "$EXPECTED_SQS_QUEUE" ]]; then
  echo ""
  echo "Checking SQS queue: $EXPECTED_SQS_QUEUE"
  QUEUE_URL="http://localhost:4566/000000000000/$EXPECTED_SQS_QUEUE"
  MSG=$("${AWS_CMD[@]}" sqs receive-message --queue-url "$QUEUE_URL" --max-number-of-messages 1 2>/dev/null || echo "{}")
  if echo "$MSG" | jq -e '.Messages[0].Body' >/dev/null 2>&1; then
    echo "PASS — Message found in SQS queue."
    echo "$MSG" | jq -r '.Messages[0].Body'
  else
    echo "FAIL — No message found in SQS queue '$EXPECTED_SQS_QUEUE'."
    exit 1
  fi
fi

echo ""
echo "=== Test passed for $FUNCTION ==="
