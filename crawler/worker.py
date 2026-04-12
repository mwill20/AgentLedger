"""Celery worker entry point."""

from __future__ import annotations

try:
    from celery import Celery
except ImportError:  # pragma: no cover - optional in local test environments
    Celery = None

from api.config import settings


def create_celery_app() -> Celery | None:
    """Create the Celery app if Celery is installed."""
    if Celery is None:
        return None

    app = Celery("agentledger", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
    )
    app.autodiscover_tasks(["crawler.tasks"])
    return app


celery_app = create_celery_app()
