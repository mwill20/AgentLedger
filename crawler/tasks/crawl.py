"""Vector A — Standard path crawl task.

Fetches /.well-known/agent-manifest.json for registered services,
detects changes via SHA-256 hash comparison, and marks services
inactive after 3 consecutive failures.
"""

from __future__ import annotations

import json
import logging
from hashlib import sha256
from uuid import UUID

import httpx

from crawler.worker import celery_app, get_sync_connection

logger = logging.getLogger(__name__)

WELL_KNOWN_MANIFEST_PATH = "/.well-known/agent-manifest.json"
CRAWL_TIMEOUT_SECONDS = 15
MAX_CONSECUTIVE_FAILURES = 3


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without DB or network)
# ---------------------------------------------------------------------------

def build_manifest_url(domain: str) -> str:
    """Build the well-known manifest URL for a domain."""
    return f"https://{domain}{WELL_KNOWN_MANIFEST_PATH}"


def compute_manifest_hash(payload: dict) -> str:
    """Hash a manifest payload deterministically."""
    serialized = json.dumps(payload, sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()


def should_mark_service_inactive(consecutive_failures: int) -> bool:
    """Layer 1 marks a service inactive after three consecutive crawl failures."""
    return consecutive_failures >= MAX_CONSECUTIVE_FAILURES


# ---------------------------------------------------------------------------
# Database helpers (sync psycopg2 for Celery workers)
# ---------------------------------------------------------------------------

def _log_crawl_event(
    conn,
    service_id: str,
    domain: str,
    event_type: str,
    details: dict | None = None,
) -> None:
    """Write a row to crawl_events."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_events (service_id, event_type, domain, details)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (service_id, event_type, domain, json.dumps(details or {})),
        )
    conn.commit()


def _get_consecutive_failure_count(conn, service_id: str) -> int:
    """Count consecutive crawl_failure events (most recent first, stop at first non-failure)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_type
            FROM crawl_events
            WHERE service_id = %s
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (service_id,),
        )
        count = 0
        for (event_type,) in cur.fetchall():
            if event_type == "crawl_failure":
                count += 1
            else:
                break
        return count


def _update_service_after_crawl(
    conn,
    service_id: str,
    manifest_hash: str | None,
    is_active: bool,
) -> None:
    """Update the service record after a crawl attempt."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE services
            SET last_crawled_at = NOW(),
                is_active = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (is_active, service_id),
        )
        # If we have a new hash, update the manifests table
        if manifest_hash is not None:
            cur.execute(
                """
                SELECT manifest_hash
                FROM manifests
                WHERE service_id = %s AND is_current = true
                ORDER BY crawled_at DESC
                LIMIT 1
                """,
                (service_id,),
            )
            row = cur.fetchone()
            if row and row[0] != manifest_hash:
                # Manifest changed — mark old as non-current
                cur.execute(
                    "UPDATE manifests SET is_current = false WHERE service_id = %s AND is_current = true",
                    (service_id,),
                )
    conn.commit()


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

def _crawl_service_impl(service_id: str, domain: str) -> dict:
    """Actual crawl logic — fetch manifest, hash-compare, update DB."""
    url = build_manifest_url(domain)
    conn = get_sync_connection()

    try:
        response = httpx.get(url, timeout=CRAWL_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        manifest_hash = compute_manifest_hash(payload)

        _log_crawl_event(conn, service_id, domain, "crawl_success", {
            "url": url,
            "status_code": response.status_code,
            "manifest_hash": manifest_hash,
        })
        _update_service_after_crawl(conn, service_id, manifest_hash, is_active=True)

        return {
            "service_id": service_id,
            "domain": domain,
            "status": "success",
            "manifest_hash": manifest_hash,
        }

    except Exception as exc:
        logger.warning("Crawl failed for %s (%s): %s", domain, url, exc)
        _log_crawl_event(conn, service_id, domain, "crawl_failure", {
            "url": url,
            "error": str(exc),
        })

        failures = _get_consecutive_failure_count(conn, service_id)
        if should_mark_service_inactive(failures):
            _update_service_after_crawl(conn, service_id, None, is_active=False)
            logger.warning(
                "Service %s (%s) marked inactive after %d consecutive failures",
                service_id, domain, failures,
            )
            return {
                "service_id": service_id,
                "domain": domain,
                "status": "marked_inactive",
                "consecutive_failures": failures,
            }
        else:
            _update_service_after_crawl(conn, service_id, None, is_active=True)
            return {
                "service_id": service_id,
                "domain": domain,
                "status": "failure",
                "consecutive_failures": failures,
            }

    finally:
        conn.close()


def _crawl_all_impl() -> dict:
    """Crawl all active services. Called by beat schedule."""
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, domain FROM services WHERE is_active = true AND is_banned = false"
            )
            services = cur.fetchall()
    finally:
        conn.close()

    results = []
    for service_id, domain in services:
        result = _crawl_service_impl(str(service_id), domain)
        results.append(result)

    return {"total": len(results), "results": results}


# ---------------------------------------------------------------------------
# Celery task registration (conditional on Celery availability)
# ---------------------------------------------------------------------------

if celery_app is not None:

    @celery_app.task(name="crawler.crawl_service")
    def crawl_service_task(service_id: str, domain: str) -> dict:
        """Crawl a single service's manifest."""
        return _crawl_service_impl(service_id, domain)

    @celery_app.task(name="crawler.crawl_all")
    def crawl_all_task() -> dict:
        """Crawl all active services (called by beat schedule)."""
        return _crawl_all_impl()

else:
    # Fallback for environments without Celery (tests, local dev)
    def crawl_service_task(service_id: str, domain: str) -> dict:
        """Non-Celery fallback."""
        return _crawl_service_impl(service_id, domain)

    def crawl_all_task() -> dict:
        """Non-Celery fallback."""
        return _crawl_all_impl()
