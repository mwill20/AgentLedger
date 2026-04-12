"""Vector B — DNS TXT domain verification task.

Checks DNS TXT records for `agentledger-verify={service_id}`.
On match: promotes trust_tier 1→2 and logs the event.
Retries daily for up to 30 days.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from api.services.verifier import expected_dns_txt_token, verify_txt_records
from crawler.worker import celery_app, get_sync_connection

logger = logging.getLogger(__name__)

VERIFICATION_MAX_AGE_DAYS = 30


# ---------------------------------------------------------------------------
# DNS resolution
# ---------------------------------------------------------------------------

def _resolve_txt_records(domain: str) -> list[str]:
    """Resolve DNS TXT records for a domain.

    Returns a list of TXT record strings. Returns an empty list if
    resolution fails (NXDOMAIN, timeout, no records, etc.).
    """
    try:
        import dns.resolver

        answers = dns.resolver.resolve(domain, "TXT")
        records = []
        for rdata in answers:
            for txt_string in rdata.strings:
                records.append(txt_string.decode("utf-8", errors="replace"))
        return records
    except Exception as exc:
        logger.debug("DNS TXT lookup failed for %s: %s", domain, exc)
        return []


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _log_verification_event(
    conn,
    service_id: str,
    domain: str,
    event_type: str,
    details: dict | None = None,
) -> None:
    """Write a verification event to crawl_events."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_events (service_id, event_type, domain, details)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (service_id, event_type, domain, json.dumps(details or {})),
        )
    conn.commit()


def _promote_trust_tier(conn, service_id: str, new_tier: int) -> None:
    """Upgrade a service's trust_tier and record verification timestamp."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE services
            SET trust_tier = %s,
                last_verified_at = NOW(),
                updated_at = NOW()
            WHERE id = %s AND trust_tier < %s
            """,
            (new_tier, service_id, new_tier),
        )
    conn.commit()


def _get_service_info(conn, service_id: str) -> dict | None:
    """Fetch current trust_tier and first_seen_at for a service."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trust_tier, first_seen_at FROM services WHERE id = %s",
            (service_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"trust_tier": row[0], "first_seen_at": row[1]}


# ---------------------------------------------------------------------------
# Core verification logic
# ---------------------------------------------------------------------------

def evaluate_domain_verification(service_id: UUID | str, txt_records: list[str]) -> bool:
    """Evaluate a set of TXT records for a service."""
    return verify_txt_records(service_id, txt_records)


def _verify_domain_impl(domain: str, service_id: str) -> dict:
    """Perform DNS TXT verification for a service.

    1. Check if service exists and is eligible (trust_tier == 1)
    2. Resolve DNS TXT records
    3. If match found: promote to trust_tier 2
    4. Log the event either way
    """
    conn = get_sync_connection()
    try:
        service_info = _get_service_info(conn, service_id)
        if service_info is None:
            return {
                "service_id": service_id,
                "domain": domain,
                "status": "service_not_found",
            }

        # Already verified — skip
        if service_info["trust_tier"] >= 2:
            return {
                "service_id": service_id,
                "domain": domain,
                "status": "already_verified",
                "trust_tier": service_info["trust_tier"],
            }

        # Check if we've exceeded the 30-day verification window
        first_seen = service_info["first_seen_at"]
        if first_seen:
            age_days = (datetime.now(timezone.utc) - first_seen).days
            if age_days > VERIFICATION_MAX_AGE_DAYS:
                _log_verification_event(conn, service_id, domain, "verification_expired", {
                    "age_days": age_days,
                    "max_days": VERIFICATION_MAX_AGE_DAYS,
                })
                return {
                    "service_id": service_id,
                    "domain": domain,
                    "status": "verification_window_expired",
                    "age_days": age_days,
                }

        # Resolve DNS TXT records
        txt_records = _resolve_txt_records(domain)
        expected_token = expected_dns_txt_token(service_id)

        if evaluate_domain_verification(service_id, txt_records):
            # SUCCESS — promote trust_tier 1→2
            _promote_trust_tier(conn, service_id, new_tier=2)
            _log_verification_event(conn, service_id, domain, "domain_verified", {
                "expected_token": expected_token,
                "matched": True,
                "txt_records_found": len(txt_records),
            })
            logger.info("Domain verified for service %s (%s) — trust_tier → 2", service_id, domain)
            return {
                "service_id": service_id,
                "domain": domain,
                "status": "verified",
                "trust_tier": 2,
            }
        else:
            # No match — log and retry on next beat cycle
            _log_verification_event(conn, service_id, domain, "verification_pending", {
                "expected_token": expected_token,
                "matched": False,
                "txt_records_found": len(txt_records),
            })
            return {
                "service_id": service_id,
                "domain": domain,
                "status": "pending",
                "txt_records_found": len(txt_records),
            }

    finally:
        conn.close()


def _verify_all_pending_impl() -> dict:
    """Verify all services still at trust_tier 1. Called by beat schedule."""
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, domain FROM services WHERE trust_tier = 1 AND is_active = true AND is_banned = false"
            )
            services = cur.fetchall()
    finally:
        conn.close()

    results = []
    for service_id, domain in services:
        result = _verify_domain_impl(domain, str(service_id))
        results.append(result)

    return {"total": len(results), "results": results}


# ---------------------------------------------------------------------------
# Enqueueing (called from FastAPI app after manifest registration)
# ---------------------------------------------------------------------------

def enqueue_domain_verification(domain: str, service_id: UUID | str) -> bool:
    """Queue domain verification when Celery is available."""
    if celery_app is None:
        return False
    verify_domain_task.delay(domain, str(service_id))
    return True


# ---------------------------------------------------------------------------
# Celery task registration
# ---------------------------------------------------------------------------

if celery_app is not None:

    @celery_app.task(name="crawler.verify_domain")
    def verify_domain_task(domain: str, service_id: str) -> dict:
        """Verify DNS TXT record for a single service."""
        return _verify_domain_impl(domain, service_id)

    @celery_app.task(name="crawler.verify_all_pending")
    def verify_all_pending_task() -> dict:
        """Verify all pending services (called by beat schedule)."""
        return _verify_all_pending_impl()

else:

    def verify_domain_task(domain: str, service_id: str) -> dict:
        """Fallback when Celery is unavailable."""
        return _verify_domain_impl(domain, service_id)

    def verify_all_pending_task() -> dict:
        """Fallback when Celery is unavailable."""
        return _verify_all_pending_impl()
