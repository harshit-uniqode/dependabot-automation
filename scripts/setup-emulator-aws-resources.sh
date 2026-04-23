#!/usr/bin/env bash
# setup-emulator-aws-resources.sh
# Creates S3 buckets, SQS queues, DynamoDB tables, and SNS topics on the
# local emulator (Floci or LocalStack). Edit the arrays below to match
# your Lambda's resource needs.
#
# Runs automatically on `make emulator-up`. Safe to re-run — existing
# resources are skipped.

set -euo pipefail

ENDPOINT="http://localhost:4566"
REGION="us-east-1"

# Prefer awslocal if installed, else aws with --endpoint-url.
if command -v awslocal >/dev/null 2>&1; then
  AWS_CMD=(awslocal)
else
  AWS_CMD=(aws --endpoint-url="$ENDPOINT" --region "$REGION" --no-cli-pager)
fi

echo "=== Setting up local AWS resources at $ENDPOINT ==="

S3_BUCKETS=(
  "test-bucket"
  "test-media-bucket"
  "test-exports-bucket"
)
echo ""
echo "Creating S3 buckets..."
for bucket in "${S3_BUCKETS[@]}"; do
  if "${AWS_CMD[@]}" s3 mb "s3://$bucket" >/dev/null 2>&1; then
    echo "  ✓ s3://$bucket"
  else
    echo "  - s3://$bucket (already exists)"
  fi
done

SQS_QUEUES=(
  "test-queue"
  "test-dead-letter-queue"
)
echo ""
echo "Creating SQS queues..."
for queue in "${SQS_QUEUES[@]}"; do
  if "${AWS_CMD[@]}" sqs create-queue --queue-name "$queue" >/dev/null 2>&1; then
    echo "  ✓ $queue"
  else
    echo "  - $queue (already exists)"
  fi
done

echo ""
echo "Creating DynamoDB tables..."
if "${AWS_CMD[@]}" dynamodb create-table \
    --table-name test-table \
    --attribute-definitions AttributeName=id,AttributeType=S \
    --key-schema AttributeName=id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null 2>&1; then
  echo "  ✓ test-table"
else
  echo "  - test-table (already exists)"
fi

SNS_TOPICS=(
  "test-topic"
)
echo ""
echo "Creating SNS topics..."
for topic in "${SNS_TOPICS[@]}"; do
  if "${AWS_CMD[@]}" sns create-topic --name "$topic" >/dev/null 2>&1; then
    echo "  ✓ $topic"
  else
    echo "  - $topic (already exists)"
  fi
done

echo ""
echo "=== Resource setup complete. Run 'make verify-resources' to confirm. ==="
