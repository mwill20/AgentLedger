"""Periodic Layer 3 chain confirmation task."""

from __future__ import annotations

import asyncio

from api.services import chain
from crawler.tasks._async_db import run_with_fresh_session
from crawler.worker import celery_app


async def _confirm_chain_events_async() -> dict[str, int]:
    return await run_with_fresh_session(chain.confirm_pending_events)


def _confirm_chain_events_impl() -> dict[str, int]:
    return asyncio.run(_confirm_chain_events_async())


if celery_app is not None:

    @celery_app.task(name="crawler.confirm_chain_events")
    def confirm_chain_events_task() -> dict[str, int]:
        """Confirm synthetic Layer 3 chain events past the safety window."""
        return _confirm_chain_events_impl()

else:

    def confirm_chain_events_task() -> dict[str, int]:
        """Fallback when Celery is unavailable."""
        return _confirm_chain_events_impl()
