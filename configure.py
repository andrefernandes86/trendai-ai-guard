#!/usr/bin/env python3
"""
Interactive setup for AI Guard S3 Monitor.

Queries your AWS account for real S3 buckets, lets you pick the one to monitor,
collects the remaining parameters, and writes samconfig.toml ready to deploy.

Usage:
    python configure.py
"""

import re
import sys
from pathlib import Path

SAMCONFIG_PATH = Path(__file__).parent / "samconfig.toml"

ENDPOINT_OPTIONS = {
    "1": ("US (default)", "https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "2": ("EU",           "https://api.eu.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "3": ("AU",           "https://api.au.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "4": ("JP",           "https://api.jp.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "5": ("SG",           "https://api.sg.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
}

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


# ── helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "", validator=None) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{hint}: ").strip()
        value = raw or default
        if not value:
            print("  ✗ Required — please enter a value.")
            continue
        if validator:
            err = validator(value)
            if err:
                print(f"  ✗ {err}")
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
        print(f"  ✗ Enter a number between {lo} and {hi}.")


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
        print("  ✗ Please type y or n.")


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
        print(f"  ✗ Enter a number between 1 and {len(items)}.")


def validate_email(v: str):
    if not EMAIL_RE.match(v):
        return "Not a valid email address."


def validate_bucket_name(v: str):
    if not re.match(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$", v):
        return ("Bucket names must be 3–63 chars, lowercase letters, numbers, "
                "hyphens, and dots only.")


def validate_app_name(v: str):
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", v):
        return "Only letters, numbers, hyphens, and underscores (max 64 chars)."


def header(text: str) -> None:
    width = 60
    print(f"\n{'─' * width}")
    print(f"  {text}")
    print(f"{'─' * width}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║      AI Guard S3 Monitor — Interactive Setup             ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print("\nThis script will query your AWS account, let you pick the")
    print("S3 bucket to monitor, and generate samconfig.toml.\n")

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("Error: boto3 is required.  Run:  pip install boto3")
        sys.exit(1)

    # ── Region ────────────────────────────────────────────────────────────────
    header("1 / 6  —  AWS Region")
    session = boto3.session.Session()
    default_region = session.region_name or "us-east-1"
    region = ask("AWS region", default=default_region)

    # ── Source bucket ─────────────────────────────────────────────────────────
    header("2 / 6  —  Source S3 Bucket  (existing, to monitor)")

    s3 = boto3.client("s3", region_name=region)
    print(f"\nQuerying S3 buckets in {region} …")

    try:
        all_buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    except (BotoCoreError, ClientError) as exc:
        print(f"\nError listing buckets: {exc}")
        print("Make sure your AWS credentials are configured and have s3:ListAllMyBuckets.")
        sys.exit(1)

    # Filter to the target region
    regional = []
    skipped = 0
    for name in all_buckets:
        try:
            loc = s3.get_bucket_location(Bucket=name)
            bucket_region = loc["LocationConstraint"] or "us-east-1"
            if bucket_region == region:
                regional.append(name)
        except (BotoCoreError, ClientError):
            skipped += 1

    if not regional:
        print(f"\n  No S3 buckets found in {region}.")
        if skipped:
            print(f"  ({skipped} bucket(s) could not be queried — check your permissions.)")
        print("\n  Create a source bucket first, then re-run this script.")
        sys.exit(1)

    print(f"\nFound {len(regional)} bucket(s) in {region}:\n")
    source_bucket = pick_from_list("Select bucket to monitor [number]", regional)
    print(f"\n  ✓ Source bucket: {source_bucket}")

    # ── Log bucket ────────────────────────────────────────────────────────────
    header("3 / 6  —  Log S3 Bucket  (new, will be created)")
    print("\nThis bucket will be created by the stack to store detection logs.")
    print("The name must be globally unique across all AWS accounts.\n")

    default_log = f"{source_bucket}-ai-guard-logs"
    log_bucket = ask("Log bucket name", default=default_log,
                     validator=validate_bucket_name)

    # Warn if it already exists in this account
    try:
        s3.head_bucket(Bucket=log_bucket)
        print(f"\n  ⚠  Bucket '{log_bucket}' already exists in your account.")
        print("  The stack will adopt it if it has no conflicting configuration.")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            print(f"\n  ✓ '{log_bucket}' is available.")
        elif code == "403":
            print(f"\n  ⚠  '{log_bucket}' exists but belongs to another account — choose a different name.")

    # ── AI Guard ──────────────────────────────────────────────────────────────
    header("4 / 6  —  Trend Micro AI Guard")

    print("\nAPI Key")
    print("  Create one at: Vision One Console → Administration → API Keys")
    print("  (input is hidden)\n")
    import getpass
    api_key = ""
    while not api_key or len(api_key) < 10:
        api_key = getpass.getpass("  Vision One API Key: ").strip()
        if len(api_key) < 10:
            print("  ✗ Key seems too short — please paste the full key.")

    print("\nVision One Region / Endpoint")
    for k, (label, _) in ENDPOINT_OPTIONS.items():
        print(f"  {k}.  {label}")
    endpoint_choice = ask("\nSelect endpoint", default="1",
                          validator=lambda v: None if v in ENDPOINT_OPTIONS else
                          f"Enter a number 1–{len(ENDPOINT_OPTIONS)}.")
    endpoint_label, endpoint_url = ENDPOINT_OPTIONS[endpoint_choice]
    print(f"\n  ✓ Endpoint: {endpoint_label}  ({endpoint_url})")

    app_name = ask("\nApplication name  (TMV1-Application-Name header)",
                   default="ai-guard-s3-monitor", validator=validate_app_name)

    # ── Notifications ─────────────────────────────────────────────────────────
    header("5 / 6  —  Email Notifications  (Amazon SES)")
    print()
    notification_email = ask("Alert recipient email", default="af.us@outlook.com",
                              validator=validate_email)
    ses_sender = ask("SES verified sender email  (FROM address)",
                     validator=validate_email)
    print("\n  ℹ  After deployment, verify the sender address with:")
    print(f"     aws ses verify-email-identity --email-address {ses_sender} --region {region}")

    # ── Scanner tuning ────────────────────────────────────────────────────────
    header("6 / 6  —  Scanner Settings  (press Enter to accept defaults)")
    print()
    max_text_kb     = ask_int("Max text to scan per file (KB, 10–2048)", 500, 10, 2048)
    lambda_memory   = ask_int("Lambda memory MB  [256/512/1024/2048]",   512, 256, 2048)
    lambda_timeout  = ask_int("Lambda timeout seconds  (30–900)",        300, 30, 900)
    log_retention   = ask_int("CloudWatch log retention days  [7/14/30/60/90/180/365]", 90, 7, 365)

    print()
    enable_cw = ask_yn("Enable CloudWatch Dashboard + Alarms?", default=False)

    # ── Write samconfig.toml ──────────────────────────────────────────────────
    header("Summary")
    rows = [
        ("Region",               region),
        ("Source bucket",        source_bucket),
        ("Log bucket",           log_bucket),
        ("AI Guard endpoint",    endpoint_label),
        ("App name",             app_name),
        ("Alert recipient",      notification_email),
        ("SES sender",           ses_sender),
        ("Max text",             f"{max_text_kb} KB"),
        ("Lambda memory",        f"{lambda_memory} MB"),
        ("Lambda timeout",       f"{lambda_timeout}s"),
        ("Log retention",        f"{log_retention} days"),
        ("CloudWatch monitoring",  "Yes" if enable_cw else "No"),
    ]
    for label, value in rows:
        print(f"  {label:<26} {value}")

    print()
    if not ask_yn("Write samconfig.toml and proceed?", default=True):
        print("\nAborted — nothing written.")
        sys.exit(0)

    samconfig = f"""\
# Generated by configure.py — do not commit this file (it contains your API key)
version = 0.1

[default.build.parameters]
cached   = true
parallel = true

[default.deploy.parameters]
stack_name        = "ai-guard-monitor"
region            = "{region}"
confirm_changeset = true
capabilities      = "CAPABILITY_IAM CAPABILITY_NAMED_IAM"
resolve_s3        = true

parameter_overrides = [
  "AIGuardApiKey={api_key}",
  "AIGuardEndpoint={endpoint_url}",
  "AIGuardAppName={app_name}",
  "SourceBucketName={source_bucket}",
  "LogBucketName={log_bucket}",
  "NotificationEmail={notification_email}",
  "SESVerifiedSender={ses_sender}",
  "MaxTextKB={max_text_kb}",
  "LambdaMemoryMB={lambda_memory}",
  "LambdaTimeoutSeconds={lambda_timeout}",
  "LogRetentionDays={log_retention}",
  "EnableCloudWatchMonitoring={'Yes' if enable_cw else 'No'}",
]
"""

    SAMCONFIG_PATH.write_text(samconfig)
    print(f"\n  ✓ Written to {SAMCONFIG_PATH.name}")
    print("\nNext steps:")
    print("  1.  sam build --use-container")
    print("  2.  sam deploy")
    print(f"  3.  aws ses verify-email-identity --email-address {ses_sender} --region {region}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
