"""Vector B domain verification helpers."""

from __future__ import annotations

from uuid import UUID

from api.services.verifier import expected_dns_txt_token, verify_txt_records
from crawler.worker import celery_app


def evaluate_domain_verification(service_id: UUID | str, txt_records: list[str]) -> bool:
    """Evaluate a set of TXT records for a service."""
    return verify_txt_records(service_id, txt_records)


def enqueue_domain_verification(domain: str, service_id: UUID | str) -> bool:
    """Queue domain verification when Celery is available."""
    if celery_app is None:
        return False
    verify_domain_task.delay(domain, str(service_id))
    return True


if celery_app is not None:

    @celery_app.task(name="crawler.verify_domain")
    def verify_domain_task(domain: str, service_id: str) -> dict[str, str]:
        """Return the expected TXT token for out-of-process verification."""
        return {"domain": domain, "expected_txt": expected_dns_txt_token(service_id)}
else:

    def verify_domain_task(domain: str, service_id: str) -> dict[str, str]:
        """Fallback task implementation when Celery is unavailable."""
        return {"domain": domain, "expected_txt": expected_dns_txt_token(service_id)}
