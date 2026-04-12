"""Periodic task schedule."""

from __future__ import annotations

CRAWL_SCHEDULE = {
    "standard-path-crawl": {"schedule": 60 * 60 * 24},
    "domain-verification-retry": {"schedule": 60 * 60 * 24},
}
