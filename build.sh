#!/usr/bin/env bash
# Packages the Lambda function + dependencies and uploads the zip to the
# deployment bucket that was created by the CloudFormation stack.
#
# Usage (after stack exists):
#   ./build.sh <stack-name> [aws-region]
#
# Usage (bucket name known directly):
#   ./build.sh --bucket <bucket-name> [aws-region]
#
# Output:
#   Prints the LambdaCodeS3Key value to use when deploying/updating the stack.

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

KEY="ai-guard-monitor/lambda-$(date +%Y%m%d-%H%M%S).zip"
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

echo ""
echo "======================================================"
echo "  Build complete."
echo "======================================================"
echo "  LambdaCodeS3Key : ${KEY}"
echo ""
echo "Update the stack with this key:"
echo "  aws cloudformation update-stack \\"
echo "    --stack-name ${STACK_NAME:-ai-guard-monitor} \\"
echo "    --use-previous-template \\"
echo "    --parameters ParameterKey=LambdaCodeS3Key,ParameterValue=${KEY} \\"
echo "                 ParameterKey=<other-params>,UsePreviousValue=true \\"
echo "    --region ${REGION}"
echo ""
