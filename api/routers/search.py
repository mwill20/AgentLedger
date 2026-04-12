"""POST /search endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_api_key
from api.models.query import SearchRequest
from api.models.service import ServiceSearchResponse
from api.services import registry

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/search", response_model=ServiceSearchResponse)
async def search_services(
    request: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> ServiceSearchResponse:
    """Run a semantic search over registered services."""
    return await registry.search_services(db=db, request=request)
