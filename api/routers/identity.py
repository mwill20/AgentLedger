"""Layer 2 identity endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import (
    get_db,
    get_redis,
    require_admin_api_key,
    require_api_key,
    require_bearer_credential,
)
from api.models.identity import (
    AgentCredentialPrincipal,
    AgentIdentityResponse,
    AgentRegistrationRequest,
    AgentRegistrationResponse,
    AgentRevokeRequest,
    AgentRevokeResponse,
    AuthorizationDecisionResponse,
    AuthorizationPendingListResponse,
    CredentialVerificationRequest,
    CredentialVerificationResponse,
    SessionRedeemRequest,
    SessionRedeemResponse,
    SessionRequest,
    SessionStatusResponse,
    ServiceDidResolutionResponse,
    ServiceIdentityActivationResponse,
)
from api.services import authorization, identity, service_identity, sessions

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


@router.post(
    "/identity/sessions/request",
    response_model=SessionStatusResponse,
)
async def request_session_assertion(
    payload: SessionRequest,
    response: Response,
    principal: AgentCredentialPrincipal = Depends(require_bearer_credential),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> SessionStatusResponse:
    """Issue a session assertion or create a pending authorization request."""
    result = await sessions.request_session(
        db=db,
        principal=principal,
        request=payload,
        redis=redis,
    )
    if result.status == "pending_approval":
        response.status_code = status.HTTP_202_ACCEPTED
    return result


@router.get(
    "/identity/sessions/{session_id}",
    response_model=SessionStatusResponse,
)
async def get_session_assertion_status(
    session_id: UUID,
    response: Response,
    principal: AgentCredentialPrincipal = Depends(require_bearer_credential),
    db: AsyncSession = Depends(get_db),
) -> SessionStatusResponse:
    """Return the current status for one issued or pending session flow."""
    result = await sessions.get_session_status(
        db=db,
        principal=principal,
        session_id=session_id,
    )
    if result.status == "pending_approval":
        response.status_code = status.HTTP_202_ACCEPTED
    elif result.status in {"denied", "expired"}:
        response.status_code = status.HTTP_403_FORBIDDEN
    return result


@router.post(
    "/identity/sessions/redeem",
    response_model=SessionRedeemResponse,
)
async def redeem_session_assertion(
    payload: SessionRedeemRequest,
    db: AsyncSession = Depends(get_db),
) -> SessionRedeemResponse:
    """Redeem a session assertion exactly once."""
    return await sessions.redeem_session(
        db=db,
        request=payload,
    )


@router.get(
    "/identity/services/{domain}/did",
    response_model=ServiceDidResolutionResponse,
)
async def get_service_did_document(
    domain: str,
    redis=Depends(get_redis),
) -> ServiceDidResolutionResponse:
    """Resolve the did:web document for one service domain."""
    return await service_identity.resolve_service_did_document(
        domain=domain,
        redis=redis,
    )


@router.post(
    "/identity/services/{domain}/activate",
    response_model=ServiceIdentityActivationResponse,
    dependencies=[Depends(require_api_key)],
)
async def activate_service_did(
    domain: str,
    force_refresh: bool = False,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ServiceIdentityActivationResponse:
    """Validate and activate one service's did:web identity."""
    return await service_identity.activate_service_identity(
        db=db,
        domain=domain,
        redis=redis,
        force_refresh=force_refresh,
    )


@router.get(
    "/authorization/pending",
    response_model=AuthorizationPendingListResponse,
)
async def get_pending_authorizations(
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> AuthorizationPendingListResponse:
    """List the pending HITL authorization queue."""
    del admin_api_key
    return await authorization.list_pending_authorizations(db=db)


@router.post(
    "/authorization/approve/{authorization_request_id}",
    response_model=AuthorizationDecisionResponse,
)
async def approve_authorization(
    authorization_request_id: UUID,
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> AuthorizationDecisionResponse:
    """Approve one pending authorization request and issue a linked session."""
    return await authorization.approve_authorization_request(
        db=db,
        authorization_request_id=authorization_request_id,
        approver_id=admin_api_key,
    )


@router.post(
    "/authorization/deny/{authorization_request_id}",
    response_model=AuthorizationDecisionResponse,
)
async def deny_authorization(
    authorization_request_id: UUID,
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> AuthorizationDecisionResponse:
    """Deny one pending authorization request."""
    return await authorization.deny_authorization_request(
        db=db,
        authorization_request_id=authorization_request_id,
        approver_id=admin_api_key,
    )
