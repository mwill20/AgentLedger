"""Periodic Layer 3 remote chain indexing task."""

from __future__ import annotations

import asyncio

from api.services import chain
from crawler.tasks._async_db import run_with_fresh_session
from crawler.worker import celery_app


async def _index_chain_events_async() -> dict[str, int | str]:
    return await run_with_fresh_session(chain.poll_remote_chain_events)


def _index_chain_events_impl() -> dict[str, int | str]:
    return asyncio.run(_index_chain_events_async())


if celery_app is not None:

    @celery_app.task(name="crawler.index_chain_events")
    def index_chain_events_task() -> dict[str, int | str]:
        """Index remote Layer 3 chain events into the local query store."""
        return _index_chain_events_impl()

else:

    def index_chain_events_task() -> dict[str, int | str]:
        """Fallback when Celery is unavailable."""
        return _index_chain_events_impl()
