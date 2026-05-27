#!/usr/bin/env python3
"""
Interactive setup for AI Guard S3 Monitor.

Handles the correct deployment order automatically:
  1. Create the Lambda deployment bucket (if it does not exist)
  2. Build and upload the Lambda zip to that bucket
  3. Deploy the CloudFormation stack in a single pass

Run:  python3 configure.py
"""

import getpass
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).parent / "cfn-deploy.sh"
PARAMS_FILE   = Path(__file__).parent / "cfn-parameters.json"
SRC_DIR       = Path(__file__).parent / "src"
TEMPLATE_FILE = Path(__file__).parent / "template.yaml"
STACK_NAME    = "ai-guard-monitor"
LAMBDA_S3_KEY = "lambda/package.zip"

ENDPOINT_OPTIONS = {
    "1": ("US (default)", "https://api.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails"),
    "2": ("EU",           "https://api.eu.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails"),
    "3": ("AU",           "https://api.au.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails"),
    "4": ("JP",           "https://api.jp.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails"),
    "5": ("SG",           "https://api.sg.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails"),
}

EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


# ── helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "", validator=None, secret: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        raw = (getpass.getpass(f"  {prompt}: ") if secret
               else input(f"{prompt}{hint}: ")).strip()
        value = raw or default
        if not value:
            print("  Required — please enter a value.")
            continue
        if validator and (err := validator(value)):
            print(f"  {err}")
            continue
        return value


def ask_optional(prompt: str, default: str = "", validator=None) -> str:
    hint = f" [{default}]" if default else " [leave blank to skip]"
    while True:
        value = (input(f"{prompt}{hint}: ").strip()) or default
        if value and validator and (err := validator(value)):
            print(f"  {err}")
            continue
        return value


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        try:
            v = int(raw) if raw else default
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(f"  Enter a number between {lo} and {hi}.")


def ask_yn(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:   return default
        if raw in ("y", "yes"): return True
        if raw in ("n", "no"):  return False
        print("  Please type y or n.")


def pick_from_list(prompt: str, items: list[str]) -> str:
    for i, item in enumerate(items, 1):
        print(f"  {i:>3}.  {item}")
    while True:
        raw = input(f"{prompt}: ").strip()
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                return items[idx - 1]
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(items)}.")


def pick_or_new_bucket(prompt: str, items: list[str], default_new: str) -> str:
    """
    Show numbered list of existing buckets plus a '0. enter a new name' option.
    Returns the chosen bucket name (existing or brand new).
    """
    if not items:
        return ask(
            "Bucket name", default=default_new,
            validator=lambda v: None if BUCKET_RE.match(v)
            else "3-63 chars, lowercase, letters/numbers/hyphens/dots.",
        )
    print()
    for i, item in enumerate(items, 1):
        print(f"  {i:>3}.  {item}")
    print(f"    0.  (enter a new bucket name - will be created)")
    print()
    while True:
        raw = input(f"{prompt}: ").strip()
        if raw == "0":
            return ask(
                "New bucket name", default=default_new,
                validator=lambda v: None if BUCKET_RE.match(v)
                else "3-63 chars, lowercase, letters/numbers/hyphens/dots.",
            )
        try:
            idx = int(raw)
            if 1 <= idx <= len(items):
                return items[idx - 1]
        except ValueError:
            pass
        print(f"  Enter 0 or a number between 1 and {len(items)}.")


