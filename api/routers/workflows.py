"""Layer 5 workflow registry endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_admin_api_key, require_api_key
from api.models.workflow import (
    BundleApproveRequest,
    BundleApproveResponse,
    BundleCreateRequest,
    BundleResponse,
    ExecutionReportRequest,
    ExecutionReportResponse,
    ValidationAssignRequest,
    ValidationResponse,
    ValidatorDecisionRequest,
    WorkflowCreateRequest,
    WorkflowCreateResponse,
    WorkflowListResponse,
    WorkflowRankResponse,
    WorkflowRecord,
)
from api.services import (
    workflow_context,
    workflow_ranker,
    workflow_registry,
    workflow_validator,
)

router = APIRouter(prefix="/workflows")


def _parse_tags(tags: str | None) -> list[str] | None:
    """Parse comma-separated workflow tag filters."""
    if not tags:
        return None
    parsed = [tag.strip() for tag in tags.split(",") if tag.strip()]
    return parsed or None


@router.post(
    "",
    response_model=WorkflowCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow(
    payload: WorkflowCreateRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> WorkflowCreateResponse:
    """Submit a workflow for validation."""
    del api_key
    return await workflow_registry.create_workflow(db=db, request=payload)


@router.put("/{workflow_id}", response_model=WorkflowRecord)
async def update_workflow(
    workflow_id: UUID,
    payload: WorkflowCreateRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRecord:
    """Replace a draft workflow spec."""
    del api_key
    return await workflow_registry.update_workflow_spec(
        db=db,
        workflow_id=workflow_id,
        request=payload,
    )


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    domain: str | None = None,
    tags: str | None = None,
    status_filter: str = Query(default="published", alias="status"),
    quality_min: float | None = Query(default=None, ge=0.0, le=100.0),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> WorkflowListResponse:
    """List workflows with optional filters."""
    del api_key
    return await workflow_registry.list_workflows(
        db=db,
        domain=domain,
        tags=_parse_tags(tags),
        status_filter=status_filter,
        quality_min=quality_min,
        limit=limit,
        offset=offset,
    )


@router.get("/slug/{slug}", response_model=WorkflowRecord)
async def get_workflow_by_slug(
    slug: str,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRecord:
    """Retrieve a workflow by slug."""
    del api_key
    return await workflow_registry.get_workflow_by_slug(db=db, slug=slug)


@router.post(
    "/context/bundle",
    response_model=BundleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow_context_bundle(
    payload: BundleCreateRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> BundleResponse:
    """Create a workflow-level context bundle for one approval interaction."""
    del api_key
    return await workflow_context.create_context_bundle_from_request(
        request=payload,
        db=db,
        redis=redis,
    )


@router.post(
    "/context/bundle/{bundle_id}/approve",
    response_model=BundleApproveResponse,
)
async def approve_workflow_context_bundle(
    bundle_id: UUID,
    payload: BundleApproveRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> BundleApproveResponse:
    """Approve a pending workflow-level context bundle."""
    del api_key
    return await workflow_context.approve_context_bundle(
        bundle_id=bundle_id,
        request=payload,
        db=db,
    )


@router.get("/{workflow_id}/rank", response_model=WorkflowRankResponse)
async def rank_workflow(
    workflow_id: UUID,
    geo: str | None = None,
    pricing_model: str | None = None,
    agent_did: str | None = None,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> WorkflowRankResponse:
    """Return ranked service candidates for each workflow step."""
    del api_key
    return await workflow_ranker.get_workflow_rank(
        workflow_id=workflow_id,
        geo=geo,
        pricing_model=pricing_model,
        agent_did=agent_did,
        db=db,
        redis=redis,
    )


@router.get("/{workflow_id}", response_model=WorkflowRecord)
async def get_workflow(
    workflow_id: UUID,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRecord:
    """Retrieve a workflow by UUID."""
    del api_key
    return await workflow_registry.get_workflow(db=db, workflow_id=workflow_id)


@router.post(
    "/{workflow_id}/executions",
    response_model=ExecutionReportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def report_workflow_execution(
    workflow_id: UUID,
    payload: ExecutionReportRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ExecutionReportResponse:
    """Report a workflow execution outcome and update quality signals."""
    del api_key
    return await workflow_registry.report_execution(
        db=db,
        workflow_id=workflow_id,
        request=payload,
        redis=redis,
    )


@router.post("/{workflow_id}/validate", response_model=ValidationResponse)
async def assign_workflow_validation(
    workflow_id: UUID,
    payload: ValidationAssignRequest,
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> ValidationResponse:
    """Assign a draft workflow to a validator."""
    del admin_api_key
    return await workflow_validator.assign_workflow_to_validator(
        db=db,
        workflow_id=workflow_id,
        request=payload,
    )


@router.put("/{workflow_id}/validation", response_model=WorkflowRecord)
async def record_workflow_validation(
    workflow_id: UUID,
    payload: ValidatorDecisionRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRecord:
    """Record a validator decision for a workflow."""
    del api_key
    return await workflow_validator.record_validator_decision(
        db=db,
        workflow_id=workflow_id,
        request=payload,
    )
