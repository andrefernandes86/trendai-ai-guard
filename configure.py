#!/usr/bin/env python3
"""
Interactive setup for AI Guard S3 Monitor.

- Lists your real S3 buckets so you can pick the one to monitor
- Packages and uploads the Lambda code to S3
- Collects all other parameters
- Writes cfn-deploy.sh (ready to run) and cfn-parameters.json

Usage:
    python3 configure.py
"""

import getpass
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).parent / "cfn-deploy.sh"
PARAMS_FILE   = Path(__file__).parent / "cfn-parameters.json"
SRC_DIR       = Path(__file__).parent / "src"

ENDPOINT_OPTIONS = {
    "1": ("US (default)", "https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "2": ("EU",           "https://api.eu.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "3": ("AU",           "https://api.au.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "4": ("JP",           "https://api.jp.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "5": ("SG",           "https://api.sg.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
}

EMAIL_RE   = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
BUCKET_RE  = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


# ── helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "", validator=None, secret: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        if secret:
            raw = getpass.getpass(f"  {prompt}: ").strip()
        else:
            raw = input(f"{prompt}{hint}: ").strip()
        value = raw or default
        if not value:
            print("  Required — please enter a value.")
            continue
        if validator:
            err = validator(value)
            if err:
                print(f"  {err}")
                continue
        return value


def ask_optional(prompt: str, default: str = "", validator=None) -> str:
    hint = f" [{default}]" if default else " [leave blank to skip]"
    while True:
        raw = input(f"{prompt}{hint}: ").strip()
        value = raw or default
        if value and validator:
            err = validator(value)
            if err:
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
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
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


def header(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def val_email(v: str):
    if not EMAIL_RE.match(v):
        return "Not a valid email address."


def val_bucket(v: str):
    if not BUCKET_RE.match(v):
        return "3-63 chars, lowercase letters/numbers/hyphens/dots, start and end with letter or number."


def val_app_name(v: str):
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", v):
        return "Letters, numbers, hyphens, underscores only (max 64 chars)."


# ── Lambda packaging ──────────────────────────────────────────────────────────

def build_and_upload(staging_bucket: str, region: str, boto3) -> str:
    """Package src/ + dependencies, upload to S3, return the S3 key."""
    key = f"ai-guard-monitor/lambda-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"

    with tempfile.TemporaryDirectory() as build_dir:
        print("\n  Installing Python dependencies...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "-r", str(SRC_DIR / "requirements.txt"),
             "-t", build_dir, "-q"],
            check=True,
        )

        print("  Copying Lambda source files...")
        for py_file in SRC_DIR.glob("*.py"):
            shutil.copy(py_file, build_dir)

        print("  Creating deployment zip...")
        zip_path = Path(tempfile.mktemp(suffix=".zip"))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in Path(build_dir).rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(build_dir))

        print(f"  Uploading to s3://{staging_bucket}/{key} ...")
        s3 = boto3.client("s3", region_name=region)
        s3.upload_file(str(zip_path), staging_bucket, key)
        zip_path.unlink(missing_ok=True)

    return key


# ── output files ──────────────────────────────────────────────────────────────

def write_deploy_script(params: dict) -> None:
    overrides = " \\\n    ".join(
        f'{k}="{v}"' for k, v in params.items()
    )
    script = f"""\
#!/usr/bin/env bash
# Generated by configure.py — do not commit (contains your API key)
# Usage: ./cfn-deploy.sh
set -euo pipefail

aws cloudformation deploy \\
  --template-file template.yaml \\
  --stack-name ai-guard-monitor \\
  --region "{params['region']}" \\
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \\
  --parameter-overrides \\
    {overrides}

echo ""
echo "Stack deployed. Check the Outputs tab in CloudFormation for next steps."
"""
    # Remove the internal 'region' key from parameter overrides
    region = params.pop("region")
    overrides = " \\\n    ".join(f'{k}="{v}"' for k, v in params.items())
    params["region"] = region  # restore

    script = f"""\
#!/usr/bin/env bash
# Generated by configure.py — do not commit (contains your API key)
# Usage: ./cfn-deploy.sh
set -euo pipefail

aws cloudformation deploy \\
  --template-file template.yaml \\
  --stack-name ai-guard-monitor \\
  --region "{region}" \\
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \\
  --parameter-overrides \\
    {overrides}

echo ""
echo "Stack deployed. Check the Outputs tab in CloudFormation for next steps."
"""
    DEPLOY_SCRIPT.write_text(script)
    DEPLOY_SCRIPT.chmod(0o755)


