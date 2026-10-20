#!/usr/bin/env bash
# Usage: ./deploy_lambda.sh <folder> <function-name> <memory-mb> <timeout-s> [layer-arn] [ephemeral-mb]
# Pass "" as layer-arn to set only the ephemeral storage.
# Example: ./deploy_lambda.sh SP03-game-catalog-parse-results game-catalog-parse-results 1024 300
set -euo pipefail

FOLDER="$1"; NAME="$2"; MEMORY="$3"; TIMEOUT="$4"; LAYER="${5:-}"; EPHEMERAL="${6:-512}"
REGION="us-east-1"
ROLE="${NAME}-role"
DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. Role: trust policy + the function's own inline policy + CloudWatch Logs
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "file://$DIR/trust-policy.json" >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "Waiting for the new role to propagate..."
  sleep 10
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name "${NAME}-policy" \
  --policy-document "file://$DIR/$FOLDER/policy-role.json"
ROLE_ARN=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)

# 2. Package the code
ZIP="/tmp/${NAME}.zip"
(cd "$DIR/$FOLDER" && zip -q -j "$ZIP" lambda_function.py)

# 3. Create or update the function
if aws lambda get-function --function-name "$NAME" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$NAME" \
    --zip-file "fileb://$ZIP" --region "$REGION" >/dev/null
  echo "Updated $NAME"
else
  aws lambda create-function --function-name "$NAME" \
    --runtime python3.12 --handler lambda_function.lambda_handler \
    --role "$ROLE_ARN" --zip-file "fileb://$ZIP" \
    --memory-size "$MEMORY" --timeout "$TIMEOUT" \
    --ephemeral-storage "Size=$EPHEMERAL" \
    ${LAYER:+--layers "$LAYER"} \
    --region "$REGION" >/dev/null
  echo "Created $NAME"
fi
