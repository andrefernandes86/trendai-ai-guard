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
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "TMV1-Application-Name": app_name,
            "TMV1-Request-Type": "SimpleRequestGuard",
            "Prefer": "return=representation",
            "Content-Type": "application/json",
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
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning("Request failed (%s), retrying in %ds", exc, wait)
                    time.sleep(wait)

        raise RuntimeError(f"AI Guard scan failed after {_MAX_RETRIES} attempts") from last_exc
