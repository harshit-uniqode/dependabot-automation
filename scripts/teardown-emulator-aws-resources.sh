#!/usr/bin/env bash
# teardown-emulator-aws-resources.sh
# Deletes all test resources from the local emulator.
# Safe: only touches localhost:4566, never real AWS.

set -euo pipefail

ENDPOINT="http://localhost:4566"
REGION="us-east-1"

if command -v awslocal >/dev/null 2>&1; then
  AWS_CMD=(awslocal)
else
  AWS_CMD=(aws --endpoint-url="$ENDPOINT" --region "$REGION" --no-cli-pager)
fi

echo "=== Tearing down local AWS resources ==="

echo ""
echo "Deleting S3 buckets..."
for bucket in $("${AWS_CMD[@]}" s3 ls 2>/dev/null | awk '{print $3}'); do
  "${AWS_CMD[@]}" s3 rm "s3://$bucket" --recursive >/dev/null 2>&1 || true
  "${AWS_CMD[@]}" s3 rb "s3://$bucket" >/dev/null 2>&1 && echo "  ✓ deleted s3://$bucket"
done

echo ""
echo "Deleting SQS queues..."
for url in $("${AWS_CMD[@]}" sqs list-queues --query 'QueueUrls[]' --output text 2>/dev/null); do
  "${AWS_CMD[@]}" sqs delete-queue --queue-url "$url" >/dev/null 2>&1 && echo "  ✓ deleted $url"
done

echo ""
echo "Deleting DynamoDB tables..."
for table in $("${AWS_CMD[@]}" dynamodb list-tables --query 'TableNames[]' --output text 2>/dev/null); do
  "${AWS_CMD[@]}" dynamodb delete-table --table-name "$table" >/dev/null 2>&1 && echo "  ✓ deleted $table"
done

echo ""
echo "Deleting SNS topics..."
for arn in $("${AWS_CMD[@]}" sns list-topics --query 'Topics[].TopicArn' --output text 2>/dev/null); do
  "${AWS_CMD[@]}" sns delete-topic --topic-arn "$arn" >/dev/null 2>&1 && echo "  ✓ deleted $arn"
done

echo ""
echo "=== Teardown complete. ==="
