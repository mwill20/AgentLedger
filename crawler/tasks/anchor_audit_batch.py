"""Periodic Layer 3 audit batch anchoring task."""

from __future__ import annotations

import asyncio

from api.services import audit
from crawler.tasks._async_db import run_with_fresh_session
from crawler.worker import celery_app


async def _anchor_audit_batch_async() -> dict[str, object]:
    return await run_with_fresh_session(audit.anchor_pending_records)


def _anchor_audit_batch_impl() -> dict[str, object]:
    return asyncio.run(_anchor_audit_batch_async())


if celery_app is not None:

    @celery_app.task(name="crawler.anchor_audit_batch")
    def anchor_audit_batch_task() -> dict[str, object]:
        """Anchor pending audit records into the next Layer 3 batch."""
        return _anchor_audit_batch_impl()

else:

    def anchor_audit_batch_task() -> dict[str, object]:
        """Fallback when Celery is unavailable."""
        return _anchor_audit_batch_impl()
