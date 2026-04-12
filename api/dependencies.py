"""Shared dependencies: database session and Redis client."""

from collections.abc import AsyncGenerator
from hashlib import sha256

from fastapi import Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.config import settings

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover - optional in local test environments
    aioredis = None

# Async SQLAlchemy engine and session factory
try:
    engine = create_async_engine(settings.database_url, echo=False)
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
except ModuleNotFoundError:  # pragma: no cover - optional in local test environments
    engine = None
    async_session_factory = None

class NullRedisClient:
    """No-op Redis client used when redis-py is unavailable."""

    async def aclose(self) -> None:
        """Match the redis client close contract."""


# Redis client
redis_client = (
    aioredis.from_url(settings.redis_url, decode_responses=True)
    if aioredis is not None
    else NullRedisClient()
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    if async_session_factory is None:
        raise RuntimeError("database driver is not installed")
    async with async_session_factory() as session:
        yield session


async def get_redis():
    """Return the Redis client."""
    return redis_client


def _configured_api_keys() -> set[str]:
    """Parse configured API keys from settings."""
    return {key.strip() for key in settings.api_keys.split(",") if key.strip()}


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Validate the API key from either settings or the database."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-API-Key header",
        )

    configured = _configured_api_keys()
    if x_api_key in configured:
        return x_api_key

    key_hash = sha256(x_api_key.encode("utf-8")).hexdigest()
    if async_session_factory is not None:
        async with async_session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT is_active
                    FROM api_keys
                    WHERE key_hash = :key_hash
                    """
                ),
                {"key_hash": key_hash},
            )
            row = result.mappings().first()
            if row and row["is_active"]:
                return x_api_key

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid API key",
    )
