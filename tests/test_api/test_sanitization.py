"""Tests for input sanitization on POST /manifests and POST /search."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from api.models.sanitize import (
    check_null_bytes_recursive,
    contains_null_bytes,
    strip_strings_recursive,
)


# ---------------------------------------------------------------------------
# Unit tests for sanitize helpers
# ---------------------------------------------------------------------------

def test_strip_strings_recursive_strips_values():
    """All string values should be stripped of whitespace."""
    data = {"name": "  SkyBridge  ", "nested": {"desc": " hello\n"}}
    result = strip_strings_recursive(data)
    assert result["name"] == "SkyBridge"
    assert result["nested"]["desc"] == "hello"


def test_strip_strings_recursive_handles_lists():
    """Strings in lists should be stripped."""
    data = {"tags": ["  travel.air.book  ", "  finance.payments.send  "]}
    result = strip_strings_recursive(data)
    assert result["tags"] == ["travel.air.book", "finance.payments.send"]


def test_strip_strings_recursive_preserves_non_strings():
    """Non-string values should pass through unchanged."""
    data = {"count": 42, "active": True, "score": 3.14, "empty": None}
    result = strip_strings_recursive(data)
    assert result == data


def test_contains_null_bytes():
    """Null byte detection should work."""
    assert contains_null_bytes("hello\x00world") is True
    assert contains_null_bytes("hello world") is False
    assert contains_null_bytes("") is False


def test_check_null_bytes_recursive_finds_nested():
    """Null bytes should be detected in nested structures."""
    data = {
        "name": "clean",
        "capabilities": [{"description": "has\x00null"}],
    }
    violations = check_null_bytes_recursive(data)
    assert len(violations) == 1
    assert "capabilities[0].description" in violations[0]


# ---------------------------------------------------------------------------
# POST /manifests sanitization tests
# ---------------------------------------------------------------------------

def test_manifest_null_bytes_in_name_returns_422(client, api_key_headers, sample_manifest_payload):
    """Null bytes in name should be rejected with 422."""
    payload = sample_manifest_payload.copy()
    payload["name"] = "Sky\x00Bridge"

    # Pydantic model_validator rejects before reaching the registry
    response = client.post("/v1/manifests", json=payload, headers=api_key_headers)

    assert response.status_code == 422
    assert "null bytes" in response.json()["detail"][0]["msg"]


def test_manifest_invalid_domain_chars_returns_422(client, api_key_headers, sample_manifest_payload):
    """Domain with invalid FQDN characters should be rejected."""
    payload = sample_manifest_payload.copy()
    payload["domain"] = "f1ight!bookerpro.com"

    # Pydantic FQDN validation rejects before reaching the registry
    response = client.post("/v1/manifests", json=payload, headers=api_key_headers)

    assert response.status_code == 422
    body = response.json()
    assert any("domain" in str(err.get("loc", "")) for err in body["detail"])


def test_manifest_name_stripped_before_validation(client, api_key_headers, sample_manifest_payload):
    """Whitespace should be stripped from name before length validation."""
    from api.models.query import ManifestRegistrationResponse

    payload = sample_manifest_payload.copy()
    payload["name"] = "   SkyBridge Travel   "

    mock_response = ManifestRegistrationResponse(
        service_id=payload["service_id"],
        trust_tier=1,
        trust_score=20.0,
        status="registered",
        capabilities_indexed=1,
    )

    captured_args = {}

    async def _mock_register(*args, **kwargs):
        captured_args.update(kwargs)
        return mock_response

    with patch("api.services.registry.register_manifest", new=_mock_register):
        response = client.post("/v1/manifests", json=payload, headers=api_key_headers)

    # Should succeed — name is valid after strip
    assert response.status_code == 201


def test_manifest_name_too_long_returns_422(client, api_key_headers, sample_manifest_payload):
    """Name exceeding 200 chars should be rejected."""
    payload = sample_manifest_payload.copy()
    payload["name"] = "A" * 201

    response = client.post("/v1/manifests", json=payload, headers=api_key_headers)

    assert response.status_code == 422


def test_manifest_description_too_long_returns_422(client, api_key_headers, sample_manifest_payload):
    """Capability description exceeding 2000 chars should be rejected."""
    payload = sample_manifest_payload.copy()
    payload["capabilities"][0]["description"] = "A" * 2001

    response = client.post("/v1/manifests", json=payload, headers=api_key_headers)

    assert response.status_code == 422


def test_manifest_domain_max_length(client, api_key_headers, sample_manifest_payload):
    """Domain exceeding 253 chars should be rejected."""
    payload = sample_manifest_payload.copy()
    # Build a domain over 253 chars: labels of 50 chars each, 6 labels = 305 chars
    payload["domain"] = ".".join(["a" * 50] * 6) + ".com"

    response = client.post("/v1/manifests", json=payload, headers=api_key_headers)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /search sanitization tests
# ---------------------------------------------------------------------------

def test_search_empty_query_after_strip_returns_400(client, api_key_headers):
    """Empty query after whitespace strip should return 400."""
    response = client.post(
        "/v1/search",
        json={"query": "   "},
        headers=api_key_headers,
    )

    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_search_query_stripped(client, api_key_headers):
    """Search query should be stripped before processing."""
    captured_requests = []

    async def _mock_search(*args, **kwargs):
        # Capture the request object to verify stripping
        request_obj = kwargs.get("request") or (args[0] if args else None)
        if request_obj is not None:
            captured_requests.append(request_obj)
        return {
            "total": 0,
            "limit": 10,
            "offset": 0,
            "results": [],
        }

    with patch("api.services.registry.search_services", new=_mock_search):
        response = client.post(
            "/v1/search",
            json={"query": "  book a flight  "},
            headers=api_key_headers,
        )

    assert response.status_code == 200
    # Verify the query was stripped before being passed to registry
    assert len(captured_requests) == 1
    assert captured_requests[0].query == "book a flight"


def test_search_query_too_long_returns_422(client, api_key_headers):
    """Query exceeding 500 chars should be rejected."""
    response = client.post(
        "/v1/search",
        json={"query": "a" * 501},
        headers=api_key_headers,
    )

    assert response.status_code == 422
