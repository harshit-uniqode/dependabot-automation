#!/usr/bin/env bash
# teardown.sh
# Removes all test resources from the local emulator.
# Safe to run — only deletes from localhost:4566, never real AWS.

set -euo pipefail

ENDPOINT="http://localhost:4566"
REGION="us-east-1"
AWS="aws --endpoint-url=$ENDPOINT --region $REGION --no-cli-pager --output text 2>/dev/null"

echo "=== Tearing down local AWS resources ==="

echo ""
echo "Deleting S3 buckets..."
for bucket in $($AWS s3 ls | awk '{print $3}'); do
  $AWS s3 rm "s3://$bucket" --recursive 2>/dev/null || true
  $AWS s3 rb "s3://$bucket" && echo "  ✓ deleted s3://$bucket"
done

echo ""
echo "Deleting SQS queues..."
for url in $($AWS sqs list-queues --query 'QueueUrls[]' --output text 2>/dev/null); do
  $AWS sqs delete-queue --queue-url "$url" && echo "  ✓ deleted $url"
done

echo ""
echo "Deleting DynamoDB tables..."
for table in $($AWS dynamodb list-tables --query 'TableNames[]' --output text 2>/dev/null); do
  $AWS dynamodb delete-table --table-name "$table" && echo "  ✓ deleted $table"
done

echo ""
echo "Deleting SNS topics..."
for arn in $($AWS sns list-topics --query 'Topics[].TopicArn' --output text 2>/dev/null); do
  $AWS sns delete-topic --topic-arn "$arn" && echo "  ✓ deleted $arn"
done

echo ""
echo "=== Teardown complete. ==="
