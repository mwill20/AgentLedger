"""Layer 4 context profile endpoints."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_admin_api_key, require_api_key
from api.models.context import (
    ContextMatchRequest,
    ContextMatchResponse,
    ContextMismatchListResponse,
    ContextMismatchResolveRequest,
    ContextMismatchResolveResponse,
    ContextProfileCreateRequest,
    ContextProfileCreateResponse,
    ContextProfileRecord,
    ContextProfileUpdateRequest,
    DisclosureListResponse,
    DisclosurePackage,
    DisclosureRequest,
    DisclosureRevokeRequest,
    DisclosureRevokeResponse,
)
from api.services import (
    context_compliance,
    context_disclosure,
    context_matcher,
    context_mismatch,
    context_profiles,
)

router = APIRouter(prefix="/context")


@router.post(
    "/profiles",
    response_model=ContextProfileCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_context_profile(
    payload: ContextProfileCreateRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> ContextProfileCreateResponse:
    """Create a context profile for one registered agent DID."""
    del api_key
    return await context_profiles.create_profile(db=db, request=payload)


@router.get("/profiles/{agent_did}", response_model=ContextProfileRecord)
async def get_context_profile(
    agent_did: str,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ContextProfileRecord:
    """Retrieve the active context profile for an agent DID."""
    del api_key
    return await context_profiles.get_active_profile(
        db=db,
        agent_did=agent_did,
        redis=redis,
    )


@router.put("/profiles/{agent_did}", response_model=ContextProfileRecord)
async def update_context_profile(
    agent_did: str,
    payload: ContextProfileUpdateRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ContextProfileRecord:
    """Replace the active context profile rules for an agent DID."""
    del api_key
    return await context_profiles.update_active_profile(
        db=db,
        agent_did=agent_did,
        request=payload,
        redis=redis,
    )


@router.post("/match", response_model=ContextMatchResponse)
async def match_context(
    payload: ContextMatchRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ContextMatchResponse:
    """Evaluate whether requested context can be disclosed."""
    del api_key
    return await context_matcher.match_context_request(
        db=db,
        request=payload,
        redis=redis,
    )


@router.get("/compliance/export/{agent_did}")
async def export_context_compliance(
    agent_did: str,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Return a complete PDF compliance export for an agent."""
    del api_key
    pdf_bytes = await context_compliance.generate_compliance_pdf(
        db=db,
        agent_did=agent_did,
    )
    filename = context_compliance.compliance_export_filename(
        agent_did,
        datetime.now(timezone.utc),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/disclose", response_model=DisclosurePackage)
async def disclose_context(
    payload: DisclosureRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> DisclosurePackage:
    """Release committed-field nonces and write disclosure audit records."""
    del api_key
    return await context_disclosure.disclose_context(
        db=db,
        request=payload,
        redis=redis,
    )


@router.get("/disclosures/{agent_did}", response_model=DisclosureListResponse)
async def list_context_disclosures(
    agent_did: str,
    service_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> DisclosureListResponse:
    """List field-name-only disclosure audit records for an agent."""
    del api_key
    return await context_disclosure.list_disclosures(
        db=db,
        agent_did=agent_did,
        service_id=service_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )


@router.post("/revoke/{disclosure_id}", response_model=DisclosureRevokeResponse)
async def revoke_context_disclosure(
    disclosure_id: UUID,
    payload: DisclosureRevokeRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> DisclosureRevokeResponse:
    """Mark a disclosure erased while retaining the audit row."""
    del api_key
    return await context_disclosure.revoke_disclosure(
        db=db,
        disclosure_id=disclosure_id,
        request=payload,
    )


@router.get("/mismatches", response_model=ContextMismatchListResponse)
async def list_context_mismatches(
    service_id: UUID | None = None,
    severity: str | None = Query(default=None, pattern="^(warning|critical)$"),
    resolved: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> ContextMismatchListResponse:
    """List context mismatch events for admin review."""
    del admin_api_key
    return await context_mismatch.list_mismatches(
        db=db,
        service_id=service_id,
        severity=severity,
        resolved=resolved,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/mismatches/{mismatch_id}/resolve",
    response_model=ContextMismatchResolveResponse,
)
async def resolve_context_mismatch(
    mismatch_id: UUID,
    payload: ContextMismatchResolveRequest,
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> ContextMismatchResolveResponse:
    """Resolve a mismatch and optionally escalate it to trust revocation."""
    del admin_api_key
    return await context_mismatch.resolve_mismatch(
        db=db,
        mismatch_id=mismatch_id,
        request=payload,
    )
