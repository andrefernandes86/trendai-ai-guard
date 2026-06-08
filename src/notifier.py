from __future__ import annotations

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
    chunk_results: list[dict] | None = None,
) -> None:
    """Write a JSON detection log to the secondary S3 bucket."""
    timestamp = datetime.now(timezone.utc).isoformat()
    total_chunks = chunk_results[0]["total_chunks"] if chunk_results else 1
    blocked_chunks = [c for c in (chunk_results or []) if c.get("action") == "block"]

    entry = {
        "timestamp": timestamp,
        "file_name": file_name,
        "file_hash_sha256": file_hash,
        "scan_id": scan_result.get("id"),
        "action": scan_result.get("action"),
        "chunks_scanned": total_chunks,
        "chunks_blocked": len(blocked_chunks),
        "chunk_breakdown": [
            {
                "chunk": c["chunk"],
                "bytes": c["bytes"],
                "action": c["action"],
            }
            for c in (chunk_results or [])
        ],
        "reasons": scan_result.get("reasons", []),
        "harmful_content": _harmful_categories(scan_result),
        "sensitive_information_rules": _sensitive_rules(scan_result),
        "prompt_attack_detected": _prompt_attack_detected(scan_result),
        "mitre_attack": _extract_mitre(scan_result),
        "owasp": _extract_owasp(scan_result),
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
    chunk_results: list[dict] | None = None,
) -> None:
    """Send an SES alert email to *recipient*. No-ops if recipient is empty."""
    if not recipient:
        logger.info("NotificationEmail is not set, skipping email alert")
        return
    sender = os.environ.get("SES_SENDER_EMAIL") or recipient
    mitre = _extract_mitre(scan_result)
    owasp = _extract_owasp(scan_result)
    reasons = scan_result.get("reasons", [])
    harmful = _harmful_categories(scan_result)
    sensitive = _sensitive_rules(scan_result)
    prompt_attack = _prompt_attack_detected(scan_result)
    basename = os.path.basename(file_name)
    total_chunks = chunk_results[0]["total_chunks"] if chunk_results else 1
    blocked_chunks = [c for c in (chunk_results or []) if c.get("action") == "block"]

    subject = f"[AI Guard Alert] Malicious content detected: {basename}"

    lines = [
        "AI Guard S3 Monitor - Security Alert",
        "=" * 50,
        "",
        f"File Name : {file_name}",
        f"SHA-256   : {file_hash}",
        f"Scan ID   : {scan_result.get('id', 'N/A')}",
        f"Action    : BLOCK",
        f"Chunks    : {len(blocked_chunks)} of {total_chunks} blocked",
        "",
    ]

    if total_chunks > 1:
        lines.append("Chunk Breakdown:")
        for c in (chunk_results or []):
            status = "BLOCK" if c["action"] == "block" else "allow"
            lines.append(f"  chunk {c['chunk']:>3}/{total_chunks}  {c['bytes']:>6} bytes  {status}")
        lines.append("")

    lines.append("Detection Reasons:")
    for reason in reasons:
        msg = reason.get("message", str(reason)) if isinstance(reason, dict) else str(reason)
        lines.append(f"  - {msg}")

    if harmful:
        lines += ["", "Harmful Content Categories:"]
        for h in harmful:
            score = f"  (confidence {h['confidence']:.2f})" if h.get("confidence") is not None else ""
            lines.append(f"  - {h['category']}{score}")

    if sensitive:
        lines += ["", "Sensitive Information Rules Triggered:"]
        for rule_id in sensitive:
            lines.append(f"  - {rule_id}")

    if prompt_attack:
        lines += ["", "Prompt Attack Detected: yes"]

    if mitre:
        lines += ["", "MITRE ATT&CK References:"]
        lines += [f"  - {r}" for r in mitre]

    if owasp:
        lines += ["", "OWASP LLM Top 10 References:"]
        lines += [f"  - {r}" for r in owasp]

    lines += [
        "",
        "Please review the file immediately and take appropriate action.",
        "",
        "- AI Guard AWS Monitor",
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


# applyGuardrails response has structured detection blocks:
#   harmfulContent:        [{ category, hasPolicyViolation, confidenceScore }]
#   sensitiveInformation:  { hasPolicyViolation, rules: [{ id }] }
#   promptAttacks:         [{ hasPolicyViolation, confidenceScore }]

def _harmful_categories(result: dict) -> list[dict]:
    """Return only the harmfulContent entries that have a policy violation."""
    items = result.get("harmfulContent") or []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("hasPolicyViolation"):
            out.append({
                "category": item.get("category", "unknown"),
                "confidence": item.get("confidenceScore"),
            })
    return out


def _sensitive_rules(result: dict) -> list[str]:
    """Return triggered sensitive-information rule IDs."""
    block = result.get("sensitiveInformation") or {}
    if not isinstance(block, dict) or not block.get("hasPolicyViolation"):
        return []
    rules = block.get("rules") or []
    out = []
    for rule in rules:
        if isinstance(rule, dict):
            rid = rule.get("id")
            if rid:
                out.append(str(rid))
        elif rule:
            out.append(str(rule))
    return out


def _prompt_attack_detected(result: dict) -> bool:
    """True if any promptAttacks entry reports a policy violation."""
    items = result.get("promptAttacks") or []
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict) and item.get("hasPolicyViolation")
        for item in items
    )
