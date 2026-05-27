# AI Guard AWS Monitor

Serverless solution that monitors an S3 bucket for document uploads, scans extracted text
with [Trend Micro AI Guard](https://docs.trendmicro.com/en-us/documentation/article/trend-vision-one-ai-guard-api-reference),
and responds to detections with an email alert and a structured log entry.

## Architecture

```
Source S3 Bucket
      │ s3:ObjectCreated:*
      ▼
  Lambda (Python 3.12)
  ├── Download file
  ├── Extract text (first 500 KB)
  ├── SHA-256 hash
  ├── POST → Trend Micro AI Guard API
  └── action == "Block"?
        ├── PUT log → Log S3 Bucket (logs/YYYY/MM/DD/...)
        └── SES → af.us@outlook.com
```

### Supported file types

| Extension | Extraction method |
|-----------|-------------------|
| `.txt` `.csv` `.md` `.yaml` `.xml` `.html` `.htm` | Direct decode (UTF-8 → UTF-16 → Latin-1 fallback) |
| `.json` | Parse and re-serialise |
| `.pdf` | `pypdf` |
| `.docx` `.doc` | `python-docx` |
| `.xlsx` `.xls` | `openpyxl` |
| `.pptx` `.ppt` | `python-pptx` |
| `.rtf` | `striprtf` |

---

## Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.12+ |
| [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) | 1.100+ |
| AWS CLI | 2.x (configured with deploy permissions) |
| Docker | Required for `sam build --use-container` |

---

## Configuration

### 1 — Trend Micro Vision One API key

1. Log in to [Trend Micro Vision One](https://portal.xdr.trendmicro.com).
2. Go to **Administration → API Keys** and create a key with AI Guard read permission.
3. Note the key value — it will be passed as the `AIGuardApiKey` parameter.

> **Regional endpoints** — If your tenant is in a non-default region replace the endpoint:
>
> | Region | Endpoint |
> |--------|----------|
> | US (default) | `https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan` |
> | EU | `https://api.eu.xdr.trendmicro.com/v3.0/xdr/guard/scan` |
> | AU | `https://api.au.xdr.trendmicro.com/v3.0/xdr/guard/scan` |
> | JP | `https://api.jp.xdr.trendmicro.com/v3.0/xdr/guard/scan` |
> | SG | `https://api.sg.xdr.trendmicro.com/v3.0/xdr/guard/scan` |

### 2 — Verify SES sender address

```bash
aws ses verify-email-identity --email-address your-sender@example.com --region us-east-1
```

Check your inbox and click the verification link. The recipient (`af.us@outlook.com`) also
needs to be verified if your SES account is in sandbox mode.

To move out of sandbox: **AWS Console → SES → Account dashboard → Request production access**.

### 3 — Create samconfig.toml

```bash
cp samconfig.toml.example samconfig.toml
# Edit samconfig.toml and fill in all placeholder values
```

---

## Deployment

```bash
# First time (interactive)
make deploy-guided

# Subsequent deploys
make deploy
```

S3 bucket names must be globally unique. Choose something like
`acme-ai-guard-source-2024` and `acme-ai-guard-logs-2024`.

### Tear down

```bash
make destroy
```

---

## Running tests

```bash
# Install dev dependencies and run the test suite
make test
```

Tests use `moto` to mock AWS services and `responses` to mock the AI Guard API — no live
AWS credentials or Trend Micro keys are required.

---

## Log format

Each detection is saved as a JSON file under:

```
s3://<log-bucket>/logs/YYYY/MM/DD/<filename>_<HHMMSSffffff>.json
```

Example entry:

```json
{
  "timestamp": "2024-06-01T14:23:45.123456+00:00",
  "file_name": "uploads/suspicious-doc.pdf",
  "file_hash_sha256": "e3b0c44298fc1c149afbf4c8996fb924...",
  "scan_id": "a1b2c3d4-e5f6-...",
  "action": "Block",
  "reasons": [
    { "message": "Prompt injection detected" }
  ],
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
File Name : uploads/suspicious-doc.pdf
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

## Environment variables (Lambda)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AI_GUARD_API_KEY` | Yes | — | Trend Micro Vision One API key |
| `AI_GUARD_ENDPOINT` | No | US endpoint | AI Guard API URL |
| `AI_GUARD_APP_NAME` | No | `ai-guard-s3-monitor` | `TMV1-Application-Name` header value |
| `LOG_BUCKET_NAME` | Yes | — | Secondary bucket for detection logs |
| `NOTIFICATION_EMAIL` | Yes | — | Alert recipient email |
| `SES_SENDER_EMAIL` | Yes | — | Verified SES sender email |

---

## Security notes

- S3 buckets are private and server-side encrypted (AES-256).
- The API key is passed as a CloudFormation `NoEcho` parameter and stored in Lambda environment variables. For production, consider [AWS Secrets Manager](https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html) instead.
- Detection logs are archived to Glacier after 90 days and deleted after 7 years.
- Lambda IAM role is scoped with least privilege (read source bucket, write log bucket, send SES email).
