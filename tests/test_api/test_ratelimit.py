"""Tests for rate limiting middleware."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from api.ratelimit import (
    IP_RATE_LIMIT,
    IP_RATE_WINDOW_SECONDS,
    _check_ip_rate_limit,
    _check_api_key_quota,
)


class FakePipeline:
    """Async pipeline double that collects commands and returns results."""

    def __init__(self, results: list, *, execute_error: Exception | None = None):
        self._results = results
        self._execute_error = execute_error

    def incr(self, key: str):
        pass  # commands are no-ops; results come from __init__

    def expire(self, key: str, ttl: int):
        pass

    def ttl(self, key: str):
        pass

    async def execute(self):
        if self._execute_error is not None:
            raise self._execute_error
        return self._results


class FakeRedis:
    """Small async Redis double for rate-limit tests (pipeline-based)."""

    def __init__(
        self,
        *,
        incr_result: int | None = None,
        ttl_result: int = 0,
        pipeline_error: Exception | None = None,
    ) -> None:
        self.incr_result = incr_result
        self.ttl_result = ttl_result
        self.pipeline_error = pipeline_error

    def pipeline(self):
        return FakePipeline(
            [self.incr_result or 0, True, self.ttl_result],
            execute_error=self.pipeline_error,
        )


class FakeMappings:
    """Minimal SQLAlchemy-style mappings wrapper."""

    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class FakeResult:
    """Minimal SQLAlchemy-style result wrapper."""

    def __init__(self, row):
        self.row = row

    def mappings(self):
        return FakeMappings(self.row)


class FakeSession:
    """Async DB session double with queued execute results."""

    def __init__(self, rows: list[dict | None] | None = None, *, execute_error: Exception | None = None):
        self.rows = list(rows or [])
        self.execute_error = execute_error
        self.execute_calls: list[tuple[tuple, dict]] = []
        self.committed = False

    async def execute(self, *args, **kwargs):
        if self.execute_error is not None:
            raise self.execute_error
        self.execute_calls.append((args, kwargs))
        row = self.rows.pop(0) if self.rows else None
        return FakeResult(row)

    async def commit(self) -> None:
        self.committed = True


# ---------------------------------------------------------------------------
# Per-IP rate limiting
# ---------------------------------------------------------------------------

def test_ip_rate_limit_allows_under_limit():
    """Requests under the limit should be allowed."""
    redis = FakeRedis(incr_result=1, ttl_result=60)

    allowed, remaining, retry_after = asyncio.run(_check_ip_rate_limit(redis, "1.2.3.4"))
    assert allowed is True
    assert remaining == IP_RATE_LIMIT - 1
    assert retry_after == 0


def test_ip_rate_limit_blocks_over_limit():
    """Requests over the limit should be blocked."""
    redis = FakeRedis(incr_result=IP_RATE_LIMIT + 1, ttl_result=45)

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
    redis = FakeRedis(pipeline_error=Exception("connection lost"))

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
        _check_api_key_quota(mock_factory, "test-api-key")
    )
    assert allowed is True
    assert retry_after is None
    mock_factory.assert_not_called()


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
    mock_session = FakeSession(rows=[None])

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is True


def test_api_key_quota_inactive_key():
    """Inactive DB keys should be rejected."""
    mock_session = FakeSession(
        rows=[
            {
                "query_count": 0,
                "monthly_limit": 1000,
                "is_active": False,
            }
        ]
    )

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is False


def test_api_key_quota_exhausted():
    """Keys at their monthly limit should be rejected with retry_after."""
    mock_session = FakeSession(
        rows=[
            {
                "query_count": 1000,
                "monthly_limit": 1000,
                "is_active": True,
            }
        ]
    )

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is False
    assert retry_after is not None
    assert retry_after > 0


def test_api_key_quota_under_limit_increments():
    """Keys under their limit should be allowed and count incremented."""
    mock_session = FakeSession(
        rows=[
            {
                "query_count": 50,
                "monthly_limit": 1000,
                "is_active": True,
            },
            None,
        ]
    )

    factory = _make_async_session_factory(mock_session)
    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
    )
    assert allowed is True
    # Should have called execute twice (SELECT + UPDATE)
    assert len(mock_session.execute_calls) == 2
    assert mock_session.committed is True


def test_api_key_quota_db_error_fails_open():
    """DB errors during quota check should fail open."""
    broken_session = FakeSession(execute_error=Exception("DB down"))
    factory = _make_async_session_factory(broken_session)

    allowed, retry_after = asyncio.run(
        _check_api_key_quota(factory, "unknown-key-not-in-config")
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
    async def _mock_ip_blocked(*args, **kwargs):
        return (False, 0, 30)

    with patch("api.ratelimit._check_ip_rate_limit", new=_mock_ip_blocked):
        response = client.get("/v1/ontology", headers=api_key_headers)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"] == "IP rate limit exceeded"


def test_api_key_quota_returns_429(client, api_key_headers):
    """Exhausted API key quota should return 429 with Retry-After."""
    async def _mock_ip_allowed(*args, **kwargs):
        return (True, 99, 0)

    async def _mock_key_exhausted(*args, **kwargs):
        return (False, 86400)

    with patch("api.ratelimit._check_ip_rate_limit", new=_mock_ip_allowed):
        with patch("api.ratelimit._check_api_key_quota", new=_mock_key_exhausted):
            response = client.get("/v1/ontology", headers=api_key_headers)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "86400"
    assert response.json()["detail"] == "API key quota exhausted"
