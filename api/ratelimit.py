"""Rate limiting middleware for AgentLedger Layer 1.

Two layers:
1. Per-IP: 100 requests/minute via Redis sliding window
2. Per-API-key: monthly quota enforcement via api_keys table
"""

from __future__ import annotations

import time
from hashlib import sha256

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from api.config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IP_RATE_LIMIT = 100  # requests per window
IP_RATE_WINDOW_SECONDS = 60  # 1-minute sliding window
EXEMPT_PATHS = {"/v1/health", "/docs", "/openapi.json", "/redoc"}


# ---------------------------------------------------------------------------
# Per-IP rate limiting (Redis-backed)
# ---------------------------------------------------------------------------

async def _check_ip_rate_limit(redis, client_ip: str) -> tuple[bool, int, int]:
    """Check per-IP rate limit using Redis INCR + EXPIRE.

    Returns (allowed, remaining, retry_after_seconds).
    """
    if redis is None or hasattr(redis, "__class__") and redis.__class__.__name__ == "NullRedisClient":
        return True, IP_RATE_LIMIT, 0

    key = f"ratelimit:ip:{client_ip}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, IP_RATE_WINDOW_SECONDS)

        ttl = await redis.ttl(key)
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

async def _check_api_key_quota(session_factory, api_key: str) -> tuple[bool, int | None]:
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
# Middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware(BaseHTTPMiddleware):
    """Combined per-IP and per-API-key rate limiting."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip rate limiting for exempt paths
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # --- Per-IP check ---
        from api.dependencies import redis_client

        client_ip = request.client.host if request.client else "unknown"
        ip_allowed, ip_remaining, ip_retry_after = await _check_ip_rate_limit(
            redis_client, client_ip
        )

        if not ip_allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "IP rate limit exceeded"},
                headers={
                    "Retry-After": str(ip_retry_after),
                    "X-RateLimit-Limit": str(IP_RATE_LIMIT),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # --- Per-API-key quota check ---
        api_key = request.headers.get("X-API-Key")
        if api_key:
            from api.dependencies import async_session_factory

            key_allowed, key_retry_after = await _check_api_key_quota(
                async_session_factory, api_key
            )

            if not key_allowed:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "API key quota exhausted"},
                    headers={
                        "Retry-After": str(key_retry_after or 86400),
                    },
                )

        # --- Proceed ---
        response = await call_next(request)

        # Add rate limit headers to successful responses
        response.headers["X-RateLimit-Limit"] = str(IP_RATE_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(ip_remaining)
        return response
