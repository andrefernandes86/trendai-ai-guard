#!/usr/bin/env bash
#
# AI Guard S3 Monitor - one-shot interactive uninstaller.
#
# Removes everything the installer created, with a confirmation prompt
# before each destructive step:
#
#   1. CloudFormation stack (Lambda, IAM, alarms, dashboard, and the S3
#      event notification on the source bucket - cleaned up by the
#      stack's helper custom resource on Delete).
#   2. Lambda deployment bucket  '<stack>-deploy-<account-id>'.
#   3. (Optional) Log bucket - protected by an extra confirmation
#      because it contains the detection history.
#   4. (Optional) SES verified sender identity.
#   5. (Optional) Local helper files (cfn-deploy.sh, cfn-parameters.json).
#
# Requirements:
#   - aws CLI v2 authenticated against the same account that ran install.sh.
#
# Usage:
#   ./uninstall.sh                # uses defaults: ai-guard-monitor / us-east-1
#   ./uninstall.sh my-stack us-east-2
#

set -euo pipefail

# ── Constants and arguments ──────────────────────────────────────────────────

STACK_NAME="${1:-ai-guard-monitor}"
REGION="${2:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
skip()   { printf '%s[-]%s  %s\n' "$YELLOW" "$RESET" "$*"; }

# ── Prompt helpers ───────────────────────────────────────────────────────────

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

# ── Prereqs ──────────────────────────────────────────────────────────────────

check_prereqs() {
    command -v aws >/dev/null 2>&1 || { err "aws CLI not found in PATH."; exit 1; }
    aws sts get-caller-identity >/dev/null 2>&1 || {
        err "AWS CLI is not authenticated. Run 'aws configure' first."
        exit 1
    }
}

# ── S3 helpers ───────────────────────────────────────────────────────────────

bucket_exists() {
    aws s3api head-bucket --bucket "$1" --region "$2" >/dev/null 2>&1
}

bucket_is_versioned() {
    local status
    status=$(aws s3api get-bucket-versioning --bucket "$1" --region "$2" \
        --query 'Status' --output text 2>/dev/null || echo "")
    [[ "$status" == "Enabled" || "$status" == "Suspended" ]]
}

# Empties a versioned bucket (deletes every version and delete-marker).
# Loops in batches of 1000 (the S3 list / delete batch limit) until empty.
empty_versioned_bucket() {
    local bucket="$1" region="$2"
    local total=0

    while :; do
        local versions markers
        versions=$(aws s3api list-object-versions --bucket "$bucket" --region "$region" \
            --max-items 1000 \
            --query 'Versions[].{Key:Key,VersionId:VersionId}' \
            --output json 2>/dev/null || echo "null")
        markers=$(aws s3api list-object-versions --bucket "$bucket" --region "$region" \
            --max-items 1000 \
            --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' \
            --output json 2>/dev/null || echo "null")

        local have_versions=0 have_markers=0
        [[ "$versions" != "null" && "$versions" != "[]" ]] && have_versions=1
        [[ "$markers"  != "null" && "$markers"  != "[]" ]] && have_markers=1

        if [[ $have_versions -eq 0 && $have_markers -eq 0 ]]; then
            break
        fi

        if [[ $have_versions -eq 1 ]]; then
            local batch_file
            batch_file=$(mktemp)
            printf '{"Objects": %s, "Quiet": true}' "$versions" > "$batch_file"
            aws s3api delete-objects --bucket "$bucket" --region "$region" \
                --delete "file://${batch_file}" >/dev/null
            rm -f "$batch_file"
        fi
        if [[ $have_markers -eq 1 ]]; then
            local batch_file
            batch_file=$(mktemp)
            printf '{"Objects": %s, "Quiet": true}' "$markers" > "$batch_file"
            aws s3api delete-objects --bucket "$bucket" --region "$region" \
                --delete "file://${batch_file}" >/dev/null
            rm -f "$batch_file"
        fi

        total=$((total + 1))
        printf "    cleared batch %d\n" "$total"
    done
}

# Empty + delete a bucket regardless of whether it has versioning on.
remove_bucket() {
    local bucket="$1" region="$2"

    if ! bucket_exists "$bucket" "$region"; then
        skip "Bucket ${bucket} already gone."
        return 0
    fi

    if bucket_is_versioned "$bucket" "$region"; then
        say "  Bucket has versioning - clearing all versions..."
        empty_versioned_bucket "$bucket" "$region"
    else
        say "  Removing all objects from ${bucket}..."
        aws s3 rm "s3://${bucket}" --recursive --region "$region" --only-show-errors
    fi

    aws s3api delete-bucket --bucket "$bucket" --region "$region"
    ok "Removed bucket: ${bucket}"
}

# ── Main ─────────────────────────────────────────────────────────────────────

printf '%s\n' "${BOLD}+==========================================================+${RESET}"
printf '%s\n' "${BOLD}|     AI Guard S3 Monitor  -  Interactive Uninstaller      |${RESET}"
printf '%s\n' "${BOLD}+==========================================================+${RESET}"

check_prereqs

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
DEPLOY_BUCKET="${STACK_NAME}-deploy-${ACCOUNT_ID}"

say ""
say "AWS account     : ${BOLD}${ACCOUNT_ID}${RESET}"
say "Region          : ${BOLD}${REGION}${RESET}"
say "Stack name      : ${BOLD}${STACK_NAME}${RESET}"
say "Deploy bucket   : ${BOLD}${DEPLOY_BUCKET}${RESET}"

# ── Discovery ────────────────────────────────────────────────────────────────
header "Discovery"

STACK_EXISTS=0
LOG_BUCKET=""
SOURCE_BUCKET=""
SES_SENDER=""

if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    STACK_EXISTS=1
    ok "Stack '${STACK_NAME}' found"

    # Pull the parameters so we know which buckets / SES sender to clean up
    LOG_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query "Stacks[0].Parameters[?ParameterKey=='LogBucketName'].ParameterValue" \
        --output text 2>/dev/null || echo "")
    SOURCE_BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query "Stacks[0].Parameters[?ParameterKey=='SourceBucketName'].ParameterValue" \
        --output text 2>/dev/null || echo "")
    SES_SENDER=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
        --query "Stacks[0].Parameters[?ParameterKey=='SESVerifiedSender'].ParameterValue" \
        --output text 2>/dev/null || echo "")

    [[ -n "$SOURCE_BUCKET" ]] && say "  Source bucket   : ${SOURCE_BUCKET}"
    [[ -n "$LOG_BUCKET"    ]] && say "  Log bucket      : ${LOG_BUCKET}"
    [[ -n "$SES_SENDER"    ]] && say "  SES sender      : ${SES_SENDER}"
