#!/usr/bin/env bash
# Packages the Lambda function + dependencies and uploads the zip to S3.
# Run this BEFORE deploying the CloudFormation stack.
#
# Usage:
#   ./build.sh <s3-staging-bucket> [aws-region]
#
# Output:
#   Prints LambdaCodeS3Bucket and LambdaCodeS3Key values to use as CFT parameters.

set -euo pipefail

BUCKET="${1:-}"
REGION="${2:-us-east-1}"

if [[ -z "$BUCKET" ]]; then
  echo "Usage: ./build.sh <s3-staging-bucket> [region]"
  echo ""
  echo "  The staging bucket is used to store the Lambda zip for CloudFormation."
  echo "  It can be any bucket you own in the target region."
  echo ""
  echo "  Example: ./build.sh my-deploy-bucket us-east-1"
  exit 1
fi

KEY="ai-guard-monitor/lambda-$(date +%Y%m%d-%H%M%S).zip"
BUILD_DIR="$(mktemp -d)"
ZIP_FILE="$(mktemp).zip"

cleanup() {
  rm -rf "$BUILD_DIR" "$ZIP_FILE" 2>/dev/null || true
}
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
echo "  Build complete. CloudFormation parameter values:"
echo "======================================================"
echo "  LambdaCodeS3Bucket : ${BUCKET}"
echo "  LambdaCodeS3Key    : ${KEY}"
echo ""
echo "Run './configure.py' or pass these values when deploying the stack."
