"""Layer 2 agent identity service functions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.identity import (
    AgentCredentialPrincipal,
    AgentIdentityResponse,
    AgentRegistrationRequest,
    AgentRegistrationResponse,
    AgentRevokeRequest,
    AgentRevokeResponse,
    CredentialVerificationResponse,
)
from api.services import credentials, did
from api.services.crypto import verify_json_signature


def _require_identity_runtime() -> None:
    """Ensure the runtime can execute Layer 2 identity operations."""
    try:
        credentials.ensure_jwt_available()
        credentials.load_issuer_private_jwk()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


def _proof_payload(request: AgentRegistrationRequest) -> dict[str, Any]:
    """Build the canonical registration proof payload."""
    return {
        "did": request.did,
        "did_document": request.did_document,
        "agent_name": request.agent_name,
        "issuing_platform": request.issuing_platform,
        "capability_scope": request.capability_scope,
        "risk_tier": request.risk_tier,
        "nonce": request.proof.nonce,
        "created_at": request.proof.created_at.astimezone(timezone.utc).isoformat(),
    }


def _revocation_cache_key(did_value: str) -> str:
    """Build the Redis cache key for one revoked agent DID."""
    return "identity:revoked:" + sha256(did_value.encode("utf-8")).hexdigest()


async def _store_proof_nonce(redis, did_value: str, nonce: str) -> None:
    """Best-effort proof nonce replay protection using Redis."""
    if redis is None:
        return
    key = "identity:proof:" + sha256(f"{did_value}:{nonce}".encode("utf-8")).hexdigest()
    try:
        stored = await redis.set(
            key,
            "1",
            ex=settings.proof_nonce_ttl_seconds,
            nx=True,
        )
    except Exception:
        return
    if stored is False:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="proof nonce has already been used",
        )


async def _get_cached_revocation(redis, did_value: str) -> dict[str, Any] | None:
    """Return cached revocation metadata for one DID when present."""
    if redis is None:
        return None
    try:
        cached = await redis.get(_revocation_cache_key(did_value))
    except Exception:
        return None
    if cached is None:
        return None
    if isinstance(cached, dict):
        return cached
    if isinstance(cached, str):
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return {"did": did_value}
        if isinstance(payload, dict):
            return payload
    return {"did": did_value}


async def _cache_revocation(
    redis,
    did_value: str,
    revoked_at: datetime | None = None,
    reason_code: str | None = None,
) -> None:
    """Best-effort Redis write for revoked agent credentials."""
    if redis is None:
        return
    payload = {"did": did_value}
    if revoked_at is not None:
        payload["revoked_at"] = revoked_at.astimezone(timezone.utc).isoformat()
    if reason_code:
        payload["reason_code"] = reason_code
    try:
        await redis.set(
            _revocation_cache_key(did_value),
            json.dumps(payload, sort_keys=True),
            ex=settings.revocation_cache_ttl_seconds,
        )
    except Exception:
        return


def _coerce_public_jwk(value: Any) -> dict[str, Any]:
    """Normalize a JSONB public JWK row value into a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="stored public key JWK is invalid",
    )


def get_issuer_did_document() -> dict[str, Any]:
    """Return AgentLedger's issuer DID document."""
    _require_identity_runtime()
    return credentials.build_issuer_did_document_payload()


