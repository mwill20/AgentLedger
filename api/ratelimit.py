"""Rate limiting middleware for AgentLedger Layer 1.

Two layers:
1. Per-IP: 100 requests/minute via Redis sliding window
2. Per-API-key: monthly quota enforcement via api_keys table

Uses a pure ASGI middleware instead of Starlette's BaseHTTPMiddleware to
avoid the per-request thread overhead that degrades throughput under high
concurrency.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from api.config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IP_RATE_LIMIT = settings.ip_rate_limit
IP_RATE_WINDOW_SECONDS = settings.ip_rate_window_seconds
EXEMPT_PATHS = {"/v1/health", "/docs", "/openapi.json", "/redoc"}


# ---------------------------------------------------------------------------
# Per-IP rate limiting (Redis-backed)
# ---------------------------------------------------------------------------

async def _check_ip_rate_limit(redis_client: Any, client_ip: str) -> tuple[bool, int, int]:
    """Check per-IP rate limit using a Redis pipeline (single round-trip).

    Returns (allowed, remaining, retry_after_seconds).
    """
    if redis_client is None or redis_client.__class__.__name__ == "NullRedisClient":
        return True, IP_RATE_LIMIT, 0
    if IP_RATE_LIMIT <= 0:
        return True, 0, 0

    key = f"ratelimit:ip:{client_ip}"
    try:
        pipe = redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, IP_RATE_WINDOW_SECONDS)
        pipe.ttl(key)
        current, _expire_ok, ttl = await pipe.execute()

        remaining = max(0, IP_RATE_LIMIT - current)
        if current > IP_RATE_LIMIT:
            return False, 0, max(1, ttl)
        return True, remaining, 0
    except Exception:
        # Redis failure should not block requests
        return True, IP_RATE_LIMIT, 0


# ---------------------------------------------------------------------------
# Per-API-key quota enforcement (DB-backed)
# ---------------------------------------------------------------------------

async def _check_api_key_quota(session_factory: Any, api_key: str) -> tuple[bool, int | None]:
    """Check and increment the api_keys query_count.

    Returns (allowed, retry_after_seconds).
    For config-based keys (not in DB), always allows.
    """
    if session_factory is None:
        return True, None

    # Config-based keys bypass DB quota check
    configured = {k.strip() for k in settings.api_keys.split(",") if k.strip()}
    if api_key in configured:
        return True, None

    key_hash = sha256(api_key.encode("utf-8")).hexdigest()

    try:
        from sqlalchemy import text

        async with session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT query_count, monthly_limit, is_active
                    FROM api_keys
                    WHERE key_hash = :key_hash
                    """
                ),
                {"key_hash": key_hash},
            )
            row = result.mappings().first()
            if row is None:
                # Key not in DB — auth middleware will reject it
                return True, None

            if not row["is_active"]:
                return False, None

            if row["monthly_limit"] is not None and row["query_count"] >= row["monthly_limit"]:
                # Calculate seconds until next month (approximate: rest of current month)
                import calendar
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                days_in_month = calendar.monthrange(now.year, now.month)[1]
                remaining_days = days_in_month - now.day + 1
                retry_after = remaining_days * 86400
                return False, retry_after

            # Increment query count and update last_used_at
            await session.execute(
                text(
                    """
                    UPDATE api_keys
                    SET query_count = query_count + 1,
                        last_used_at = NOW()
                    WHERE key_hash = :key_hash
                    """
                ),
                {"key_hash": key_hash},
            )
            await session.commit()
            return True, None
    except Exception:
        # DB failure should not block requests
        return True, None


# ---------------------------------------------------------------------------
# Pure ASGI Middleware (replaces BaseHTTPMiddleware for performance)
# ---------------------------------------------------------------------------

def _parse_scope_path(scope: dict) -> str:
    """Extract the URL path from an ASGI scope."""
    return scope.get("path", "/")


def _get_client_ip(scope: dict) -> str:
    """Extract client IP from an ASGI scope."""
    client = scope.get("client")
    return client[0] if client else "unknown"


def _get_header(scope: dict, name: bytes) -> str | None:
    """Extract a single header value from ASGI scope headers."""
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _send_json_response(
    send,
    status_code: int,
    body: dict,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """Send a complete JSON response via the ASGI send callable."""
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class RateLimitMiddleware:
    """Pure ASGI rate-limiting middleware.

    Unlike Starlette's ``BaseHTTPMiddleware``, this does **not** spawn a
    background thread per request and does not buffer the response body,
    which eliminates a major source of thread contention under load.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = _parse_scope_path(scope)

        # Skip rate limiting for exempt paths
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # --- Per-IP check ---
        from api.dependencies import redis_client

        client_ip = _get_client_ip(scope)
        ip_allowed, ip_remaining, ip_retry_after = await _check_ip_rate_limit(
            redis_client, client_ip
        )

        if not ip_allowed:
            await _send_json_response(
                send,
                429,
                {"detail": "IP rate limit exceeded"},
                {
                    "Retry-After": str(ip_retry_after),
                    "X-RateLimit-Limit": str(IP_RATE_LIMIT),
                    "X-RateLimit-Remaining": "0",
                },
            )
            return

        # --- Per-API-key quota check ---
        api_key = _get_header(scope, b"x-api-key")
        if api_key:
            from api.dependencies import async_session_factory

            key_allowed, key_retry_after = await _check_api_key_quota(
                async_session_factory, api_key
            )

            if not key_allowed:
                await _send_json_response(
                    send,
                    429,
                    {"detail": "API key quota exhausted"},
                    {"Retry-After": str(key_retry_after or 86400)},
                )
                return

        # --- Proceed: inject rate-limit headers into response ---
        rl_limit = str(IP_RATE_LIMIT)
        rl_remaining = str(ip_remaining)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", rl_limit.encode()))
                headers.append((b"x-ratelimit-remaining", rl_remaining.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
