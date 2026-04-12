"""Shared dependencies: database session and Redis client."""

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.config import settings

# Async SQLAlchemy engine and session factory
engine = create_async_engine(settings.database_url, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

# Redis client
redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    async with async_session_factory() as session:
        yield session


async def get_redis() -> aioredis.Redis:
    """Return the Redis client."""
    return redis_client
