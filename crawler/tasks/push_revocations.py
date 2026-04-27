"""Periodic Layer 3 revocation fan-out task."""

from __future__ import annotations

import asyncio

from api.services import federation
from crawler.tasks._async_db import run_with_fresh_session
from crawler.worker import celery_app


async def _push_revocations_async() -> dict[str, int]:
    return await run_with_fresh_session(federation.dispatch_revocation_pushes)


def _push_revocations_impl() -> dict[str, int]:
    return asyncio.run(_push_revocations_async())


if celery_app is not None:

    @celery_app.task(name="crawler.push_revocations")
    def push_revocations_task() -> dict[str, int]:
        """Push confirmed Layer 3 revocations to subscribers."""
        return _push_revocations_impl()

else:

    def push_revocations_task() -> dict[str, int]:
        """Fallback when Celery is unavailable."""
        return _push_revocations_impl()
