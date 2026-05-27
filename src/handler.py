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

MAX_TEXT_BYTES = 500 * 1024  # 500 KB


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

    # Truncate to first 500 KB
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        text = encoded[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
        logger.info("Text truncated to %d bytes", MAX_TEXT_BYTES)

    client = AIGuardClient(
        api_key=os.environ["AI_GUARD_API_KEY"],
        endpoint=os.environ.get(
            "AI_GUARD_ENDPOINT",
            "https://api.xdr.trendmicro.com/v3.0/xdr/guard/scan",
        ),
        app_name=os.environ.get("AI_GUARD_APP_NAME", "ai-guard-s3-monitor"),
    )

    result = client.scan(text)
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
