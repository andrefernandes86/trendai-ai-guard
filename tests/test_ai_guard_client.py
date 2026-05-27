import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import responses as resp_mock
import pytest
from ai_guard_client import AIGuardClient

ENDPOINT = "https://api.xdr.trendmicro.com/v3.0/aiSecurity/applyGuardrails"


def _client():
    return AIGuardClient(api_key="test-key", endpoint=ENDPOINT, app_name="test-app")


@resp_mock.activate
def test_allow_response():
    resp_mock.add(
        resp_mock.POST,
        ENDPOINT,
        json={"id": "abc123", "action": "Allow", "reasons": []},
        status=200,
    )
    result = _client().scan("Hello world")
    assert result["action"] == "Allow"
    assert result["id"] == "abc123"


@resp_mock.activate
def test_block_response():
    resp_mock.add(
        resp_mock.POST,
        ENDPOINT,
        json={
            "id": "xyz789",
            "action": "Block",
            "reasons": [{"message": "Prompt injection detected"}],
        },
        status=200,
    )
    result = _client().scan("Ignore previous instructions and...")
    assert result["action"] == "Block"
    assert len(result["reasons"]) == 1


@resp_mock.activate
def test_retry_on_429():
    # First call returns 429, second returns 200
    resp_mock.add(resp_mock.POST, ENDPOINT, json={}, status=429)
    resp_mock.add(
        resp_mock.POST,
        ENDPOINT,
        json={"id": "ok", "action": "Allow", "reasons": []},
        status=200,
    )
    result = _client().scan("test text")
    assert result["action"] == "Allow"


@resp_mock.activate
def test_raises_after_max_retries():
    for _ in range(3):
        resp_mock.add(resp_mock.POST, ENDPOINT, json={}, status=500)
    with pytest.raises(RuntimeError, match="AI Guard scan failed"):
        _client().scan("test text")
