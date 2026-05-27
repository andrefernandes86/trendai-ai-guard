#!/usr/bin/env bash
# Packages the Lambda function + dependencies and uploads the zip to the
# deployment bucket, then updates the Lambda function code in-place.
#
# Usage (after stack exists):
#   ./build.sh <stack-name> [aws-region]
#
# Usage (bucket name known directly):
#   ./build.sh --bucket <bucket-name> [aws-region]

set -euo pipefail

REGION="us-east-1"
BUCKET=""
STACK_NAME=""

# Parse arguments
if [[ "${1:-}" == "--bucket" ]]; then
  BUCKET="${2:-}"
  REGION="${3:-$REGION}"
else
  STACK_NAME="${1:-ai-guard-monitor}"
  REGION="${2:-$REGION}"
fi

# If no bucket given, look it up from the stack outputs
if [[ -z "$BUCKET" ]]; then
  echo ">>> Looking up deployment bucket from stack: ${STACK_NAME} ..."
  BUCKET=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='LambdaDeployBucketName'].OutputValue" \
    --output text 2>/dev/null || true)

  if [[ -z "$BUCKET" || "$BUCKET" == "None" ]]; then
    echo ""
    echo "ERROR: Could not find LambdaDeployBucketName in stack '${STACK_NAME}' outputs."
    echo "       Make sure the stack is deployed first, then run this script."
    echo ""
    echo "  Alternatively, pass the bucket name directly:"
    echo "  ./build.sh --bucket <bucket-name> [region]"
    exit 1
  fi
  echo ">>> Deployment bucket: ${BUCKET}"
fi

KEY="lambda/package.zip"
BUILD_DIR="$(mktemp -d)"
ZIP_FILE="$(mktemp).zip"

cleanup() { rm -rf "$BUILD_DIR" "$ZIP_FILE" 2>/dev/null || true; }
trap cleanup EXIT

echo ">>> Installing Python dependencies..."
pip3 install -r src/requirements.txt -t "$BUILD_DIR/" -q

echo ">>> Copying Lambda source files..."
cp src/*.py "$BUILD_DIR/"

echo ">>> Creating deployment zip..."
(cd "$BUILD_DIR" && zip -r "$ZIP_FILE" . -q)

echo ">>> Uploading to s3://${BUCKET}/${KEY} ..."
aws s3 cp "$ZIP_FILE" "s3://${BUCKET}/${KEY}" --region "$REGION"

# Update the Lambda function code directly (no CloudFormation parameter change needed)
FUNCTION_NAME="${STACK_NAME:-ai-guard-monitor}-scanner"
echo ">>> Updating Lambda function code: ${FUNCTION_NAME} ..."
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --s3-bucket "$BUCKET" \
  --s3-key "$KEY" \
  --region "$REGION" \
  --output text --query "CodeSize" | xargs -I{} echo "    Code size: {} bytes"

echo ""
echo "======================================================"
echo "  Build complete."
echo "======================================================"
echo "  Lambda function updated: ${FUNCTION_NAME}"
echo "  Deployment bucket:       ${BUCKET}"
echo ""
