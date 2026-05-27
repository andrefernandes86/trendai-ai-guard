from __future__ import annotations

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds

# TMV1-Application-Name constraint per AI Guard API:
#   characters: a-z A-Z 0-9 _ -        max length: 64
_APP_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")
_APP_NAME_MAX_LEN = 64


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
        ([a-zA-Z0-9_-], max 64 chars). Useful for tagging each scan with
        the file name it came from so the call shows up in Vision One
        audit logs that way.
        """
        effective_app_name = (
            sanitize_app_name(app_name, fallback=self.default_app_name)
            if app_name
            else self.default_app_name
        )
        headers = {**self._base_headers, "TMV1-Application-Name": effective_app_name}

        payload = {"prompt": text}
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = requests.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
                if resp.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning(
                        "AI Guard returned %s, retrying in %ds (attempt %d/%d)",
                        resp.status_code,
                        wait,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                if not resp.ok:
                    # Surface the server's error message so we can see exactly
                    # what was rejected (missing header, bad body field, etc).
                    body_preview = (resp.text or "")[:1500]
                    logger.error(
                        "AI Guard returned %s: %s",
                        resp.status_code,
                        body_preview,
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
