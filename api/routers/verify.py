"""POST /services/{service_id}/verify — trigger domain verification."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from api.dependencies import require_api_key
from crawler.worker import get_sync_connection

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/services/{service_id}/verify")
async def verify_service_domain(service_id: UUID) -> dict:
    """Trigger DNS TXT verification for a service.

    Runs synchronously — resolves TXT records and updates trust_tier
    if the agentledger-verify token is found.
    """
    from crawler.tasks.verify_domain import _verify_domain_impl

    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT domain FROM services WHERE id = %s", (str(service_id),))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="service not found",
                )
            domain = row[0]
    finally:
        conn.close()

    return _verify_domain_impl(domain, str(service_id))
