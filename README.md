# AI Guard S3 Document Monitor

Serverless solution that monitors an **existing** S3 bucket for document uploads,
scans extracted text with [Trend Micro AI Guard](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-ai-guard-api-reference),
and on a detection sends an email alert and writes a structured JSON log.

---

## Architecture

```
Existing S3 Bucket
      │  s3:ObjectCreated:*  (added automatically by the stack)
      ▼
  Lambda — Python 3.12 / arm64
  ├── Download file from S3
  ├── Extract text (PDF/DOCX/XLSX/PPTX/TXT/CSV/RTF/…)
  ├── Truncate to first N KB  (configurable, default 500 KB)
  ├── SHA-256 hash
  ├── POST → Trend Micro AI Guard API
  └── action == "Block"?
        ├── PUT JSON log → Log S3 Bucket  (logs/YYYY/MM/DD/…)
        └── SES → notification email

[Optional]
  CloudWatch Dashboard  +  Alarms (Lambda errors, blocked detections)
```

### What file types are scanned?

| Extension | Library |
|-----------|---------|
| `.txt` `.csv` `.md` `.yaml` `.xml` `.html` `.htm` | Direct UTF-8 decode |
| `.json` | Parse + re-serialise |
| `.pdf` | `pypdf` |
| `.docx` `.doc` | `python-docx` |
| `.xlsx` `.xls` | `openpyxl` |
| `.pptx` `.ppt` | `python-pptx` |
| `.rtf` | `striprtf` |

All other extensions (`.zip`, `.png`, `.mp4`, …) are silently skipped.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.12+ | [python.org](https://python.org) |
| AWS SAM CLI | 1.100+ | [SAM install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) |
| AWS CLI | 2.x | [AWS CLI install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Docker | Any recent | Required for `sam build --use-container` |

---

## Deployment

### Step 1 — Clone and run the interactive setup

```bash
git clone https://github.com/YOUR_USERNAME/ai-guard-aws.git
cd ai-guard-aws
pip install boto3          # only needed for the setup script
make configure             # or: python3 configure.py
```

The script connects to your AWS account, lists the S3 buckets in the selected
region so you can pick the one to monitor, prompts for all other parameters,
and writes `samconfig.toml` automatically. No manual file editing required.

Alternatively you can copy and edit the example file manually:

```bash
cp samconfig.toml.example samconfig.toml
```

### Step 2 — Verify your SES sender address

Alerts will not be delivered until Amazon SES has verified the sender email.

```bash
aws ses verify-email-identity \
  --email-address your-sender@example.com \
  --region us-east-1
```

Check the inbox for the verification link and click it.

> **SES Sandbox** — New AWS accounts start in SES sandbox mode, which also requires
> verifying the *recipient* address (`af.us@outlook.com`).
> To send to any address, request production access:
> **AWS Console → SES → Account dashboard → Request production access**.

### Step 3 — Deploy

```bash
# First time (interactive, creates samconfig.toml automatically if you prefer)
make deploy-guided

# Subsequent deploys
make deploy
```

After a successful deploy the **Outputs** tab in CloudFormation (or the SAM CLI output)
will show:
- The scanner Lambda ARN
- The log bucket URL
- The SES verify command (reminder, in case you haven't done step 2)
- The CloudWatch Dashboard URL (if monitoring was enabled)

### Tear down

```bash
make destroy
```

> The log bucket has `DeletionPolicy: Retain` — it is **not** deleted when the stack is
> removed, so your detection history is preserved.

---

## CFT Parameters reference

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `AIGuardApiKey` | Yes | — | Vision One API key (NoEcho) |
| `AIGuardEndpoint` | No | US endpoint | Regional AI Guard URL |
| `AIGuardAppName` | No | `ai-guard-s3-monitor` | `TMV1-Application-Name` header |
| `SourceBucketName` | Yes | — | **Existing** bucket to monitor |
| `LogBucketName` | Yes | — | **New** bucket for detection logs |
| `NotificationEmail` | No | `af.us@outlook.com` | Alert recipient |
| `SESVerifiedSender` | Yes | — | Verified SES FROM address |
| `MaxTextKB` | No | `500` | Max KB of text per scan (10–2048) |
| `LambdaMemoryMB` | No | `512` | Lambda memory: 256/512/1024/2048 MB |
| `LambdaTimeoutSeconds` | No | `300` | Lambda timeout in seconds (30–900) |
| `LogRetentionDays` | No | `90` | CloudWatch log retention |
| `EnableCloudWatchMonitoring` | No | `No` | `Yes` adds dashboard + alarms |

---

## Detection log format

Logs are written to `s3://<log-bucket>/logs/YYYY/MM/DD/<filename>_<timestamp>.json`:

```json
{
  "timestamp": "2024-06-01T14:23:45.123456+00:00",
  "file_name": "uploads/report.pdf",
  "file_hash_sha256": "e3b0c44298fc1c149afbf4c8996fb924...",
  "scan_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "action": "Block",
  "reasons": [{ "message": "Prompt injection detected" }],
  "mitre_attack": ["T1190"],
  "owasp": ["LLM01"],
  "malicious_prompt_snippet": "Ignore previous instructions and...",
  "full_scan_result": { ... }
}
```

---

## Email alert format

```
AI Guard S3 Monitor — Security Alert
==================================================
File Name : uploads/report.pdf
SHA-256   : e3b0c44298fc1c...
Scan ID   : a1b2c3d4-e5f6-...
Action    : BLOCK

Detection Reasons:
  - Prompt injection detected

OWASP / OWASP LLM Top 10 References:
  - LLM01

Please review the file immediately and take appropriate action.
```

---

## Running tests

No live AWS credentials or Trend Micro API key are needed — tests use `moto` and
`responses` to mock all external services.

```bash
make test
```

---

## Security notes

- The API key is passed as a `NoEcho` CloudFormation parameter and stored in Lambda
  environment variables. For a higher-security setup, store it in
  [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html)
  and grant the Lambda `secretsmanager:GetSecretValue`.
- The log bucket is private, server-side encrypted (AES-256), and versioned.
- The Lambda IAM role uses least privilege: read from the source bucket, write to
  `logs/*` in the log bucket, and send SES email — nothing else.
- The S3 notification is removed cleanly when the stack is deleted (custom resource).
