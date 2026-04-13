"""Layer 2 identity endpoints."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, get_redis, require_admin_api_key, require_api_key
from api.models.identity import (
    AgentIdentityResponse,
    AgentRegistrationRequest,
    AgentRegistrationResponse,
    AgentRevokeRequest,
    AgentRevokeResponse,
    CredentialVerificationRequest,
    CredentialVerificationResponse,
)
from api.services import identity

router = APIRouter()


@router.get("/identity/.well-known/did.json")
async def get_issuer_did_document() -> dict:
    """Expose AgentLedger's issuer DID document."""
    return identity.get_issuer_did_document()


@router.post(
    "/identity/agents/register",
    response_model=AgentRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def register_agent_identity(
    payload: AgentRegistrationRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> AgentRegistrationResponse:
    """Register an agent DID and issue a signed VC."""
    return await identity.register_agent(db=db, request=payload, redis=redis)


@router.post(
    "/identity/agents/verify",
    response_model=CredentialVerificationResponse,
)
async def verify_agent_identity(
    payload: CredentialVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> CredentialVerificationResponse:
    """Verify a presented agent credential."""
    return await identity.verify_agent_online(db=db, credential_jwt=payload.credential_jwt)


@router.get(
    "/identity/agents/{did_value}",
    response_model=AgentIdentityResponse,
)
async def get_agent_identity_record(
    did_value: str,
    db: AsyncSession = Depends(get_db),
) -> AgentIdentityResponse:
    """Resolve one registered agent DID."""
    return await identity.get_agent_identity(db=db, did_value=did_value)


@router.post(
    "/identity/agents/{did_value}/revoke",
    response_model=AgentRevokeResponse,
)
async def revoke_agent_identity(
    did_value: str,
    payload: AgentRevokeRequest,
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> AgentRevokeResponse:
    """Admin revocation for a registered agent DID."""
    return await identity.revoke_agent(
        db=db,
        did_value=did_value,
        request=payload,
        revoked_by=admin_api_key,
    )
