import hashlib
import logging
import os

import boto3

from ai_guard_client import AIGuardClient
from notifier import save_log_to_s3, send_email_notification
from text_extractor import extract_text

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")

DOCUMENT_EXTENSIONS = {
    ".txt", ".csv", ".json", ".md", ".xml", ".yaml", ".yml",
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".html", ".htm", ".rtf",
}

# MAX_TEXT_KB: 0 means "no truncation - send everything we extracted".
# Any other positive integer is the cap in KB.
MAX_TEXT_BYTES = int(os.environ.get("MAX_TEXT_KB", "500")) * 1024

# Whether to tag the source S3 object with the scan verdict (Yes/No string
# from the CloudFormation parameter EnableFileTagging).
ENABLE_FILE_TAGGING = os.environ.get("ENABLE_FILE_TAGGING", "No").lower() == "yes"

# S3 object tag we write back. Existing tags on the object are preserved.
TAG_KEY = "tm-v1-aiguard"
TAG_VALUE_ALLOW = "no-risks-detected"
TAG_VALUE_BLOCK = "malicious-prompt-detected"


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        ext = os.path.splitext(key.lower())[1]
        if ext not in DOCUMENT_EXTENSIONS:
            logger.info("Skipping non-document file: %s", key)
            continue
        try:
            _process_file(bucket, key)
        except Exception:
            logger.exception("Error processing s3://%s/%s", bucket, key)
    return {"statusCode": 200, "body": "OK"}


def _process_file(bucket: str, key: str) -> None:
    logger.info("Processing s3://%s/%s", bucket, key)

    response = s3_client.get_object(Bucket=bucket, Key=key)
    file_bytes = response["Body"].read()

    file_hash = hashlib.sha256(file_bytes).hexdigest()
    logger.info("SHA-256: %s", file_hash)

    ext = os.path.splitext(key.lower())[1]
    text = extract_text(file_bytes, ext)

    if not text or not text.strip():
        logger.info("No text extracted from %s, skipping", key)
        return

    encoded = text.encode("utf-8")
    if MAX_TEXT_BYTES > 0 and len(encoded) > MAX_TEXT_BYTES:
        text = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        logger.info("Text truncated to %d bytes", MAX_TEXT_BYTES)
    elif MAX_TEXT_BYTES == 0:
        logger.info("MAX_TEXT_KB=0 - sending full %d bytes of extracted text", len(encoded))

    client = AIGuardClient(
        api_key=os.environ["AI_GUARD_API_KEY"],
        endpoint=os.environ.get(
            "AI_GUARD_ENDPOINT",
            "https://api.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails",
        ),
        # Fallback name used only if the per-file sanitized name is empty.
        app_name=os.environ.get("AI_GUARD_APP_NAME", "ai-guard-s3-monitor"),
    )

    # Tag each scan with "<bucket>--<basename>" so Vision One audit
    # logs show where every scan came from. The '--' is a visual
    # separator that survives the [a-zA-Z0-9_-] / 64-char sanitization
    # the client applies (it only collapses underscores, not hyphens).
    # Casing is preserved: S3 bucket names are always lowercase, but
    # file names keep their original case so they're recognizable in
    # the Vision One audit log.
    result = client.scan(
        text,
        app_name=f"{bucket}--{os.path.basename(key)}",
    )
    action = result.get("action", "").lower()
    logger.info("AI Guard action for %s: %s", key, action)

    if action == "block":
        logger.warning("Malicious content detected in %s", key)
        save_log_to_s3(
            s3_client=s3_client,
            log_bucket=os.environ["LOG_BUCKET_NAME"],
            file_name=key,
            file_hash=file_hash,
            scan_result=result,
            text_snippet=text[:2000],
        )
        send_email_notification(
            recipient=os.environ["NOTIFICATION_EMAIL"],
            file_name=key,
            file_hash=file_hash,
            scan_result=result,
        )

    if ENABLE_FILE_TAGGING:
        _tag_object_with_verdict(bucket, key, action)


def _tag_object_with_verdict(bucket: str, key: str, action: str) -> None:
    """
    Add or update a 'tm-v1-aiguard' tag on the source object reflecting
    the AI Guard verdict. Existing tags (other keys) are preserved.
    Non-fatal: logs and returns on any failure so a tagging issue never
    blocks a successful scan + log + alert.
    """
    verdict = TAG_VALUE_BLOCK if action == "block" else TAG_VALUE_ALLOW
    try:
        # Read existing tags so we can merge instead of replace.
        existing = s3_client.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", [])
        tags = [t for t in existing if t.get("Key") != TAG_KEY]
        tags.append({"Key": TAG_KEY, "Value": verdict})
        # S3 caps at 10 tags per object; if the customer has 9 of their
        # own tags already, drop the oldest to make room for ours.
        if len(tags) > 10:
            tags = tags[-10:]
        s3_client.put_object_tagging(
            Bucket=bucket,
            Key=key,
            Tagging={"TagSet": tags},
        )
        logger.info("Tagged s3://%s/%s with %s=%s", bucket, key, TAG_KEY, verdict)
    except Exception as exc:
        logger.warning(
            "Could not tag s3://%s/%s with verdict (%s); continuing.",
            bucket, key, exc,
        )
