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
    app.conf.include = [
        "crawler.tasks.crawl",
        "crawler.tasks.verify_domain",
    ]
    app.conf.beat_schedule = {
        "crawl-all-active-services": {
            "task": "crawler.crawl_all",
            "schedule": 60 * 60 * 24,  # every 24 hours
        },
        "verify-all-pending-domains": {
            "task": "crawler.verify_all_pending",
            "schedule": 60 * 60 * 24,  # every 24 hours
        },
    }
    return app


celery_app = create_celery_app()


def get_sync_connection():
    """Return a psycopg2 connection using the sync database URL.

    Celery workers are synchronous processes, so they use psycopg2
    rather than the async SQLAlchemy engine that the FastAPI app uses.
    """
    import psycopg2

    return psycopg2.connect(settings.database_url_sync)