else
    warn "Stack '${STACK_NAME}' not found in ${REGION}."
    say  "  (We'll still try to clean up the deploy bucket and local files.)"
fi

if bucket_exists "$DEPLOY_BUCKET" "$REGION"; then
    ok "Deploy bucket '${DEPLOY_BUCKET}' found"
else
    skip "Deploy bucket '${DEPLOY_BUCKET}' not found - nothing to remove there."
fi

# ── Plan summary ─────────────────────────────────────────────────────────────
header "Plan"

say "  The following will happen (each step prompts separately):"
say "    1. Delete the CloudFormation stack '${STACK_NAME}'"
say "         -> also removes the S3 event notification on ${SOURCE_BUCKET:-the source bucket}"
say "    2. Delete the Lambda deploy bucket '${DEPLOY_BUCKET}'"
if [[ -n "$LOG_BUCKET" ]]; then
    say "    3. (optional) Delete the log bucket '${LOG_BUCKET}'  ${YELLOW}<- contains detection history${RESET}"
fi
if [[ -n "$SES_SENDER" ]]; then
    say "    4. (optional) Remove SES verified sender '${SES_SENDER}'"
fi
say  "    5. (optional) Remove local cfn-deploy.sh / cfn-parameters.json"
say ""

ask_yn "Continue?" "N" || { say "Aborted."; exit 0; }

# ── Step 1: Delete the CloudFormation stack ──────────────────────────────────
header "Step 1/5  CloudFormation stack"

if [[ $STACK_EXISTS -eq 1 ]]; then
    if ask_yn "Delete stack '${STACK_NAME}'?" "Y"; then
        say "  aws cloudformation delete-stack --stack-name ${STACK_NAME} --region ${REGION}"
        aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
        say "  Waiting for delete to complete (this can take a few minutes)..."
        if aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>&1; then
            ok "Stack deleted."
        else
            err "Stack delete did not complete cleanly. Inspect the events:"
            err "  aws cloudformation describe-stack-events --stack-name ${STACK_NAME} --region ${REGION} --max-items 30"
            ask_yn "Continue with the remaining steps anyway?" "N" || exit 1
        fi
    else
        skip "Skipped stack delete."
    fi
else
    skip "No stack to delete."
fi

# ── Step 2: Delete the Lambda deployment bucket ──────────────────────────────
header "Step 2/5  Lambda deployment bucket"

if bucket_exists "$DEPLOY_BUCKET" "$REGION"; then
    if ask_yn "Delete deploy bucket '${DEPLOY_BUCKET}' and its contents?" "Y"; then
        remove_bucket "$DEPLOY_BUCKET" "$REGION"
    else
        skip "Skipped deploy-bucket delete."
    fi
else
    skip "Deploy bucket already gone."
fi

# ── Step 3: Log bucket (optional, extra-protected) ───────────────────────────
header "Step 3/5  Log bucket (detection history)"

if [[ -z "$LOG_BUCKET" ]]; then
    skip "No log bucket name discovered (stack was already gone)."
    say  "  If you want to delete it, run:"
    say  "    aws s3 rm s3://<log-bucket> --recursive && aws s3 rb s3://<log-bucket>"
elif ! bucket_exists "$LOG_BUCKET" "$REGION"; then
    skip "Log bucket '${LOG_BUCKET}' not found."
