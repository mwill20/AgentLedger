"""Tests for rate limiting middleware."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock, patch

from api.ratelimit import (
    IP_RATE_LIMIT,
    IP_RATE_WINDOW_SECONDS,
    _check_ip_rate_limit,
    _check_api_key_quota,
)


# ---------------------------------------------------------------------------
# Per-IP rate limiting
# ---------------------------------------------------------------------------

def test_ip_rate_limit_allows_under_limit():
    """Requests under the limit should be allowed."""
    redis = AsyncMock()
    redis.incr.return_value = 1
    redis.expire.return_value = True
    redis.ttl.return_value = 60

    allowed, remaining, retry_after = asyncio.run(_check_ip_rate_limit(redis, "1.2.3.4"))
    assert allowed is True
    assert remaining == IP_RATE_LIMIT - 1
    assert retry_after == 0


def test_ip_rate_limit_blocks_over_limit():
    """Requests over the limit should be blocked."""
    redis = AsyncMock()
    redis.incr.return_value = IP_RATE_LIMIT + 1
    redis.ttl.return_value = 45

    allowed, remaining, retry_after = asyncio.run(_check_ip_rate_limit(redis, "1.2.3.4"))
    assert allowed is False
    assert remaining == 0
    assert retry_after == 45


def test_ip_rate_limit_passes_on_null_redis():
    """Rate limiting should pass when Redis is unavailable."""
    allowed, remaining, _ = asyncio.run(_check_ip_rate_limit(None, "1.2.3.4"))
    assert allowed is True


def test_ip_rate_limit_passes_on_redis_error():
    """Rate limiting should fail open on Redis errors."""
    redis = AsyncMock()
    redis.incr.side_effect = Exception("connection lost")

    allowed, remaining, _ = asyncio.run(_check_ip_rate_limit(redis, "1.2.3.4"))
    assert allowed is True


def test_api_key_quota_allows_config_keys():
    """Config-based API keys bypass DB quota check."""
    allowed, retry_after = asyncio.run(_check_api_key_quota(None, "test-api-key"))
    assert allowed is True


def test_api_key_quota_allows_configured_keys():
    """Keys matching settings.api_keys should bypass DB check."""
    mock_factory = MagicMock()
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(mock_factory, "dev-local-only")
    )
    assert allowed is True
    assert retry_after is None


def _make_async_session_factory(mock_session):
    """Create a properly async context manager factory for _check_api_key_quota."""
    class _FakeSessionCtx:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, *args):
            pass
    def factory():
        return _FakeSessionCtx()
    return factory


def test_api_key_quota_db_not_found():
    """Keys not in DB should be allowed (auth middleware handles rejection)."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.first.return_value = None
    mock_session.execute.return_value = mock_result

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is True


def test_api_key_quota_inactive_key():
    """Inactive DB keys should be rejected."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.first.return_value = {
        "query_count": 0,
        "monthly_limit": 1000,
        "is_active": False,
    }
    mock_session.execute.return_value = mock_result

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is False


def test_api_key_quota_exhausted():
    """Keys at their monthly limit should be rejected with retry_after."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.first.return_value = {
        "query_count": 1000,
        "monthly_limit": 1000,
        "is_active": True,
    }
    mock_session.execute.return_value = mock_result

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is False
    assert retry_after is not None
    assert retry_after > 0


def test_api_key_quota_under_limit_increments():
    """Keys under their limit should be allowed and count incremented."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value.first.return_value = {
        "query_count": 50,
        "monthly_limit": 1000,
        "is_active": True,
    }
    mock_session.execute.return_value = mock_result

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is True
    # Should have called execute twice (SELECT + UPDATE)
    assert mock_session.execute.call_count == 2


def test_api_key_quota_db_error_fails_open():
    """DB errors during quota check should fail open."""
    def broken_factory():
        raise Exception("DB down")

    allowed, retry_after = asyncio.run(
        _check_api_key_quota(broken_factory, "unknown-key-not-in-config")
    )
    assert allowed is True


def test_ip_rate_limit_constants():
    """Rate limit constants should match spec."""
    assert IP_RATE_LIMIT == 100
    assert IP_RATE_WINDOW_SECONDS == 60


# ---------------------------------------------------------------------------
# Middleware integration (via test client)
# ---------------------------------------------------------------------------

def test_health_endpoint_exempt_from_rate_limiting(client):
    """Health endpoint should bypass rate limiting."""
    response = client.get("/v1/health")
    assert response.status_code == 200
    # Health should NOT have rate limit headers
    assert "X-RateLimit-Limit" not in response.headers


def test_ip_rate_limit_returns_429(client, api_key_headers):
    """Exceeding IP rate limit should return 429 with Retry-After."""
    with patch("api.ratelimit._check_ip_rate_limit", new_callable=AsyncMock, return_value=(False, 0, 30)):
        response = client.get("/v1/ontology", headers=api_key_headers)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"] == "IP rate limit exceeded"


def test_api_key_quota_returns_429(client, api_key_headers):
    """Exhausted API key quota should return 429 with Retry-After."""
    with patch("api.ratelimit._check_ip_rate_limit", new_callable=AsyncMock, return_value=(True, 99, 0)):
        with patch("api.ratelimit._check_api_key_quota", new_callable=AsyncMock, return_value=(False, 86400)):
            response = client.get("/v1/ontology", headers=api_key_headers)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "86400"
    assert response.json()["detail"] == "API key quota exhausted"
