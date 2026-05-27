#!/usr/bin/env bash
#
# AI Guard S3 Monitor - one-shot interactive installer.
#
# Asks for all required parameters, creates the supporting S3 buckets,
# builds and uploads the Lambda package, then deploys the CloudFormation
# stack. Does the same work as 'python3 configure.py' but using only the
# AWS CLI (boto3 not required).
#
# Requirements:
#   - aws CLI v2 (authenticated: 'aws sts get-caller-identity' must work)
#   - python3 + pip3 + zip (needed only to build the Lambda package)
#
# Usage:
#   ./install.sh

set -euo pipefail

# ── Constants ────────────────────────────────────────────────────────────────

STACK_NAME="ai-guard-monitor"
LAMBDA_S3_KEY="lambda/package.zip"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/template.yaml"
SRC_DIR="${SCRIPT_DIR}/src"

# Terminal colors (degrade gracefully if not a TTY)
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    BOLD=$(tput bold);    RESET=$(tput sgr0)
    RED=$(tput setaf 1);  GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3); BLUE=$(tput setaf 4)
else
    BOLD=""; RESET=""; RED=""; GREEN=""; YELLOW=""; BLUE=""
fi

# ── Output helpers ───────────────────────────────────────────────────────────

say()    { printf '%s\n' "$*"; }
header() { printf '\n%s%s== %s ==%s\n' "$BOLD" "$BLUE" "$*" "$RESET"; }
ok()     { printf '%s[OK]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()   { printf '%s[!]%s  %s\n' "$YELLOW" "$RESET" "$*"; }
err()    { printf '%s[X]%s  %s\n' "$RED" "$RESET" "$*" >&2; }

# ── Prompt helpers ───────────────────────────────────────────────────────────

# ask "Prompt" [default] -> echoes response (default if user hits Enter)
ask() {
    local prompt="$1" default="${2-}" reply
    if [[ -n "$default" ]]; then
        read -rp "  ${prompt} [${default}]: " reply
        printf '%s\n' "${reply:-$default}"
    else
        read -rp "  ${prompt}: " reply
        printf '%s\n' "$reply"
    fi
}

ask_secret() {
    local prompt="$1" reply
    read -rsp "  ${prompt}: " reply
    printf '\n' >&2
    printf '%s\n' "$reply"
}

# ask_yn "Prompt" [Y|N default]  -> exit 0 for yes, 1 for no
ask_yn() {
    local prompt="$1" default="${2:-N}" reply hint
    if [[ "$default" == "Y" ]]; then hint="Y/n"; else hint="y/N"; fi
    while true; do
        read -rp "  ${prompt} [${hint}]: " reply
        reply="${reply:-$default}"
        case "$reply" in
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo])     return 1 ;;
            *) err "Please answer y or n." ;;
        esac
    done
}

