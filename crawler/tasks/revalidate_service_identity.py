"""Nightly revalidation for active service identities."""

from __future__ import annotations

import asyncio
import json

from api.models.manifest import ServiceManifest
from api.services import service_identity
from crawler.worker import celery_app, get_sync_connection


def _revalidate_service_identity_impl() -> dict[str, int]:
    """Revalidate signed manifests for services with active identity state."""
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.domain, m.raw_json
                FROM services s
                JOIN manifests m
                    ON m.service_id = s.id
                   AND m.is_current = true
                WHERE s.last_verified_at IS NOT NULL
                """
            )
            rows = cur.fetchall()

            checked = 0
            revalidated = 0
            failed = 0
            for service_id, domain, raw_json in rows:
                checked += 1
                try:
                    manifest = ServiceManifest.model_validate(raw_json)
                    asyncio.run(
                        service_identity.validate_signed_manifest(
                            manifest=manifest,
                            force_refresh=True,
                        )
                    )
                    cur.execute(
                        """
                        UPDATE services
                        SET last_verified_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (service_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                        VALUES (%s, 'service_identity_revalidated', %s, '{}'::jsonb, NOW())
                        """,
                        (service_id, domain),
                    )
                    revalidated += 1
                except Exception as exc:
                    cur.execute(
                        """
                        INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                        VALUES (%s, 'service_identity_revalidation_failed', %s, %s::jsonb, NOW())
                        """,
                        (service_id, domain, json.dumps({"error": str(exc)})),
                    )
                    failed += 1

        conn.commit()
        return {"checked": checked, "revalidated": revalidated, "failed": failed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if celery_app is not None:

    @celery_app.task(name="crawler.revalidate_service_identity")
    def revalidate_service_identity() -> dict[str, int]:
        """Celery entry point for nightly did:web revalidation."""
        return _revalidate_service_identity_impl()
