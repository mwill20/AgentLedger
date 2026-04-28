"""Layer 3 attestation and auditor endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_api_key
from api.models.layer3 import (
    AttestationCreateRequest,
    AttestationCreateResponse,
    AttestationRecord,
    AttestationVerifyResponse,
    AuditorRecord,
    AuditorRegistrationRequest,
    AuditorRegistrationResponse,
    RevocationCreateRequest,
    RevocationCreateResponse,
)
from api.services import attestation, auditor

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post(
    "/auditors/register",
    response_model=AuditorRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_auditor(
    payload: AuditorRegistrationRequest,
    db: AsyncSession = Depends(get_db),
) -> AuditorRegistrationResponse:
    """Register or refresh one active auditor."""
    return await auditor.register_auditor(db=db, request=payload)


@router.get("/auditors", response_model=list[AuditorRecord])
async def list_auditors(
    db: AsyncSession = Depends(get_db),
) -> list[AuditorRecord]:
    """List all active auditors."""
    return await auditor.list_auditors(db=db)


@router.get("/auditors/{did_value}", response_model=AuditorRecord)
async def get_auditor(
    did_value: str,
    db: AsyncSession = Depends(get_db),
) -> AuditorRecord:
    """Resolve one auditor record."""
    return await auditor.get_auditor(db=db, did=did_value)


@router.post(
    "/attestations",
    response_model=AttestationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_attestation(
    payload: AttestationCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> AttestationCreateResponse:
    """Submit one Layer 3 service attestation."""
    return await attestation.submit_attestation(db=db, request=payload)


@router.post(
    "/attestations/revoke",
    response_model=RevocationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_revocation(
    payload: RevocationCreateRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> RevocationCreateResponse:
    """Submit one Layer 3 service revocation."""
    return await attestation.submit_revocation(db=db, request=payload, redis=redis)


@router.get("/attestations/{service_id}", response_model=list[AttestationRecord])
async def list_attestations(
    service_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[AttestationRecord]:
    """Return all active attestations for one service."""
    return await attestation.list_attestations_for_service(db=db, service_id=service_id)


@router.get(
    "/attestations/{service_id}/verify",
    response_model=AttestationVerifyResponse,
)
async def verify_attestations(
    service_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> AttestationVerifyResponse:
    """Cross-check the service attestation view against chain events."""
    return await attestation.verify_service_attestations(db=db, service_id=service_id)
