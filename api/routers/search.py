"""POST /search endpoint."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_api_key
from api.models.query import SearchRequest
from api.models.service import ServiceSearchResponse
from api.services import registry

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/search", response_model=ServiceSearchResponse)
async def search_services(
    request: SearchRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ServiceSearchResponse:
    """Run a semantic search over registered services."""
    if not request.query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="query must not be empty",
        )
    return await registry.search_services(db=db, redis=redis, request=request)
