import hashlib
import logging
import os
from urllib.parse import unquote_plus

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

# Each chunk sent to AI Guard is at most 40 KB of UTF-8 text.
# The API enforces a 50 KB JSON payload limit; 40 KB of text leaves
# comfortable room for the JSON wrapper and any multi-byte characters.
CHUNK_BYTES = 40 * 1024

# Whether to tag the source S3 object with the scan verdict (Yes/No string
# from the CloudFormation parameter EnableFileTagging).
ENABLE_FILE_TAGGING = os.environ.get("ENABLE_FILE_TAGGING", "No").lower() == "yes"

# S3 object tag we write back. Existing tags on the object are preserved.
TAG_KEY = "tm-v1-aiguard"
TAG_VALUE_ALLOW = "no-risks-detected"
TAG_VALUE_BLOCK = "malicious-prompt-detected"


def _chunk_text(text: str, chunk_size: int = CHUNK_BYTES) -> list[str]:
    """
    Split *text* into UTF-8-aware chunks of at most *chunk_size* bytes each.

    Tries to split on a newline boundary within the last 10 % of each chunk
    so that sentences / paragraphs are not cut mid-line. Falls back to a
    hard byte boundary when no newline is found.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= chunk_size:
        return [text]

    chunks: list[str] = []
    offset = 0
    total = len(encoded)

    while offset < total:
        end = min(offset + chunk_size, total)
        slice_bytes = encoded[offset:end]

        # Try to break on the last newline in the final 10 % of the slice
        # so chunks end at a natural boundary.
        if end < total:
            search_start = max(0, len(slice_bytes) - chunk_size // 10)
            nl = slice_bytes.rfind(b"\n", search_start)
            if nl != -1:
                slice_bytes = slice_bytes[: nl + 1]

        chunk = slice_bytes.decode("utf-8", errors="ignore")
        if not chunk:
            # Safety: avoid infinite loop on a degenerate input
            offset = end
            continue

        chunks.append(chunk)
        offset += len(chunk.encode("utf-8"))

    return chunks


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # S3 event notifications URL-encode the object key (spaces → '+',
        # special chars → '%xx'). Decode back to the real key before use.
        key = unquote_plus(record["s3"]["object"]["key"])
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

    total_bytes = len(text.encode("utf-8"))

    client = AIGuardClient(
        api_key=os.environ["AI_GUARD_API_KEY"],
        endpoint=os.environ.get(
            "AI_GUARD_ENDPOINT",
            "https://api.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails",
        ),
        app_name=os.environ.get("AI_GUARD_APP_NAME", "ai-guard-s3-monitor"),
    )

    # Tag each scan with "<bucket>--<basename>" so Vision One audit logs show
    # where every scan came from.
    file_app_name = f"{bucket}--{os.path.basename(key)}"

    chunks = _chunk_text(text)
    total_chunks = len(chunks)

    if total_chunks == 1:
        logger.info("Scanning %s (%d bytes, single chunk)", key, total_bytes)
    else:
        logger.info(
            "Scanning %s in %d chunks of up to %d KB each (%d bytes total)",
            key, total_chunks, CHUNK_BYTES // 1024, total_bytes,
        )

    # Scan every chunk regardless of intermediate verdicts so we know exactly
    # which parts of the file are malicious.
    chunk_results: list[dict] = []
    overall_action = "allow"

    for idx, chunk in enumerate(chunks, 1):
        chunk_bytes = len(chunk.encode("utf-8"))
        logger.info("Scanning chunk %d/%d (%d bytes)...", idx, total_chunks, chunk_bytes)

        result = client.scan(chunk, app_name=file_app_name)
        action = result.get("action", "").lower()

        chunk_results.append({
            "chunk": idx,
            "total_chunks": total_chunks,
            "bytes": chunk_bytes,
            "action": action,
            "result": result,
        })

        logger.info("AI Guard action for %s chunk %d/%d: %s", key, idx, total_chunks, action)

        if action == "block":
            overall_action = "block"
            logger.warning(
                "Malicious content detected in %s (chunk %d/%d)",
                key, idx, total_chunks,
            )

    logger.info(
        "AI Guard overall action for %s: %s (%d/%d chunks blocked)",
        key, overall_action,
        sum(1 for c in chunk_results if c["action"] == "block"),
        total_chunks,
    )

    if overall_action == "block":
        # Use the first blocking chunk's result as the primary scan_result for
        # the log entry; full per-chunk breakdown is stored alongside it.
        primary_result = next(c["result"] for c in chunk_results if c["action"] == "block")
        first_blocked_chunk = next(c for c in chunk_results if c["action"] == "block")
        snippet_start = sum(
            c["bytes"] for c in chunk_results if c["chunk"] < first_blocked_chunk["chunk"]
        )
        text_snippet = text[snippet_start: snippet_start + 2000]

        save_log_to_s3(
            s3_client=s3_client,
            log_bucket=os.environ["LOG_BUCKET_NAME"],
            file_name=key,
            file_hash=file_hash,
            scan_result=primary_result,
            text_snippet=text_snippet,
            chunk_results=chunk_results,
        )
        send_email_notification(
            recipient=os.environ["NOTIFICATION_EMAIL"],
            file_name=key,
            file_hash=file_hash,
            scan_result=primary_result,
            chunk_results=chunk_results,
        )

    if ENABLE_FILE_TAGGING:
        _tag_object_with_verdict(bucket, key, overall_action)


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
