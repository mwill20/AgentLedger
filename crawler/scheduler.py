"""Periodic task schedule definitions.

These are wired into the Celery beat configuration in crawler/worker.py.
This module exists for documentation and reference.

Vector A: crawl all active services every 24 hours
Vector B: retry domain verification for pending services every 24 hours
"""

from __future__ import annotations

CRAWL_SCHEDULE = {
    "crawl-all-active-services": {
        "task": "crawler.crawl_all",
        "schedule": 60 * 60 * 24,  # every 24 hours
    },
    "verify-all-pending-domains": {
        "task": "crawler.verify_all_pending",
        "schedule": 60 * 60 * 24,  # every 24 hours
    },
    "expire-identity-records": {
        "task": "crawler.expire_identity_records",
        "schedule": 60,  # every minute
    },
}
