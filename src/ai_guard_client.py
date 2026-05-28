from __future__ import annotations

import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

# 413 is included here because empirically AI Guard returns
# PayloadTooLarge under sustained concurrent load even for bodies well
# below the 51,200-byte ceiling - we treat that as a soft rate-limit
# signal and back off. Real oversize bodies are caught by the client's
# pre-flight trim before the request goes out, so a 413 on the wire
# is almost always transient.
_RETRY_STATUS_CODES = {413, 429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BACKOFF_BASE = 2  # seconds

# TMV1-Application-Name constraint per AI Guard API:
#   characters: a-z A-Z 0-9 _ -        max length: 64
_APP_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")
_APP_NAME_MAX_LEN = 64

# AI Guard request payload hard limit. Confirmed by direct probe:
# bodies <= 51,200 bytes succeed; bodies > 51,200 bytes return HTTP
# 413. Earlier observations of 51,200-byte bodies "failing" under
# Lambda burst were concurrency-induced - AI Guard appears to return
# 413 rather than 429 when its backend is saturated, which is why 413
# is now also in the retry set.
MAX_API_PAYLOAD_BYTES = 51200  # 50 KB exactly (inclusive)


def sanitize_app_name(value: str, fallback: str = "ai-guard-s3-monitor") -> str:
    """
    Coerce *value* into the TMV1-Application-Name accepted charset.

    The header only permits [a-zA-Z0-9_-] and is capped at 64 chars. We
    replace any other character with '_', collapse repeats, trim, and
    truncate. Empty / unusable input falls back to *fallback*.
    """
    if not value:
        return fallback
    cleaned = _APP_NAME_INVALID.sub("_", value)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    cleaned = cleaned[:_APP_NAME_MAX_LEN]
    return cleaned or fallback


def _build_payload(text: str) -> tuple[bytes, int, int]:
    """
    Build the JSON body for a scan, trimming the text if the encoded body
    would exceed AI Guard's payload limit.

    Uses ensure_ascii=False so non-ASCII characters keep their UTF-8 size
    instead of expanding to \\uXXXX escapes (which used to triple the
    size of CJK / Russian / heavily-accented text and trip HTTP 413s).

    Returns (body_bytes, original_chars, sent_chars).
    """
    original_chars = len(text)
    body = json.dumps({"prompt": text}, ensure_ascii=False).encode("utf-8")
    if len(body) <= MAX_API_PAYLOAD_BYTES:
        return body, original_chars, original_chars

    # Binary-search for the longest prefix whose JSON-encoded body fits.
    lo, hi = 0, original_chars
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = json.dumps({"prompt": text[:mid]}, ensure_ascii=False).encode("utf-8")
        if len(candidate) <= MAX_API_PAYLOAD_BYTES:
            lo = mid
        else:
            hi = mid - 1
    trimmed = text[:lo]
    body = json.dumps({"prompt": trimmed}, ensure_ascii=False).encode("utf-8")
    return body, original_chars, lo


class AIGuardClient:
    def __init__(self, api_key: str, endpoint: str, app_name: str) -> None:
        self.endpoint = endpoint
        self.default_app_name = app_name or "ai-guard-s3-monitor"
        # Auth + content-type are constant; app-name is set per request so
        # callers can attribute the scan to a specific file or workload.
        self._base_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json;charset=utf-8",
        }

    def scan(self, text: str, app_name: str | None = None) -> dict:
        """
        Submit *text* to AI Guard and return the parsed JSON response.

        If *app_name* is provided, it overrides the client's default and is
        sanitized to satisfy the TMV1-Application-Name constraint
        ([a-zA-Z0-9_-], max 64 chars).

        The text is JSON-encoded with ensure_ascii=False and the body is
        pre-flighted against AI Guard's 50 KB payload limit; if the body
        would exceed it we binary-search for the largest prefix that fits
        and trim before sending. Callers therefore never need to think
        about the API limit.
        """
        effective_app_name = (
            sanitize_app_name(app_name, fallback=self.default_app_name)
            if app_name
            else self.default_app_name
        )
        headers = {**self._base_headers, "TMV1-Application-Name": effective_app_name}
        logger.info("TMV1-Application-Name: %s", effective_app_name)

        body, original_chars, sent_chars = _build_payload(text)
        if sent_chars < original_chars:
            logger.warning(
                "Text trimmed in client from %d to %d chars to fit %d-byte API limit",
                original_chars, sent_chars, MAX_API_PAYLOAD_BYTES,
            )
        logger.info("Outgoing AI Guard payload: %d bytes", len(body))

        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = requests.post(
                    self.endpoint,
                    headers=headers,
                    data=body,
                    timeout=30,
                )
                if resp.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning(
                        "AI Guard returned %s, retrying in %ds (attempt %d/%d)",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                if not resp.ok:
                    # Surface the server's error message so we can see exactly
                    # what was rejected (missing header, bad body field, etc).
                    body_preview = (resp.text or "")[:1500]
                    logger.error(
                        "AI Guard returned %s: %s",
                        resp.status_code, body_preview,
                    )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning("Request failed (%s), retrying in %ds", exc, wait)
                    time.sleep(wait)

        raise RuntimeError(f"AI Guard scan failed after {_MAX_RETRIES} attempts") from last_exc