def write_params_json(params: dict) -> None:
    import json
    entries = [
        {"ParameterKey": k, "ParameterValue": str(v)}
        for k, v in params.items()
        if k != "region"
    ]
    PARAMS_FILE.write_text(json.dumps(entries, indent=2))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║      AI Guard S3 Monitor — Interactive Setup             ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print("\nAnswers are used to build the Lambda package, upload it to S3,")
    print("and generate cfn-deploy.sh ready to run.\n")

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("Error: boto3 is required.  Run:  pip install boto3")
        sys.exit(1)

    # ── Region ────────────────────────────────────────────────────────────────
    header("1 / 7  —  AWS Region")
    session = boto3.session.Session()
    default_region = session.region_name or "us-east-1"
    region = ask("AWS region", default=default_region)

    s3 = boto3.client("s3", region_name=region)

    # ── Staging bucket (Lambda code) ──────────────────────────────────────────
    header("2 / 7  —  Staging Bucket  (for Lambda deployment zip)")
    print("\nThe Lambda code needs to be uploaded to S3 before CloudFormation")
    print("can deploy it. Use any existing bucket you own in this region.")
    print("This is NOT the bucket being monitored — it is just for deployment.\n")

    print("Fetching your S3 buckets in this region...")
    try:
        all_buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    except (BotoCoreError, ClientError) as exc:
        print(f"\nError listing buckets: {exc}")
        sys.exit(1)

    regional = []
    for name in all_buckets:
        try:
            loc = s3.get_bucket_location(Bucket=name)
            if (loc["LocationConstraint"] or "us-east-1") == region:
                regional.append(name)
        except (BotoCoreError, ClientError):
            pass

    if regional:
        print(f"\nFound {len(regional)} bucket(s) in {region}:\n")
        staging_bucket = pick_from_list("Select staging bucket [number]", regional)
    else:
        print(f"\n  No buckets found in {region}.")
        staging_bucket = ask("Enter staging bucket name manually", validator=val_bucket)

    print(f"\n  Staging bucket: {staging_bucket}")

    # Build and upload Lambda package
    print()
    if ask_yn("Build and upload Lambda package now?", default=True):
        try:
            lambda_key = build_and_upload(staging_bucket, region, boto3)
            print(f"\n  Lambda uploaded: s3://{staging_bucket}/{lambda_key}")
        except Exception as exc:
            print(f"\n  Build failed: {exc}")
            print("  Fix the error and re-run, or run ./build.sh manually.")
            sys.exit(1)
    else:
        print("\n  Run './build.sh <bucket> [region]' to build and upload the package.")
        lambda_key = ask("Enter LambdaCodeS3Key after running build.sh",
                         default="ai-guard-monitor/lambda-YYYYMMDD-HHMMSS.zip")

    # ── Source bucket ─────────────────────────────────────────────────────────
    header("3 / 7  —  Source Bucket  (existing bucket to monitor)")
    print(f"\nFetching S3 buckets in {region}...\n")

    # Re-use the regional list, excluding the staging bucket
    monitor_candidates = [b for b in regional if b != staging_bucket]

    if monitor_candidates:
        source_bucket = pick_from_list("Select bucket to monitor [number]", monitor_candidates)
    else:
        source_bucket = ask("Enter source bucket name", validator=val_bucket)

    print(f"\n  Source bucket: {source_bucket}")

    # ── Log bucket ────────────────────────────────────────────────────────────
    header("4 / 7  —  Log Bucket  (new, will be created by the stack)")
    print()
    default_log = f"{source_bucket}-ai-guard-logs"
    log_bucket = ask("Log bucket name", default=default_log, validator=val_bucket)

    # ── AI Guard ──────────────────────────────────────────────────────────────
    header("5 / 7  —  Trend Micro AI Guard")
    print("\nCreate an API key at: Vision One Console -> Administration -> API Keys\n")
    api_key = ask("Vision One API Key", secret=True,
                  validator=lambda v: "Key seems too short." if len(v) < 10 else None)

    print("\nVision One region endpoint:")
    for k, (label, _) in ENDPOINT_OPTIONS.items():
        print(f"  {k}.  {label}")
    ep_choice = ask("\nSelect endpoint", default="1",
                    validator=lambda v: None if v in ENDPOINT_OPTIONS
                    else f"Enter 1-{len(ENDPOINT_OPTIONS)}.")
    ep_label, endpoint_url = ENDPOINT_OPTIONS[ep_choice]
    print(f"\n  Endpoint: {ep_label}")

    app_name = ask("\nApplication name", default="ai-guard-s3-monitor",
                   validator=val_app_name)

    # ── Notifications ─────────────────────────────────────────────────────────
    header("6 / 7  —  Email Notifications  (optional)")
    print("\nLeave both fields blank to skip email alerts.")
    print("Detections are always logged to S3 regardless.\n")
    notification_email = ask_optional("Alert recipient email", validator=val_email)
    ses_sender = ""
    if notification_email:
        ses_sender = ask_optional("SES verified sender email  (FROM address)",
                                  default=notification_email, validator=val_email)
        print(f"\n  After deployment, verify the sender:")
        print(f"  aws ses verify-email-identity --email-address {ses_sender} --region {region}")

    # ── Scanner settings ──────────────────────────────────────────────────────
    header("7 / 7  —  Scanner Settings  (Enter to accept defaults)")
    print()
    max_text_kb    = ask_int("Max text per file (KB, 10-2048)", 500, 10, 2048)
    lambda_memory  = ask_int("Lambda memory MB  [256/512/1024/2048]", 512, 256, 2048)
    lambda_timeout = ask_int("Lambda timeout seconds  (30-900)", 300, 30, 900)
    log_retention  = ask_int("CloudWatch log retention days", 90, 7, 365)
    print()
    enable_cw = ask_yn("Enable CloudWatch Dashboard + Alarms?", default=False)

    # ── Summary & write ───────────────────────────────────────────────────────
    header("Summary")
    rows = [
        ("Region",                 region),
        ("Staging bucket",         staging_bucket),
        ("Lambda key",             lambda_key),
        ("Source bucket",          source_bucket),
        ("Log bucket",             log_bucket),
        ("AI Guard endpoint",      ep_label),
        ("App name",               app_name),
        ("Alert recipient",        notification_email or "(disabled)"),
        ("SES sender",             ses_sender or "(disabled)"),
        ("Max text",               f"{max_text_kb} KB"),
        ("Lambda memory",          f"{lambda_memory} MB"),
        ("Lambda timeout",         f"{lambda_timeout}s"),
        ("Log retention",          f"{log_retention} days"),
        ("CloudWatch monitoring",  "Yes" if enable_cw else "No"),
    ]
    for label, value in rows:
        print(f"  {label:<26} {value}")

    print()
    if not ask_yn("Write cfn-deploy.sh and cfn-parameters.json?", default=True):
        print("\nAborted.")
        sys.exit(0)

    params = {
        "region":                    region,
        "LambdaCodeS3Bucket":        staging_bucket,
        "LambdaCodeS3Key":           lambda_key,
        "AIGuardApiKey":             api_key,
        "AIGuardEndpoint":           endpoint_url,
        "AIGuardAppName":            app_name,
        "SourceBucketName":          source_bucket,
        "LogBucketName":             log_bucket,
        "NotificationEmail":         notification_email,
        "SESVerifiedSender":         ses_sender,
        "MaxTextKB":                 str(max_text_kb),
        "LambdaMemoryMB":            str(lambda_memory),
        "LambdaTimeoutSeconds":      str(lambda_timeout),
        "LogRetentionDays":          str(log_retention),
        "EnableCloudWatchMonitoring": "Yes" if enable_cw else "No",
    }

    write_deploy_script(params)
    write_params_json(params)

    print(f"\n  cfn-deploy.sh      — run this to deploy the stack")
    print(f"  cfn-parameters.json — parameters for Console or create-stack")
    print("\nNext steps:")
    print("  ./cfn-deploy.sh")
    if notification_email and ses_sender:
        print(f"  aws ses verify-email-identity --email-address {ses_sender} --region {region}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
