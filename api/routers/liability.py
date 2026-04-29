"""Layer 6 liability endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_admin_api_key, require_api_key
from api.models.liability import (
    AppealRequest,
    ClaimCreateRequest,
    ClaimDetailResponse,
    ClaimResponse,
    ComplianceExportType,
    DeterminationResponse,
    DetermineRequest,
    EvidenceGatherResponse,
    LiabilitySnapshotListResponse,
    LiabilitySnapshotRecord,
    ResolveRequest,
)
from api.services import (
    liability_attribution,
    liability_claims,
    liability_compliance,
    liability_snapshot,
)

router = APIRouter(prefix="/liability")


@router.get("/compliance/export")
async def export_liability_compliance(
    export_type: ComplianceExportType = Query(...),
    agent_did: str | None = None,
    execution_id: UUID | None = None,
    claim_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Generate a regulatory liability compliance PDF export."""
    del api_key
    pdf_bytes, filename = await liability_compliance.generate_liability_compliance_export(
        db=db,
        export_type=export_type,
        agent_did=agent_did,
        execution_id=execution_id,
        claim_id=claim_id,
        from_date=from_date,
        to_date=to_date,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/claims",
    response_model=ClaimResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_liability_claim(
    payload: ClaimCreateRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ClaimResponse:
    """File a liability claim against a workflow execution."""
    del api_key
    return await liability_claims.create_claim(
        execution_id=payload.execution_id,
        claimant_did=payload.claimant_did,
        claim_type=payload.claim_type,
        description=payload.description,
        harm_value_usd=payload.harm_value_usd,
        db=db,
        redis=redis,
        background_tasks=background_tasks,
    )


@router.get("/claims/{claim_id}", response_model=ClaimDetailResponse)
async def get_liability_claim(
    claim_id: UUID,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ClaimDetailResponse:
    """Retrieve a liability claim with evidence and latest determination."""
    del api_key
    return await liability_claims.get_claim_detail(
        claim_id=claim_id,
        db=db,
        redis=redis,
    )


@router.post("/claims/{claim_id}/gather", response_model=EvidenceGatherResponse)
async def gather_liability_claim_evidence(
    claim_id: UUID,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> EvidenceGatherResponse:
    """Manually trigger idempotent evidence gathering for a claim."""
    del api_key
    return await liability_claims.gather_evidence(
        claim_id=claim_id,
        db=db,
        redis=redis,
    )


@router.post("/claims/{claim_id}/resolve", response_model=ClaimResponse)
async def resolve_liability_claim(
    claim_id: UUID,
    payload: ResolveRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ClaimResponse:
    """Resolve a determined liability claim."""
    del api_key
    return await liability_claims.resolve_claim(
        claim_id=claim_id,
        resolution_note=payload.resolution_note,
        reviewer_did=payload.reviewer_did,
        db=db,
        redis=redis,
    )


@router.post("/claims/{claim_id}/appeal", response_model=ClaimResponse)
async def appeal_liability_claim(
    claim_id: UUID,
    payload: AppealRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ClaimResponse:
    """Appeal a determined liability claim and return it to review."""
    del api_key
    return await liability_claims.appeal_claim(
        claim_id=claim_id,
        appeal_reason=payload.appeal_reason,
        claimant_did=payload.claimant_did,
        db=db,
        redis=redis,
    )


@router.post("/claims/{claim_id}/determine", response_model=DeterminationResponse)
async def determine_liability_claim(
    claim_id: UUID,
    payload: DetermineRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> DeterminationResponse:
    """Compute liability attribution for a gathered claim."""
    del api_key
    return await liability_attribution.determine_claim(
        claim_id=claim_id,
        reviewer_did=payload.reviewer_did,
        db=db,
        redis=redis,
    )


@router.get(
    "/snapshots/{execution_id}",
    response_model=LiabilitySnapshotRecord,
)
async def get_liability_snapshot(
    execution_id: UUID,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> LiabilitySnapshotRecord:
    """Retrieve the liability snapshot for a workflow execution."""
    del api_key
    return await liability_snapshot.get_snapshot_by_execution(
        db=db,
        execution_id=execution_id,
    )


@router.get("/snapshots", response_model=LiabilitySnapshotListResponse)
async def list_liability_snapshots(
    workflow_id: UUID | None = None,
    agent_did: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> LiabilitySnapshotListResponse:
    """List liability snapshots for admin review."""
    del admin_api_key
    return await liability_snapshot.list_snapshots(
        db=db,
        workflow_id=workflow_id,
        agent_did=agent_did,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )
