"""Layer 3 chain status and indexed event endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_api_key
from api.models.layer3 import ChainEventsResponse, ChainStatusResponse
from api.services import chain

router = APIRouter()


@router.get("/chain/status", response_model=ChainStatusResponse)
async def get_chain_status(
    tx_hash: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> ChainStatusResponse:
    """Return current Layer 3 chain status."""
    return await chain.get_chain_status_for_tx(db=db, tx_hash=tx_hash)


@router.get(
    "/chain/events",
    response_model=ChainEventsResponse,
    dependencies=[Depends(require_api_key)],
)
async def list_chain_events(
    service_id: UUID | None = None,
    event_type: str | None = None,
    from_block: int | None = None,
    to_block: int | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> ChainEventsResponse:
    """Query indexed Layer 3 chain events."""
    return await chain.list_chain_events(
        db=db,
        service_id=service_id,
        event_type=event_type,
        from_block=from_block,
        to_block=to_block,
        limit=limit,
    )
