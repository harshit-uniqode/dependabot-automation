#!/usr/bin/env bash
# setup-local-resources.sh
# Creates S3 buckets, SQS queues, DynamoDB tables needed for Lambda testing.
# Edit the lists below to match your Lambda's actual AWS resources.
# Run: ./scripts/setup-local-resources.sh  (or via: make setup-resources)

set -euo pipefail

ENDPOINT="http://localhost:4566"
REGION="us-east-1"
AWS="aws --endpoint-url=$ENDPOINT --region $REGION --no-cli-pager \
         --output text 2>/dev/null"

echo "=== Setting up local AWS resources at $ENDPOINT ==="

# ── S3 Buckets ────────────────────────────────────────────────────────────────
# Add your Lambda's S3 bucket names here
S3_BUCKETS=(
  "test-bucket"
  "test-media-bucket"
  "test-exports-bucket"
)

echo ""
echo "Creating S3 buckets..."
for bucket in "${S3_BUCKETS[@]}"; do
  $AWS s3 mb "s3://$bucket" && echo "  ✓ s3://$bucket" || echo "  - s3://$bucket already exists"
done

# ── SQS Queues ────────────────────────────────────────────────────────────────
# Add your Lambda's SQS queue names here
SQS_QUEUES=(
  "test-queue"
  "test-dead-letter-queue"
)

echo ""
echo "Creating SQS queues..."
for queue in "${SQS_QUEUES[@]}"; do
  $AWS sqs create-queue --queue-name "$queue" && echo "  ✓ $queue" || echo "  - $queue already exists"
done

# ── DynamoDB Tables ───────────────────────────────────────────────────────────
# Adjust attribute definitions and key schema to match your actual tables

echo ""
echo "Creating DynamoDB tables..."

# Example table — replace with your actual schema
$AWS dynamodb create-table \
  --table-name test-table \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  && echo "  ✓ test-table" || echo "  - test-table already exists"

# Add more tables as needed:
# $AWS dynamodb create-table \
#   --table-name another-table \
#   --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
#   --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
#   --billing-mode PAY_PER_REQUEST \
#   && echo "  ✓ another-table" || echo "  - another-table already exists"

# ── SNS Topics ────────────────────────────────────────────────────────────────
SNS_TOPICS=(
  "test-topic"
)

echo ""
echo "Creating SNS topics..."
for topic in "${SNS_TOPICS[@]}"; do
  $AWS sns create-topic --name "$topic" && echo "  ✓ $topic" || echo "  - $topic already exists"
done

echo ""
echo "=== Resource setup complete. Run 'make verify-resources' to confirm. ==="
