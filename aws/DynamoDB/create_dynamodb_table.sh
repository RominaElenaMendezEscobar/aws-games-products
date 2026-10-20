#!/usr/bin/env bash
set -euo pipefail

aws dynamodb create-table \
  --table-name game-catalog-products \
  --attribute-definitions \
      AttributeName=parent_asin,AttributeType=S \
      AttributeName=canonical_id,AttributeType=S \
      AttributeName=family_id,AttributeType=S \
  --key-schema AttributeName=parent_asin,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --global-secondary-indexes '[
    {"IndexName": "canonical_id-index",
     "KeySchema": [{"AttributeName": "canonical_id", "KeyType": "HASH"}],
     "Projection": {"ProjectionType": "ALL"}},
    {"IndexName": "family_id-index",
     "KeySchema": [{"AttributeName": "family_id", "KeyType": "HASH"}],
     "Projection": {"ProjectionType": "ALL"}}
  ]' \
  --region us-east-1