def header(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


# ── AWS helpers ───────────────────────────────────────────────────────────────

def get_regional_buckets(s3_client, region: str) -> list[str]:
    try:
        all_buckets = [b["Name"] for b in s3_client.list_buckets().get("Buckets", [])]
    except Exception as exc:
        print(f"  Warning: could not list buckets — {exc}")
        return []
    result = []
    for name in all_buckets:
        try:
            loc = s3_client.get_bucket_location(Bucket=name)
            if (loc["LocationConstraint"] or "us-east-1") == region:
                result.append(name)
        except Exception:
            pass
    return result


def get_account_id(boto3, region: str) -> str:
    sts = boto3.client("sts", region_name=region)
    return sts.get_caller_identity()["Account"]


def ensure_deploy_bucket(boto3, bucket_name: str, region: str) -> None:
    """Create the Lambda deployment bucket if it does not already exist."""
    s3 = boto3.client("s3", region_name=region)
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        print(f"  Created deployment bucket: {bucket_name}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print(f"  Deployment bucket already exists: {bucket_name}")
    except Exception as exc:
        # us-east-1 returns a different exception when the bucket already exists
        if "BucketAlreadyOwnedByYou" in str(exc) or "BucketAlreadyExists" in str(exc):
            print(f"  Deployment bucket already exists: {bucket_name}")
        else:
            raise

    # Apply encryption and public-access block (idempotent)
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )


def ensure_log_bucket(boto3, bucket_name: str, region: str) -> str:
    """
    Create the log bucket with our recommended settings if it does not exist.
    If it already exists, leave it untouched (the user may have their own
    configuration we should not overwrite).

    Returns "created" or "existing" so the caller can show the right message.
    """
    s3 = boto3.client("s3", region_name=region)

    # First check whether the bucket already exists (and we own it)
    try:
        s3.head_bucket(Bucket=bucket_name)
        return "existing"
    except Exception:
        # 404 / NoSuchBucket / Forbidden -> attempt to create below
        pass

    # Create the bucket
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
    except s3.exceptions.BucketAlreadyOwnedByYou:
        return "existing"
    except Exception as exc:
        if "BucketAlreadyOwnedByYou" in str(exc):
            return "existing"
        raise

    # Apply recommended settings (only on newly created buckets)
    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_versioning(
        Bucket=bucket_name,
        VersioningConfiguration={"Status": "Enabled"},
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "ArchiveToGlacier",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "logs/"},
                    "Transitions": [{"Days": 90, "StorageClass": "GLACIER"}],
                },
                {
                    "ID": "ExpireAfter7Years",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "logs/"},
                    "Expiration": {"Days": 2555},
                },
                {
                    "ID": "CleanUpOldVersions",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "logs/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                },
            ]
        },
    )
    return "created"


# ── Lambda packaging ──────────────────────────────────────────────────────────

def build_and_upload(bucket: str, region: str, boto3) -> None:
    """Package src/ + dependencies and upload to s3://<bucket>/lambda/package.zip."""
    with tempfile.TemporaryDirectory() as build_dir:
        print("\n  Installing Python dependencies (Linux x86_64 wheels for Lambda)...")
        # Lambda runs on Linux; force pip to grab Linux wheels even if we're
        # on macOS, otherwise C-extension packages (lxml, pillow, etc.) get
        # built for the local OS and fail at import time inside Lambda.
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "-r", str(SRC_DIR / "requirements.txt"),
             "-t", build_dir,
             "--platform", "manylinux2014_x86_64",
             "--only-binary=:all:",
             "--python-version", "3.12",
             "--implementation", "cp",
             "--upgrade",
             "-q"],
            check=True,
        )
        print("  Copying Lambda source files...")
        for f in SRC_DIR.glob("*.py"):
            shutil.copy(f, build_dir)

        print("  Creating deployment zip...")
        zip_path = Path(tempfile.mktemp(suffix=".zip"))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in Path(build_dir).rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(build_dir))

        print(f"  Uploading to s3://{bucket}/{LAMBDA_S3_KEY} ...")
        s3 = boto3.client("s3", region_name=region)
        s3.upload_file(str(zip_path), bucket, LAMBDA_S3_KEY)
        zip_path.unlink(missing_ok=True)


# ── output files ──────────────────────────────────────────────────────────────

def write_deploy_script(params: dict, region: str) -> None:
    overrides = " \\\n    ".join(f'{k}="{v}"' for k, v in params.items())
    script = f"""\
#!/usr/bin/env bash
# Generated by configure.py — do not commit (contains your API key)
# Re-run this script to update stack configuration.
# To update Lambda code, run: make build
set -euo pipefail
aws cloudformation deploy \\
  --template-file template.yaml \\
  --stack-name {STACK_NAME} \\
  --region "{region}" \\
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \\
  --parameter-overrides \\
    {overrides}
echo "Stack deployed. Check Outputs tab in CloudFormation for next steps."
"""
    DEPLOY_SCRIPT.write_text(script)
    DEPLOY_SCRIPT.chmod(0o755)


