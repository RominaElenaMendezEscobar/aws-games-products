#!/usr/bin/env bash
# Creates the vector bucket and the index used for similarity search.
set -euo pipefail

VECTOR_BUCKET="game-catalog-vectors"
INDEX="games-v2"
REGION="us-east-1"

aws s3vectors create-vector-bucket --vector-bucket-name "$VECTOR_BUCKET" --region "$REGION"

aws s3vectors create-index \
  --vector-bucket-name "$VECTOR_BUCKET" \
  --index-name "$INDEX" \
  --data-type float32 \
  --dimension 1024 \
  --distance-metric cosine \
  --metadata-configuration '{"nonFilterableMetadataKeys": ["title", "base_title"]}' \
  --region "$REGION"