valid_bucket()  { [[ "$1" =~ ^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$ ]]; }
valid_email()   { [[ "$1" =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; }
is_positive_int() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }

# ── Prereq checks ────────────────────────────────────────────────────────────

check_prereqs() {
    local missing=()
    for cmd in aws python3 pip3 zip; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Missing required commands: ${missing[*]}"
        err "Install them and re-run this script."
        exit 1
    fi
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        err "AWS CLI is not authenticated. Run 'aws configure' first."
        exit 1
    fi
    [[ -f "$TEMPLATE_FILE" ]] || { err "template.yaml not found in ${SCRIPT_DIR}"; exit 1; }
    [[ -d "$SRC_DIR" ]]       || { err "src/ directory not found in ${SCRIPT_DIR}"; exit 1; }
}

# ── Bucket helpers (idempotent) ──────────────────────────────────────────────

bucket_exists() {
    aws s3api head-bucket --bucket "$1" --region "$2" 2>/dev/null
}

create_bucket() {
    local name="$1" region="$2"
    if [[ "$region" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$name" --region "$region" >/dev/null
    else
        aws s3api create-bucket --bucket "$name" --region "$region" \
            --create-bucket-configuration LocationConstraint="$region" >/dev/null
    fi
}

apply_secure_defaults() {
    local name="$1"
    aws s3api put-public-access-block --bucket "$name" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,BlockPublicPolicy=true,IgnorePublicAcls=true,RestrictPublicBuckets=true" >/dev/null
    aws s3api put-bucket-encryption --bucket "$name" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
}

apply_log_lifecycle() {
    local name="$1"
    aws s3api put-bucket-versioning --bucket "$name" \
        --versioning-configuration "Status=Enabled" >/dev/null
    aws s3api put-bucket-lifecycle-configuration --bucket "$name" \
        --lifecycle-configuration '{
            "Rules": [
                {"ID":"ArchiveToGlacier","Status":"Enabled","Filter":{"Prefix":"logs/"},"Transitions":[{"Days":90,"StorageClass":"GLACIER"}]},
                {"ID":"ExpireAfter7Years","Status":"Enabled","Filter":{"Prefix":"logs/"},"Expiration":{"Days":2555}},
                {"ID":"CleanUpOldVersions","Status":"Enabled","Filter":{"Prefix":"logs/"},"NoncurrentVersionExpiration":{"NoncurrentDays":30}}
            ]
        }' >/dev/null
}

# ── Main script ──────────────────────────────────────────────────────────────

printf '%s\n' "${BOLD}+==========================================================+${RESET}"
printf '%s\n' "${BOLD}|     AI Guard S3 Monitor  -  Interactive Installer        |${RESET}"
printf '%s\n' "${BOLD}+==========================================================+${RESET}"

check_prereqs

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
say "AWS Account: ${BOLD}${ACCOUNT_ID}${RESET}"

# ── 1/5  Region ──────────────────────────────────────────────────────────────
header "1/5  AWS Region"

DEFAULT_REGION=$(aws configure get region 2>/dev/null || true)
DEFAULT_REGION="${DEFAULT_REGION:-us-east-1}"
REGION=$(ask "AWS region" "$DEFAULT_REGION")

# ── 2/5  Source bucket ───────────────────────────────────────────────────────
header "2/5  Source S3 Bucket (existing bucket to monitor)"

say "Fetching buckets in ${REGION}..."

BUCKETS=()
while IFS= read -r b; do
    [[ -z "$b" ]] && continue
    loc=$(aws s3api get-bucket-location --bucket "$b" \
            --query LocationConstraint --output text 2>/dev/null || true)
    [[ "$loc" == "None" || -z "$loc" ]] && loc="us-east-1"
    [[ "$loc" == "$REGION" ]] && BUCKETS+=("$b")
done < <(aws s3api list-buckets --query "Buckets[].Name" --output text 2>/dev/null | tr '\t' '\n')

if [[ ${#BUCKETS[@]} -eq 0 ]]; then
    warn "No buckets found in ${REGION}."
    while :; do
        SOURCE_BUCKET=$(ask "Enter bucket name manually")
        valid_bucket "$SOURCE_BUCKET" && break
        err "Invalid bucket name (3-63 chars, lowercase letters/digits/dots/hyphens)."
    done
else
    say ""
    i=1
    for b in "${BUCKETS[@]}"; do
        printf "    %3d. %s\n" "$i" "$b"
        i=$((i+1))
    done
    say ""
    while :; do
        idx=$(ask "Select bucket [number]")
        if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx >= 1 && idx <= ${#BUCKETS[@]} )); then
            SOURCE_BUCKET="${BUCKETS[$((idx-1))]}"
            break
        fi
        err "Enter a number between 1 and ${#BUCKETS[@]}."
    done
fi
say "Source bucket: ${BOLD}${SOURCE_BUCKET}${RESET}"

# ── 3/5  Log bucket ──────────────────────────────────────────────────────────
header "3/5  Log Bucket"

while :; do
    LOG_BUCKET=$(ask "Log bucket name" "${SOURCE_BUCKET}-ai-guard-logs")
    valid_bucket "$LOG_BUCKET" && break
    err "3-63 chars, lowercase, alphanumeric start/end, dots/hyphens OK."
done

# ── 4/5  AI Guard ────────────────────────────────────────────────────────────
header "4/5  Trend Micro AI Guard"

say "Get your API key from: Vision One Console -> Administration -> API Keys"
while :; do
    API_KEY=$(ask_secret "Vision One API Key")
    [[ ${#API_KEY} -ge 10 ]] && break
    err "Key too short (need at least 10 chars)."
done

say ""
say "Endpoint region:"
say "    1. US (default)"
say "    2. EU"
say "    3. AU"
say "    4. JP"
say "    5. SG"
EP_CHOICE=$(ask "Select endpoint" "1")
case "$EP_CHOICE" in
    1) ENDPOINT="https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan" ;;
    2) ENDPOINT="https://api.eu.xdr.trendmicro.com/v3.0/xdr/guard/scan" ;;
    3) ENDPOINT="https://api.au.xdr.trendmicro.com/v3.0/xdr/guard/scan" ;;
    4) ENDPOINT="https://api.jp.xdr.trendmicro.com/v3.0/xdr/guard/scan" ;;
    5) ENDPOINT="https://api.sg.xdr.trendmicro.com/v3.0/xdr/guard/scan" ;;
    *) err "Invalid choice, defaulting to US."
       ENDPOINT="https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan" ;;
esac

APP_NAME=$(ask "Application name" "ai-guard-s3-monitor")

# ── 5/5  Settings ────────────────────────────────────────────────────────────
header "5/5  Notifications & Tuning"

NOTIFICATION_EMAIL=$(ask "Alert recipient email (blank to disable)" "")
SES_SENDER=""
if [[ -n "$NOTIFICATION_EMAIL" ]]; then
    if ! valid_email "$NOTIFICATION_EMAIL"; then
        err "Invalid email format."; exit 1
    fi
    SES_SENDER=$(ask "SES verified sender (FROM address)" "$NOTIFICATION_EMAIL")
    valid_email "$SES_SENDER" || { err "Invalid SES sender email."; exit 1; }
fi

MAX_TEXT_KB=$(ask "Max text per file (KB, 10-2048)" "500")
LAMBDA_MEMORY=$(ask "Lambda memory MB [256/512/1024/2048]" "512")
LAMBDA_TIMEOUT=$(ask "Lambda timeout seconds (30-900)" "300")
LOG_RETENTION=$(ask "CloudWatch log retention days" "90")

if ask_yn "Enable CloudWatch Dashboard + Alarms?" "N"; then
    ENABLE_CW="Yes"
else
    ENABLE_CW="No"
fi

# Derived
DEPLOY_BUCKET="${STACK_NAME}-deploy-${ACCOUNT_ID}"

# ── Summary ──────────────────────────────────────────────────────────────────
header "Summary"

printf '  %-26s %s\n'  "Region"                  "$REGION"
printf '  %-26s %s\n'  "Source bucket"           "$SOURCE_BUCKET"
printf '  %-26s %s\n'  "Log bucket"              "$LOG_BUCKET"
printf '  %-26s %s\n'  "Lambda deploy bucket"    "$DEPLOY_BUCKET"
printf '  %-26s %s\n'  "AI Guard endpoint"       "$ENDPOINT"
printf '  %-26s %s\n'  "Application name"        "$APP_NAME"
printf '  %-26s %s\n'  "Alert recipient"         "${NOTIFICATION_EMAIL:-(disabled)}"
printf '  %-26s %s\n'  "SES sender"              "${SES_SENDER:-(disabled)}"
printf '  %-26s %s KB\n' "Max text"              "$MAX_TEXT_KB"
printf '  %-26s %s MB\n' "Lambda memory"         "$LAMBDA_MEMORY"
printf '  %-26s %s s\n'  "Lambda timeout"        "$LAMBDA_TIMEOUT"
printf '  %-26s %s days\n' "Log retention"       "$LOG_RETENTION"
printf '  %-26s %s\n'  "CloudWatch monitoring"   "$ENABLE_CW"

say ""
ask_yn "Proceed with deployment?" "Y" || { say "Aborted."; exit 0; }

# ── Step 1: Deploy bucket ────────────────────────────────────────────────────
header "Step 1/4  Create Lambda deployment bucket"

if bucket_exists "$DEPLOY_BUCKET" "$REGION"; then
    ok "Deploy bucket already exists: $DEPLOY_BUCKET"
else
    say "Creating $DEPLOY_BUCKET..."
    create_bucket "$DEPLOY_BUCKET" "$REGION"
    apply_secure_defaults "$DEPLOY_BUCKET"
    ok "Created deploy bucket: $DEPLOY_BUCKET"
fi

# ── Step 2: Build and upload Lambda code ─────────────────────────────────────
header "Step 2/4  Build and upload Lambda code"

BUILD_DIR=$(mktemp -d)
ZIP_FILE=$(mktemp).zip
trap 'rm -rf "$BUILD_DIR" "$ZIP_FILE" 2>/dev/null || true' EXIT

say "Installing Python dependencies..."
pip3 install -r "${SRC_DIR}/requirements.txt" -t "$BUILD_DIR" -q

say "Copying source files..."
cp "${SRC_DIR}"/*.py "$BUILD_DIR/"

say "Creating zip..."
( cd "$BUILD_DIR" && zip -rq "$ZIP_FILE" . )

say "Uploading to s3://${DEPLOY_BUCKET}/${LAMBDA_S3_KEY} ..."
aws s3 cp "$ZIP_FILE" "s3://${DEPLOY_BUCKET}/${LAMBDA_S3_KEY}" --region "$REGION"
ok "Lambda code uploaded"

# ── Step 3: Log bucket ───────────────────────────────────────────────────────
header "Step 3/4  Prepare log bucket"

if bucket_exists "$LOG_BUCKET" "$REGION"; then
    warn "Log bucket already exists, using as-is: $LOG_BUCKET"
    warn "(Existing settings preserved - no lifecycle rules applied)"
else
    say "Creating $LOG_BUCKET..."
    create_bucket "$LOG_BUCKET" "$REGION"
    apply_secure_defaults "$LOG_BUCKET"
    apply_log_lifecycle    "$LOG_BUCKET"
    ok "Created log bucket: $LOG_BUCKET"
    ok "Applied: encryption, versioning, lifecycle (90d -> Glacier, 7y expire)"
fi

# ── Step 4: Deploy CloudFormation stack ──────────────────────────────────────
header "Step 4/4  Deploy CloudFormation stack"

# Save a re-runnable deploy script (without the API key) for future config changes
DEPLOY_SCRIPT="${SCRIPT_DIR}/cfn-deploy.sh"
cat > "$DEPLOY_SCRIPT" <<EOF
#!/usr/bin/env bash
# Generated by install.sh - re-run to update stack configuration.
# To update Lambda code, run: make build
set -euo pipefail
aws cloudformation deploy \\
  --template-file template.yaml \\
  --stack-name ${STACK_NAME} \\
  --region "${REGION}" \\
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \\
  --parameter-overrides \\
    AIGuardApiKey="${API_KEY}" \\
    AIGuardEndpoint="${ENDPOINT}" \\
    AIGuardAppName="${APP_NAME}" \\
    SourceBucketName="${SOURCE_BUCKET}" \\
    LogBucketName="${LOG_BUCKET}" \\
    NotificationEmail="${NOTIFICATION_EMAIL}" \\
    SESVerifiedSender="${SES_SENDER}" \\
    MaxTextKB="${MAX_TEXT_KB}" \\
    LambdaMemoryMB="${LAMBDA_MEMORY}" \\
    LambdaTimeoutSeconds="${LAMBDA_TIMEOUT}" \\
    LogRetentionDays="${LOG_RETENTION}" \\
    EnableCloudWatchMonitoring="${ENABLE_CW}"
echo "Stack deployed."
EOF
chmod 755 "$DEPLOY_SCRIPT"

aws cloudformation deploy \
    --template-file "$TEMPLATE_FILE" \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
    --parameter-overrides \
        AIGuardApiKey="$API_KEY" \
        AIGuardEndpoint="$ENDPOINT" \
        AIGuardAppName="$APP_NAME" \
        SourceBucketName="$SOURCE_BUCKET" \
        LogBucketName="$LOG_BUCKET" \
        NotificationEmail="$NOTIFICATION_EMAIL" \
        SESVerifiedSender="$SES_SENDER" \
        MaxTextKB="$MAX_TEXT_KB" \
        LambdaMemoryMB="$LAMBDA_MEMORY" \
        LambdaTimeoutSeconds="$LAMBDA_TIMEOUT" \
        LogRetentionDays="$LOG_RETENTION" \
        EnableCloudWatchMonitoring="$ENABLE_CW" \
    --no-fail-on-empty-changeset

# ── Done ─────────────────────────────────────────────────────────────────────

printf '\n%s\n' "${BOLD}${GREEN}+======================================+${RESET}"
printf '%s\n'   "${BOLD}${GREEN}|      Deployment Complete             |${RESET}"
printf '%s\n\n' "${BOLD}${GREEN}+======================================+${RESET}"

if [[ -n "$NOTIFICATION_EMAIL" ]]; then
    warn "ACTION REQUIRED - verify the SES sender email so alerts can be delivered:"
    say  "  aws ses verify-email-identity --email-address ${SES_SENDER} --region ${REGION}"
    say  ""
fi

say "Stack outputs:"
say "  aws cloudformation describe-stacks --stack-name ${STACK_NAME} \\"
say "    --region ${REGION} --query 'Stacks[0].Outputs' --output table"
say ""
say "Tail the scanner logs:"
say "  aws logs tail /aws/lambda/${STACK_NAME}-scanner --region ${REGION} --follow"
say ""
say "Test it - upload a document to s3://${SOURCE_BUCKET}/ and watch the logs."
say ""
say "Re-deploy to change config: ./cfn-deploy.sh"
say "Update Lambda code:         make build"
say ""