def write_params_json(params: dict) -> None:
    entries = [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()]
    PARAMS_FILE.write_text(json.dumps(entries, indent=2))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║      AI Guard S3 Monitor — Interactive Setup             ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print("\nThis script collects your settings, creates the Lambda")
    print("deployment bucket, uploads the code, and deploys the stack.\n")

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("Error: boto3 is required.  Run:  pip install boto3")
        sys.exit(1)

    # ── Region ────────────────────────────────────────────────────────────────
    header("1 / 5  —  AWS Region")
    session = boto3.session.Session()
    region = ask("AWS region", default=session.region_name or "us-east-1")

    s3  = boto3.client("s3",             region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)

    # ── Source bucket ─────────────────────────────────────────────────────────
    header("2 / 5  —  Source S3 Bucket  (existing bucket to monitor)")
    print(f"\nFetching your S3 buckets in {region}...\n")
    regional = get_regional_buckets(s3, region)

    if regional:
        source_bucket = pick_from_list("Select bucket to monitor [number]", regional)
    else:
        print("  No buckets found in this region.")
        source_bucket = ask("Enter bucket name manually",
                            validator=lambda v: None if BUCKET_RE.match(v)
                            else "3-63 chars, lowercase, letters/numbers/hyphens/dots.")
    print(f"\n  Source bucket: {source_bucket}")

    # ── Log bucket ────────────────────────────────────────────────────────────
    header("3 / 5  —  Log Bucket  (pick existing or create new)")
    # Exclude the source bucket so the user cannot pick it as the log target
    # (it would self-trigger the scanner on every log write).
    log_candidates = [b for b in regional if b != source_bucket]
    log_bucket = pick_or_new_bucket(
        "Select log bucket or 0 for new [number]",
        log_candidates,
        default_new=f"{source_bucket}-ai-guard-logs",
    )

    # ── AI Guard ──────────────────────────────────────────────────────────────
    header("4 / 5  —  Trend Micro AI Guard")
    print("\nCreate API key: Vision One Console -> Administration -> API Keys\n")
    api_key = ask("Vision One API Key", secret=True,
                  validator=lambda v: "Key too short." if len(v) < 10 else None)

    print("\nVision One region endpoint:")
    for k, (label, _) in ENDPOINT_OPTIONS.items():
        print(f"  {k}.  {label}")
    ep_key = ask("\nSelect endpoint", default="1",
                 validator=lambda v: None if v in ENDPOINT_OPTIONS
                 else f"Enter 1-{len(ENDPOINT_OPTIONS)}.")
    ep_label, endpoint_url = ENDPOINT_OPTIONS[ep_key]
    print("\n  Note: each scan is tagged with '<bucket>/<file>' automatically.")
    print("  This 'Application name' is only used as a fallback identifier.")
    app_name = ask("Fallback application name", default="ai-guard-s3-monitor",
                   validator=lambda v: None if re.match(r"^[a-zA-Z0-9_-]{1,64}$", v)
                   else "Letters, numbers, hyphens, underscores only (max 64 chars).")

    # ── Notifications ─────────────────────────────────────────────────────────
    header("5 / 5  —  Settings & Notifications  (Enter to accept defaults)")
    print()
    notification_email = ask_optional("Alert recipient email (blank = disable)",
                                      validator=lambda v: None if EMAIL_RE.match(v)
                                      else "Not a valid email.")
    ses_sender = ""
    if notification_email:
        ses_sender = ask_optional("SES verified sender (FROM address)",
                                  default=notification_email,
                                  validator=lambda v: None if EMAIL_RE.match(v)
                                  else "Not a valid email.")

    max_text_kb    = ask_int("Max text per file (KB, 10-2048)",  500,  10, 2048)
    lambda_memory  = ask_int("Lambda memory MB [256/512/1024/2048]", 512, 256, 2048)
    lambda_timeout = ask_int("Lambda timeout seconds (30-900)", 300, 30, 900)
    log_retention  = ask_int("CloudWatch log retention days",    90,   7, 365)
    print()
    enable_cw = ask_yn("Enable CloudWatch Dashboard + Alarms?", default=False)

    # ── Derive deployment bucket name ─────────────────────────────────────────
    # Must match the !Sub expression in template.yaml exactly:
    #   "${AWS::StackName}-deploy-${AWS::AccountId}"
    print("\n  Resolving AWS account ID...")
    account_id = get_account_id(boto3, region)
    deploy_bucket = f"{STACK_NAME}-deploy-{account_id}"

    # ── Summary ───────────────────────────────────────────────────────────────
    header("Summary")
    rows = [
        ("Region",                 region),
        ("Source bucket",          source_bucket),
        ("Log bucket",             log_bucket),
        ("Lambda deploy bucket",   deploy_bucket),
        ("AI Guard endpoint",      ep_label),
        ("Fallback app name",      app_name),
        ("Alert recipient",        notification_email or "(disabled)"),
        ("SES sender",             ses_sender or "(disabled)"),
        ("Max text",               f"{max_text_kb} KB"),
        ("Lambda memory",          f"{lambda_memory} MB"),
        ("Lambda timeout",         f"{lambda_timeout}s"),
        ("Log retention",          f"{log_retention} days"),
        ("CloudWatch monitoring",  "Yes" if enable_cw else "No"),
    ]
    for label, value in rows:
        print(f"  {label:<28} {value}")

    print()
    if not ask_yn("Proceed with deployment?", default=True):
        print("\nAborted.")
        sys.exit(0)

    # ── Step 1: Create the Lambda deployment bucket ───────────────────────────

    print("\n" + "=" * 60)
    print("  Step 1 — Creating Lambda deployment bucket")
    print("=" * 60 + "\n")

    ensure_deploy_bucket(boto3, deploy_bucket, region)

    # ── Step 2: Build and upload Lambda code ──────────────────────────────────

    print("\n" + "=" * 60)
    print("  Step 2 — Building and uploading Lambda code")
    print("=" * 60)

    build_and_upload(deploy_bucket, region, boto3)
    print(f"\n  Lambda code uploaded to s3://{deploy_bucket}/{LAMBDA_S3_KEY}")

    # ── Step 3: Prepare the log bucket ────────────────────────────────────────

    print("\n" + "=" * 60)
    print("  Step 3 — Preparing log bucket")
    print("=" * 60 + "\n")

    log_status = ensure_log_bucket(boto3, log_bucket, region)
    if log_status == "created":
        print(f"  Created log bucket: {log_bucket}")
        print(f"  Applied: encryption, versioning, lifecycle (90d -> Glacier, 7y expire)")
    else:
        print(f"  Log bucket already exists, using as-is: {log_bucket}")
        print(f"  (Existing settings preserved - no lifecycle rules applied)")

    # ── Step 4: Deploy the full stack ─────────────────────────────────────────

    print("\n" + "=" * 60)
    print("  Step 4 — Deploying CloudFormation stack")
    print("=" * 60 + "\n")

    stack_params = {
        "AIGuardApiKey":               api_key,
        "AIGuardEndpoint":             endpoint_url,
        "AIGuardAppName":              app_name,
        "SourceBucketName":            source_bucket,
        "LogBucketName":               log_bucket,
        "NotificationEmail":           notification_email,
        "SESVerifiedSender":           ses_sender,
        "MaxTextKB":                   str(max_text_kb),
        "LambdaMemoryMB":              str(lambda_memory),
        "LambdaTimeoutSeconds":        str(lambda_timeout),
        "LogRetentionDays":            str(log_retention),
        "EnableCloudWatchMonitoring":  "Yes" if enable_cw else "No",
    }

    write_deploy_script(stack_params, region)
    write_params_json(stack_params)

    deploy_result = subprocess.run([
        "aws", "cloudformation", "deploy",
        "--template-file", str(TEMPLATE_FILE),
        "--stack-name", STACK_NAME,
        "--region", region,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_NAMED_IAM",
        "--parameter-overrides",
        *[f"{k}={v}" for k, v in stack_params.items()],
        "--no-fail-on-empty-changeset",
    ])

    if deploy_result.returncode == 0:
        print("\n  Stack deployed successfully!")
        if notification_email and ses_sender:
            print(f"\n  ACTION REQUIRED — verify the SES sender email:")
            print(f"  aws ses verify-email-identity --email-address {ses_sender} --region {region}")
        print(f"\n  cfn-deploy.sh saved for future configuration updates.")
        print(f"  To update Lambda code later, run:  make build")
    else:
        print("\n  Deployment failed. Check the CloudFormation console for details.")
        print(f"  cfn-deploy.sh was written for manual retry.")

    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
