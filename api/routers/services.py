"""GET /services endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_api_key
from api.models.service import ServiceDetail, ServiceSearchResponse
from api.services import registry

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/services", response_model=ServiceSearchResponse)
async def list_services(
    ontology: str,
    trust_min: float = 0,
    trust_tier_min: int = 1,
    geo: str | None = None,
    pricing_model: str | None = None,
    latency_max_ms: int | None = None,
    limit: int = 10,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ServiceSearchResponse:
    """Return structured search results for a specific ontology tag."""
    return await registry.query_services(
        db=db,
        redis=redis,
        ontology=ontology,
        trust_min=trust_min,
        trust_tier_min=trust_tier_min,
        geo=geo,
        pricing_model=pricing_model,
        latency_max_ms=latency_max_ms,
        limit=limit,
        offset=offset,
    )


@router.get("/services/{service_id}", response_model=ServiceDetail)
async def get_service(
    service_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ServiceDetail:
    """Return the full record for a single service."""
    return await registry.get_service_detail(db=db, service_id=service_id)