else
    warn "This bucket contains your AI Guard detection history."
    warn "Deletion is permanent and cannot be undone."
    if ask_yn "Delete log bucket '${LOG_BUCKET}' and ALL its contents?" "N"; then
        if ask_yn "Are you ABSOLUTELY sure? Type y to confirm." "N"; then
            remove_bucket "$LOG_BUCKET" "$REGION"
        else
            skip "Log bucket kept."
        fi
    else
        skip "Log bucket kept."
    fi
fi

# ── Step 4: SES verified sender (optional) ───────────────────────────────────
header "Step 4/5  SES verified sender"

if [[ -z "$SES_SENDER" ]]; then
    skip "No SES sender configured on the stack."
else
    if ask_yn "Remove SES verified identity '${SES_SENDER}'?" "N"; then
        if aws ses delete-identity --identity "$SES_SENDER" --region "$REGION" 2>&1; then
            ok "SES identity '${SES_SENDER}' removed."
        else
            warn "Could not remove SES identity (may not exist or may belong to another region)."
        fi
    else
        skip "SES identity kept."
    fi
fi

# ── Step 5: Local helper files ───────────────────────────────────────────────
header "Step 5/5  Local helper files"

LOCAL_FILES=()
[[ -f "${SCRIPT_DIR}/cfn-deploy.sh"                    ]] && LOCAL_FILES+=("${SCRIPT_DIR}/cfn-deploy.sh")
[[ -f "${SCRIPT_DIR}/cfn-deploy-${STACK_NAME}.sh"      ]] && LOCAL_FILES+=("${SCRIPT_DIR}/cfn-deploy-${STACK_NAME}.sh")
[[ -f "${SCRIPT_DIR}/cfn-parameters.json"              ]] && LOCAL_FILES+=("${SCRIPT_DIR}/cfn-parameters.json")

if [[ ${#LOCAL_FILES[@]} -eq 0 ]]; then
    skip "No local helper files to remove."
else
    say "  Found:"
    for f in "${LOCAL_FILES[@]}"; do say "    - $f"; done
    warn "cfn-deploy.sh contains your AI Guard API key in plaintext."
    if ask_yn "Delete these files?" "Y"; then
        rm -f "${LOCAL_FILES[@]}"
        ok "Local helper files removed."
    else
        skip "Local files kept (remember they contain secrets)."
    fi
fi

# ── Verification ─────────────────────────────────────────────────────────────
header "Verification"

# Stack
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    warn "Stack '${STACK_NAME}' is still present."
else
    ok "Stack '${STACK_NAME}' is gone."
fi

# Deploy bucket
if bucket_exists "$DEPLOY_BUCKET" "$REGION"; then
    warn "Deploy bucket '${DEPLOY_BUCKET}' is still present."
else
    ok "Deploy bucket '${DEPLOY_BUCKET}' is gone."
fi

# Source-bucket notification residue
if [[ -n "$SOURCE_BUCKET" ]]; then
    LEFTOVER=$(aws s3api get-bucket-notification-configuration \
        --bucket "$SOURCE_BUCKET" --region "$REGION" \
        --query "LambdaFunctionConfigurations[?contains(LambdaFunctionArn, '${STACK_NAME}-scanner')]" \
        --output text 2>/dev/null || echo "")
    if [[ -z "$LEFTOVER" ]]; then
        ok "Source bucket '${SOURCE_BUCKET}' has no leftover notification."
    else
        warn "Source bucket still has a Lambda notification referencing '${STACK_NAME}-scanner'."
        warn "  Remove it manually with:"
        warn "    aws s3api put-bucket-notification-configuration --bucket ${SOURCE_BUCKET} --notification-configuration '{}'"
    fi
fi

# Orphaned CloudWatch log groups
ORPHANS=$(aws logs describe-log-groups --region "$REGION" \
    --log-group-name-prefix "/aws/lambda/${STACK_NAME}-" \
    --query 'logGroups[].logGroupName' --output text 2>/dev/null || echo "")
if [[ -z "$ORPHANS" ]]; then
    ok "No orphaned CloudWatch log groups."
else
    warn "Orphaned CloudWatch log groups found (left behind from a previous deployment):"
    for lg in $ORPHANS; do warn "  ${lg}"; done
    if ask_yn "Delete these orphaned log groups?" "Y"; then
        for lg in $ORPHANS; do
            aws logs delete-log-group --log-group-name "$lg" --region "$REGION"
            ok "Deleted log group: ${lg}"
        done
    else
        skip "Orphaned log groups kept."
        warn "  These will block redeployment. Delete manually:"
        for lg in $ORPHANS; do
            warn "    aws logs delete-log-group --log-group-name ${lg} --region ${REGION}"
        done
    fi
fi

printf '\n%s\n' "${BOLD}${GREEN}+============================================+${RESET}"
printf '%s\n'   "${BOLD}${GREEN}|       Uninstall finished                   |${RESET}"
printf '%s\n\n' "${BOLD}${GREEN}+============================================+${RESET}"

if [[ -n "$LOG_BUCKET" ]] && bucket_exists "$LOG_BUCKET" "$REGION"; then
    say "Log bucket kept: s3://${LOG_BUCKET}  (your detection history)"
fi
say ""
