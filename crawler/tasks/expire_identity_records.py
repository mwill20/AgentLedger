"""Expire pending authorization requests and prune expired session assertions."""

from __future__ import annotations

from crawler.worker import celery_app, get_sync_connection


def _expire_identity_records_impl() -> dict[str, int]:
    """Expire pending HITL requests and delete expired session assertions."""
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE authorization_requests
                SET status = 'expired'
                WHERE status = 'pending'
                  AND expires_at <= NOW()
                """
            )
            expired_authorizations = cur.rowcount

            cur.execute(
                """
                DELETE FROM session_assertions
                WHERE expires_at <= NOW()
                """
            )
            pruned_sessions = cur.rowcount

        conn.commit()
        return {
            "expired_authorizations": expired_authorizations,
            "pruned_sessions": pruned_sessions,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if celery_app is not None:

    @celery_app.task(name="crawler.expire_identity_records")
    def expire_identity_records() -> dict[str, int]:
        """Celery entry point for Layer 2 session and auth expiry."""
        return _expire_identity_records_impl()
