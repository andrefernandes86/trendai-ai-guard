import logging
import time

import requests

logger = logging.getLogger(__name__)

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds


class AIGuardClient:
    def __init__(self, api_key: str, endpoint: str, app_name: str) -> None:
        self.endpoint = endpoint
        # Only the three headers documented as required by applyGuardrails.
        # TMV1-Request-Type and Prefer are intentionally NOT sent: they show
        # up as user-supplied slots in Trend's sample code, and the server
        # rejects unknown values (e.g. "SimpleRequestGuard").
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json;charset=utf-8",
            "TMV1-Application-Name": app_name,
        }

    def scan(self, text: str) -> dict:
        """Submit *text* to AI Guard and return the parsed JSON response."""
        payload = {"prompt": text}
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                resp = requests.post(
                    self.endpoint,
                    headers=self._headers,
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
