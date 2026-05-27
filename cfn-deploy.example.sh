#!/usr/bin/env bash
# Example deploy script — copy to cfn-deploy.sh and fill in your values.
# cfn-deploy.sh is gitignored so your API key is never committed.
#
# Easier: run 'python3 configure.py' and it generates this file for you.

set -euo pipefail

aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name ai-guard-monitor \
  --region "us-east-1" \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    LambdaCodeS3Bucket="my-deploy-bucket" \
    LambdaCodeS3Key="ai-guard-monitor/lambda-20240101-120000.zip" \
    AIGuardApiKey="YOUR_VISION_ONE_API_KEY" \
    AIGuardEndpoint="https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan" \
    AIGuardAppName="ai-guard-s3-monitor" \
    SourceBucketName="my-existing-source-bucket" \
    LogBucketName="my-ai-guard-logs" \
    NotificationEmail="af.us@outlook.com" \
    SESVerifiedSender="sender@example.com" \
    MaxTextKB="500" \
    LambdaMemoryMB="512" \
    LambdaTimeoutSeconds="300" \
    LogRetentionDays="90" \
    EnableCloudWatchMonitoring="No"

echo ""
echo "Stack deployed. Check the Outputs tab in CloudFormation for next steps."
