"""Helpers for Celery tasks that need isolated async DB sessions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.config import settings

_T = TypeVar("_T")


async def run_with_fresh_session(
    operation: Callable[[AsyncSession], Awaitable[_T]],
) -> _T:
    """Run one async DB operation on a fresh engine/session pair.

    Celery background tasks are forked worker processes. Reusing the
    module-level async engine/session factory across those workers can lead to
    inherited asyncpg connection state. A per-task engine with ``NullPool``
    keeps these short periodic jobs isolated and deterministic.
    """
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await operation(session)
    finally:
        await engine.dispose()
