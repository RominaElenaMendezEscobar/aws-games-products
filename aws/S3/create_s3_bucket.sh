#!/usr/bin/env bash
# Creates the data bucket and enables EventBridge notifications (used to trigger the ETL).
set -euo pipefail

BUCKET="game-catalog-dedup"
REGION="us-east-1"

aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-notification-configuration --bucket "$BUCKET" \
  --notification-configuration '{"EventBridgeConfiguration": {}}'

echo "Bucket $BUCKET created. Upload the dataset with:"
echo "aws s3 cp data/meta_Video_Games.jsonl s3://$BUCKET/raw/meta_Video_Games.jsonl"