async def register_agent(
    db: AsyncSession,
    request: AgentRegistrationRequest,
    redis=None,
) -> AgentRegistrationResponse:
    """Register an agent DID and issue a signed JWT VC."""
    _require_identity_runtime()

    age_seconds = abs(
        (datetime.now(timezone.utc) - request.proof.created_at.astimezone(timezone.utc)).total_seconds()
    )
    if age_seconds > settings.proof_nonce_ttl_seconds:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="proof timestamp is outside the allowed replay window",
        )

    try:
        public_jwk = did.extract_public_jwk_from_did_document(
            request.did_document,
            expected_did=request.did,
        )
        derived_did = did.did_key_from_public_jwk(public_jwk)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if derived_did != request.did:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="submitted DID does not match the DID document public key",
        )

    if not verify_json_signature(
        payload=_proof_payload(request),
        signature=request.proof.signature,
        public_jwk=public_jwk,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid proof signature",
        )

    await _store_proof_nonce(redis, request.did, request.proof.nonce)

    try:
        existing = await db.execute(
            text(
                """
                SELECT did
                FROM agent_identities
                WHERE did = :did
                """
            ),
            {"did": request.did},
        )
        if existing.mappings().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent DID is already registered",
            )

        credential_jwt, expires_at = credentials.issue_agent_credential(
            subject_did=request.did,
            agent_name=request.agent_name,
            issuing_platform=request.issuing_platform,
            capability_scope=request.capability_scope,
            risk_tier=request.risk_tier,
        )
        credential_hash = sha256(credential_jwt.encode("utf-8")).hexdigest()

        await db.execute(
            text(
                """
                INSERT INTO agent_identities (
                    did,
                    agent_name,
                    issuing_platform,
                    public_key_jwk,
                    capability_scope,
                    risk_tier,
                    credential_hash,
                    credential_expires_at,
                    registered_at,
                    is_active,
                    is_revoked
                )
                VALUES (
                    :did,
                    :agent_name,
                    :issuing_platform,
                    CAST(:public_key_jwk AS JSONB),
                    :capability_scope,
                    :risk_tier,
                    :credential_hash,
                    :credential_expires_at,
                    NOW(),
                    true,
                    false
                )
                """
            ),
            {
                "did": request.did,
                "agent_name": request.agent_name,
                "issuing_platform": request.issuing_platform,
                "public_key_jwk": json.dumps(public_jwk),
                "capability_scope": request.capability_scope,
                "risk_tier": request.risk_tier,
                "credential_hash": credential_hash,
                "credential_expires_at": expires_at,
            },
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to register agent identity: {exc.__class__.__name__}",
        ) from exc

    return AgentRegistrationResponse(
        did=request.did,
        credential_jwt=credential_jwt,
        credential_expires_at=expires_at,
        did_document=did.build_did_key_document(public_jwk),
        issuer_did=settings.issuer_did,
    )


async def verify_agent_online(
    db: AsyncSession,
    credential_jwt: str,
    redis=None,
) -> CredentialVerificationResponse:
    """Verify a JWT VC against issuer config and online revocation state."""
    _require_identity_runtime()
    try:
        claims = credentials.verify_agent_credential(credential_jwt)
    except Exception:
        return CredentialVerificationResponse(valid=False)

    did_value = claims["sub"]
    cached_revocation = await _get_cached_revocation(redis, did_value)
    if cached_revocation is not None:
        return CredentialVerificationResponse(
            valid=False,
            did=did_value,
            is_revoked=True,
        )

    result = await db.execute(
        text(
            """
            SELECT did, is_active, is_revoked, risk_tier, capability_scope, credential_expires_at
            FROM agent_identities
            WHERE did = :did
            """
        ),
        {"did": did_value},
    )
    row = result.mappings().first()
    if not row:
        return CredentialVerificationResponse(valid=False, did=did_value)

    is_valid = bool(row["is_active"]) and not bool(row["is_revoked"])
    if bool(row["is_revoked"]):
        await _cache_revocation(redis, did_value)
    if is_valid:
        try:
            await db.execute(
                text(
                    """
                    UPDATE agent_identities
                    SET last_seen_at = NOW()
                    WHERE did = :did
                    """
                ),
                {"did": did_value},
            )
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()

    return CredentialVerificationResponse(
        valid=is_valid,
        did=did_value,
        expires_at=row["credential_expires_at"],
        is_revoked=bool(row["is_revoked"]),
        capability_scope=list(row["capability_scope"] or []),
        risk_tier=row["risk_tier"],
    )


