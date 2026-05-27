#!/usr/bin/env python3
"""
Interactive setup for AI Guard S3 Monitor.

Handles the correct deployment order automatically:
  1. Deploy the stack (creates the Lambda deployment bucket + all infra
     EXCEPT the scanner Lambda, which needs code first)
  2. Build and upload the Lambda zip to the created bucket
  3. Update the stack to activate the scanner Lambda

Run:  python3 configure.py
"""

import getpass
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).parent / "cfn-deploy.sh"
PARAMS_FILE   = Path(__file__).parent / "cfn-parameters.json"
SRC_DIR       = Path(__file__).parent / "src"
TEMPLATE_FILE = Path(__file__).parent / "template.yaml"
STACK_NAME    = "ai-guard-monitor"

ENDPOINT_OPTIONS = {
    "1": ("US (default)", "https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "2": ("EU",           "https://api.eu.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "3": ("AU",           "https://api.au.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "4": ("JP",           "https://api.jp.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
    "5": ("SG",           "https://api.sg.xdr.trendmicro.com/v3.0/xdr/guard/scan"),
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


def cfn_stack_exists(cfn, stack_name: str) -> bool:
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
        return bool(stacks)
    except Exception:
        return False


def get_stack_output(cfn, stack_name: str, key: str) -> str | None:
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
        for output in stacks[0].get("Outputs", []):
            if output["OutputKey"] == key:
                return output["OutputValue"]
    except Exception:
        pass
    return None


def deploy_stack(params: dict, region: str, wait: bool = True) -> bool:
    """Run aws cloudformation deploy with the given parameters."""
    overrides = [f"{k}={v}" for k, v in params.items() if k != "region"]
    cmd = [
        "aws", "cloudformation", "deploy",
        "--template-file", str(TEMPLATE_FILE),
        "--stack-name", STACK_NAME,
        "--region", region,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_NAMED_IAM",
        "--parameter-overrides", *overrides,
    ]
    result = subprocess.run(cmd)
    return result.returncode == 0


# ── Lambda packaging ──────────────────────────────────────────────────────────

def build_and_upload(bucket: str, region: str, boto3) -> str:
    """Package src/ + dependencies, upload to S3, return the S3 key."""
    from datetime import datetime
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
        for f in SRC_DIR.glob("*.py"):
            shutil.copy(f, build_dir)

        print("  Creating deployment zip...")
        zip_path = Path(tempfile.mktemp(suffix=".zip"))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in Path(build_dir).rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(build_dir))

        print(f"  Uploading to s3://{bucket}/{key} ...")
        s3 = boto3.client("s3", region_name=region)
        s3.upload_file(str(zip_path), bucket, key)
        zip_path.unlink(missing_ok=True)

    return key


# ── output files ──────────────────────────────────────────────────────────────

def write_deploy_script(params: dict, region: str) -> None:
    overrides = " \\\n    ".join(f'{k}="{v}"' for k, v in params.items())
    script = f"""\
#!/usr/bin/env bash
# Generated by configure.py — do not commit (contains your API key)
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
    print("\nThis script collects your settings, deploys the stack")
    print("(which creates the Lambda deployment bucket automatically),")
    print("then builds and uploads the Lambda code in the right order.\n")

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
    header("3 / 5  —  Log Bucket  (new, will be created by the stack)")
    print()
    log_bucket = ask("Log bucket name",
                     default=f"{source_bucket}-ai-guard-logs",
                     validator=lambda v: None if BUCKET_RE.match(v)
                     else "3-63 chars, lowercase, letters/numbers/hyphens/dots.")

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
    app_name = ask("\nApplication name", default="ai-guard-s3-monitor",
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

    # ── Summary ───────────────────────────────────────────────────────────────
    header("Summary")
    rows = [
        ("Region",                 region),
        ("Source bucket",          source_bucket),
        ("Log bucket",             log_bucket),
        ("Lambda deploy bucket",   "(auto-created by the stack)"),
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
        print(f"  {label:<28} {value}")

    print()
    if not ask_yn("Proceed with deployment?", default=True):
        print("\nAborted.")
        sys.exit(0)

    # ── Phase 1: Deploy stack WITHOUT LambdaCodeS3Key (creates the bucket) ──

    print("\n" + "=" * 60)
    print("  Phase 1 — Deploying infrastructure (creates Lambda bucket)")
    print("=" * 60)

    # We need a placeholder key for the first deploy so that CFN accepts the
    # parameter. The Lambda will fail to create (key doesn't exist yet) but
    # we suppress that and handle it in Phase 2.
    # A cleaner approach: deploy with a real key immediately after creating the
    # bucket by doing a two-pass deploy.

    phase1_params = {
        "LambdaCodeS3Key":             "placeholder/not-uploaded-yet",
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

    # First deploy intentionally uses --no-fail-on-empty-changeset
    # It will error when Lambda can't find the placeholder key, but we catch that.
    print("\n  Deploying stack to create the Lambda deployment bucket...")
    deploy_result = subprocess.run([
        "aws", "cloudformation", "deploy",
        "--template-file", str(TEMPLATE_FILE),
        "--stack-name", STACK_NAME,
        "--region", region,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_NAMED_IAM",
        "--parameter-overrides",
        *[f"{k}={v}" for k, v in phase1_params.items()],
        "--no-fail-on-empty-changeset",
    ], capture_output=True, text=True)

    # Get the bucket name regardless of whether the deploy fully succeeded
    # (it may have failed only on Lambda creation, but the bucket was created)
    deploy_bucket = None
    for attempt in range(10):
        deploy_bucket = get_stack_output(cfn, STACK_NAME, "LambdaDeployBucketName")
        if deploy_bucket:
            break
        print(f"  Waiting for bucket output... ({attempt + 1}/10)")
        time.sleep(6)

    if not deploy_bucket:
        print("\n  ERROR: Could not retrieve LambdaDeployBucketName from stack.")
        print("  Check the CloudFormation console for errors.")
        sys.exit(1)

    print(f"\n  Lambda deployment bucket: {deploy_bucket}")

    # ── Phase 2: Build Lambda, upload to the created bucket ──────────────────

    print("\n" + "=" * 60)
    print("  Phase 2 — Building and uploading Lambda code")
    print("=" * 60)

    lambda_key = build_and_upload(deploy_bucket, region, boto3)
    print(f"\n  Lambda uploaded: s3://{deploy_bucket}/{lambda_key}")

    # ── Phase 3: Re-deploy with real key ─────────────────────────────────────

    print("\n" + "=" * 60)
    print("  Phase 3 — Updating stack with Lambda code")
    print("=" * 60)

    phase3_params = {**phase1_params, "LambdaCodeS3Key": lambda_key}

    # Write deploy script with the real key (useful for future redeployments)
    write_deploy_script(phase3_params, region)
    write_params_json(phase3_params)

    deploy_ok = deploy_stack(phase3_params, region)

    if deploy_ok:
        print("\n  Stack deployed successfully!")
        if notification_email and ses_sender:
            print(f"\n  ACTION REQUIRED — verify the SES sender email:")
            print(f"  aws ses verify-email-identity --email-address {ses_sender} --region {region}")
        print(f"\n  cfn-deploy.sh saved for future redeployments.")
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
