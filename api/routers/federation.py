"""Layer 3 federation endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_api_key
from api.models.layer3 import (
    FederationBlocklistResponse,
    FederationRegistrySubscribeRequest,
    FederationRegistrySubscribeResponse,
    FederationRevocationSubmitRequest,
    FederationRevocationSubmitResponse,
)
from api.services import federation

router = APIRouter()


@router.get(
    "/.well-known/agentledger-blocklist.json",
    response_model=FederationBlocklistResponse,
)
async def get_well_known_blocklist(
    db: AsyncSession = Depends(get_db),
) -> FederationBlocklistResponse:
    """Expose the current blocklist at a lightweight discovery path."""
    return await federation.get_blocklist(db=db, page=1, limit=1000, since=None)


@router.get("/federation/blocklist", response_model=FederationBlocklistResponse)
async def get_blocklist(
    page: int = 1,
    limit: int = 50,
    since: datetime | None = None,
    db: AsyncSession = Depends(get_db),
) -> FederationBlocklistResponse:
    """Return the confirmed global revocation list."""
    return await federation.get_blocklist(db=db, page=page, limit=limit, since=since)


@router.get("/federation/blocklist/stream")
async def stream_blocklist(
    since: datetime | None = None,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Return a simple SSE feed for the current blocklist snapshot."""
    return StreamingResponse(
        federation.stream_blocklist(db=db, since=since),
        media_type="text/event-stream",
    )


@router.post(
    "/federation/registries/subscribe",
    response_model=FederationRegistrySubscribeResponse,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def subscribe_registry(
    payload: FederationRegistrySubscribeRequest,
    db: AsyncSession = Depends(get_db),
) -> FederationRegistrySubscribeResponse:
    """Register one federated registry subscriber."""
    return await federation.subscribe_registry(db=db, request=payload)


@router.post(
    "/federation/revocations/submit",
    response_model=FederationRevocationSubmitResponse,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
async def submit_federated_revocation(
    payload: FederationRevocationSubmitRequest,
    db: AsyncSession = Depends(get_db),
) -> FederationRevocationSubmitResponse:
    """Accept one federated revocation for review."""
    return await federation.submit_federated_revocation(db=db, request=payload)