async def authenticate_agent_credential(
    db: AsyncSession,
    credential_jwt: str,
    redis=None,
) -> AgentCredentialPrincipal:
    """Authenticate a bearer VC and return the current agent principal."""
    _require_identity_runtime()
    try:
        claims = credentials.verify_agent_credential(credential_jwt)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer credential",
        ) from exc

    did_value = claims["sub"]
    cached_revocation = await _get_cached_revocation(redis, did_value)
    if cached_revocation is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent credential is revoked",
        )

    result = await db.execute(
        text(
            """
            SELECT
                did,
                public_key_jwk,
                capability_scope,
                risk_tier,
                is_active,
                is_revoked,
                credential_expires_at
            FROM agent_identities
            WHERE did = :did
            """
        ),
        {"did": did_value},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown bearer credential",
        )
    if row["is_revoked"]:
        await _cache_revocation(redis, did_value)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent credential is revoked",
        )
    if not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="agent identity is inactive",
        )

    await db.execute(
        text(
            """
            UPDATE agent_identities
            SET last_seen_at = NOW()
            WHERE did = :did
            """
        ),
        {"did": did_value},
    )

    return AgentCredentialPrincipal(
        did=did_value,
        capability_scope=list(row["capability_scope"] or []),
        risk_tier=row["risk_tier"],
        public_key_jwk=_coerce_public_jwk(row["public_key_jwk"]),
        credential_claims=claims,
        credential_expires_at=row["credential_expires_at"],
    )


async def get_agent_identity(db: AsyncSession, did_value: str) -> AgentIdentityResponse:
    """Return the public registry record for one agent DID."""
    result = await db.execute(
        text(
            """
            SELECT
                did,
                agent_name,
                issuing_platform,
                public_key_jwk,
                capability_scope,
                risk_tier,
                is_active,
                is_revoked,
                registered_at,
                last_seen_at,
                credential_expires_at
            FROM agent_identities
            WHERE did = :did
            """
        ),
        {"did": did_value},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent DID not found",
        )

    public_jwk = _coerce_public_jwk(row["public_key_jwk"])
    return AgentIdentityResponse(
        did=row["did"],
        did_document=did.build_did_document(did=row["did"], public_jwk=public_jwk),
        agent_name=row["agent_name"],
        issuing_platform=row["issuing_platform"],
        capability_scope=list(row["capability_scope"] or []),
        risk_tier=row["risk_tier"],
        is_active=bool(row["is_active"]),
        is_revoked=bool(row["is_revoked"]),
        registered_at=row["registered_at"],
        last_seen_at=row["last_seen_at"],
        credential_expires_at=row["credential_expires_at"],
    )


async def revoke_agent(
    db: AsyncSession,
    did_value: str,
    request: AgentRevokeRequest,
    revoked_by: str,
    redis=None,
) -> AgentRevokeResponse:
    """Revoke an agent identity and append a revocation event."""
    result = await db.execute(
        text(
            """
            SELECT did, is_revoked, revoked_at
            FROM agent_identities
            WHERE did = :did
            """
        ),
        {"did": did_value},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent DID not found",
        )

    revoked_at = row["revoked_at"]

    try:
        if not row["is_revoked"]:
            await db.execute(
                text(
                    """
                    UPDATE agent_identities
                    SET is_revoked = true,
                        is_active = false,
                        revoked_at = NOW(),
                        revocation_reason = :reason_code
                    WHERE did = :did
                    """
                ),
                {"did": did_value, "reason_code": request.reason_code},
            )
            await db.execute(
                text(
                    """
                    INSERT INTO revocation_events (
                        target_type,
                        target_id,
                        reason_code,
                        revoked_by,
                        evidence,
                        created_at
                    )
                    VALUES (
                        'agent',
                        :did,
                        :reason_code,
                        :revoked_by,
                        CAST(:evidence AS JSONB),
                        NOW()
                    )
                    """
                ),
                {
                    "did": did_value,
                    "reason_code": request.reason_code,
                    "revoked_by": revoked_by,
                    "evidence": json.dumps(request.evidence),
                },
            )
            refreshed = await db.execute(
                text("SELECT revoked_at FROM agent_identities WHERE did = :did"),
                {"did": did_value},
            )
            revoked_at = refreshed.scalar_one()
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to revoke agent identity: {exc.__class__.__name__}",
        ) from exc

    if revoked_at is not None:
        await _cache_revocation(
            redis,
            did_value=did_value,
            revoked_at=revoked_at,
            reason_code=request.reason_code,
        )

    return AgentRevokeResponse(
        did=did_value,
        revoked_at=revoked_at,
        reason_code=request.reason_code,
    )
