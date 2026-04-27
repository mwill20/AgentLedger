"""Layer 3 audit chain endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_api_key
from api.models.layer3 import (
    AuditRecordCreateRequest,
    AuditRecordCreateResponse,
    AuditRecordDetailResponse,
    AuditRecordListResponse,
    AuditRecordVerifyResponse,
)
from api.services import audit

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post(
    "/audit/records",
    response_model=AuditRecordCreateResponse,
    status_code=201,
)
async def create_audit_record(
    payload: AuditRecordCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> AuditRecordCreateResponse:
    """Create one audit record pending batch anchoring."""
    return await audit.create_audit_record(db=db, request=payload)


@router.get("/audit/records/{record_id}", response_model=AuditRecordDetailResponse)
async def get_audit_record(
    record_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> AuditRecordDetailResponse:
    """Return one stored audit record."""
    return await audit.get_audit_record(db=db, record_id=record_id)


@router.get(
    "/audit/records/{record_id}/verify",
    response_model=AuditRecordVerifyResponse,
)
async def verify_audit_record(
    record_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> AuditRecordVerifyResponse:
    """Verify one audit record hash and Merkle proof."""
    return await audit.verify_audit_record(db=db, record_id=record_id)


@router.get("/audit/records", response_model=AuditRecordListResponse)
async def list_audit_records(
    agent_did: str | None = None,
    service_id: UUID | None = None,
    ontology_tag: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> AuditRecordListResponse:
    """Query Layer 3 audit history."""
    return await audit.list_audit_records(
        db=db,
        agent_did=agent_did,
        service_id=service_id,
        ontology_tag=ontology_tag,
        from_date=from_date,
        to_date=to_date,
        outcome=outcome,
        limit=limit,
        offset=offset,
    )
