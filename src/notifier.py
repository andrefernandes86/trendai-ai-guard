import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)


def save_log_to_s3(
    s3_client,
    log_bucket: str,
    file_name: str,
    file_hash: str,
    scan_result: dict,
    text_snippet: str,
) -> None:
    """Write a JSON detection log to the secondary S3 bucket."""
    timestamp = datetime.now(timezone.utc).isoformat()
    mitre = _extract_mitre(scan_result)
    owasp = _extract_owasp(scan_result)

    entry = {
        "timestamp": timestamp,
        "file_name": file_name,
        "file_hash_sha256": file_hash,
        "scan_id": scan_result.get("id"),
        "action": scan_result.get("action"),
        "reasons": scan_result.get("reasons", []),
        "mitre_attack": mitre,
        "owasp": owasp,
        "malicious_prompt_snippet": text_snippet[:2000],
        "full_scan_result": scan_result,
    }

    dt = datetime.now(timezone.utc)
    basename = os.path.basename(file_name)
    log_key = (
        f"logs/{dt.year}/{dt.month:02d}/{dt.day:02d}/"
        f"{basename}_{dt.strftime('%H%M%S%f')}.json"
    )

    s3_client.put_object(
        Bucket=log_bucket,
        Key=log_key,
        Body=json.dumps(entry, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("Detection log saved to s3://%s/%s", log_bucket, log_key)


def send_email_notification(
    recipient: str,
    file_name: str,
    file_hash: str,
    scan_result: dict,
) -> None:
    """Send an SES alert email to *recipient*."""
    sender = os.environ.get("SES_SENDER_EMAIL", recipient)
    mitre = _extract_mitre(scan_result)
    owasp = _extract_owasp(scan_result)
    reasons = scan_result.get("reasons", [])
    basename = os.path.basename(file_name)

    subject = f"[AI Guard Alert] Malicious content detected: {basename}"

    lines = [
        "AI Guard S3 Monitor — Security Alert",
        "=" * 50,
        "",
        f"File Name : {file_name}",
        f"SHA-256   : {file_hash}",
        f"Scan ID   : {scan_result.get('id', 'N/A')}",
        f"Action    : {scan_result.get('action', 'N/A').upper()}",
        "",
        "Detection Reasons:",
    ]
    for reason in reasons:
        msg = reason.get("message", str(reason)) if isinstance(reason, dict) else str(reason)
        lines.append(f"  - {msg}")

    if mitre:
        lines += ["", "MITRE ATT&CK References:"]
        lines += [f"  - {r}" for r in mitre]

    if owasp:
        lines += ["", "OWASP / OWASP LLM Top 10 References:"]
        lines += [f"  - {r}" for r in owasp]

    lines += [
        "",
        "Please review the file immediately and take appropriate action.",
        "",
        "— AI Guard AWS Monitor",
    ]

    ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": "\n".join(lines), "Charset": "UTF-8"}},
        },
    )
    logger.info("Alert email sent to %s", recipient)


# ── helpers ──────────────────────────────────────────────────────────────────

def _extract_mitre(result: dict) -> list[str]:
    blob = json.dumps(result)
    ids = set(re.findall(r"\bT\d{4}(?:\.\d{3})?\b", blob))
    for key in ("mitre_attack", "mitreAttack", "mitre", "attack_patterns"):
        val = result.get(key)
        if val:
            ids.update(val if isinstance(val, list) else [val])
    return sorted(ids)


def _extract_owasp(result: dict) -> list[str]:
    blob = json.dumps(result)
    ids = set(re.findall(r"\b(?:LLM\d{2}|A\d{2}:\d{4}|OWASP[-\s][A-Z]\d+)\b", blob, re.I))
    for key in ("owasp", "owasp_llm", "owaspLlm"):
        val = result.get(key)
        if val:
            ids.update(val if isinstance(val, list) else [val])
    return sorted(ids)
