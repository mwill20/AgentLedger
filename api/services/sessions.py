"""Layer 2 session assertion service functions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.identity import (
    AgentCredentialPrincipal,
    SessionRedeemRequest,
    SessionRedeemResponse,
    SessionRequest,
    SessionStatusResponse,
)
from api.services import credentials
from api.services.crypto import verify_json_signature


def _service_did_from_domain(domain: str) -> str:
    """Derive the current service DID format from a service domain."""
    return f"did:web:{domain}"


def _session_proof_payload(
    principal: AgentCredentialPrincipal,
    request: SessionRequest,
) -> dict[str, Any]:
    """Build the canonical payload signed for a session request."""
    return {
        "agent_did": principal.did,
        "service_domain": request.service_domain,
        "ontology_tag": request.ontology_tag,
        "request_context": request.request_context,
        "nonce": request.proof.nonce,
        "created_at": request.proof.created_at.astimezone(timezone.utc).isoformat(),
    }


async def _store_proof_nonce(redis, principal_did: str, nonce: str) -> None:
    """Best-effort proof nonce replay protection using Redis."""
    if redis is None:
        return
    key = "session:proof:" + sha256(f"{principal_did}:{nonce}".encode("utf-8")).hexdigest()
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


def _scope_allows(capability_scope: list[str], ontology_tag: str) -> bool:
    """Return whether one ontology tag is permitted by the agent scope list."""
    if not capability_scope:
        return False
    for scope in capability_scope:
        if scope == "*":
            return True
        normalized_scope = scope[:-2] if scope.endswith(".*") else scope
        if normalized_scope == ontology_tag:
            return True
        if ontology_tag.startswith(normalized_scope + "."):
            return True
    return False


async def request_session(
    db: AsyncSession,
    principal: AgentCredentialPrincipal,
    request: SessionRequest,
    redis=None,
) -> SessionStatusResponse:
    """Issue a low-risk session assertion or create a pending authorization record."""
    age_seconds = abs(
        (datetime.now(timezone.utc) - request.proof.created_at.astimezone(timezone.utc)).total_seconds()
    )
    if age_seconds > settings.proof_nonce_ttl_seconds:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="proof timestamp is outside the allowed replay window",
        )

    if not verify_json_signature(
        payload=_session_proof_payload(principal, request),
        signature=request.proof.signature,
        public_jwk=principal.public_key_jwk,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid session proof signature",
        )

    await _store_proof_nonce(redis, principal.did, request.proof.nonce)

    if not _scope_allows(principal.capability_scope, request.ontology_tag):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requested ontology_tag is outside the agent credential scope",
        )

    try:
        service_result = await db.execute(
            text(
                """
                SELECT
                    s.id AS service_id,
                    s.domain,
                    s.last_verified_at,
                    t.sensitivity_tier
                FROM services s
                JOIN service_capabilities c
                    ON c.service_id = s.id
                JOIN ontology_tags t
                    ON t.tag = c.ontology_tag
                WHERE s.domain = :service_domain
                  AND c.ontology_tag = :ontology_tag
                  AND s.is_active = true
                  AND s.is_banned = false
                LIMIT 1
                """
            ),
            {
                "service_domain": request.service_domain,
                "ontology_tag": request.ontology_tag,
            },
        )
        service_row = service_result.mappings().first()
        if not service_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="service capability not found",
            )
        if service_row["last_verified_at"] is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="service identity is not active",
            )

        if service_row["sensitivity_tier"] >= 3:
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=settings.authorization_request_ttl_seconds
            )
            auth_result = await db.execute(
                text(
                    """
                    INSERT INTO authorization_requests (
                        agent_did,
                        service_id,
                        ontology_tag,
                        sensitivity_tier,
                        request_context,
                        status,
                        expires_at,
                        created_at
                    )
                    VALUES (
                        :agent_did,
                        :service_id,
                        :ontology_tag,
                        :sensitivity_tier,
                        CAST(:request_context AS JSONB),
                        'pending',
                        :expires_at,
                        NOW()
                    )
                    RETURNING id
                    """
                ),
                {
                    "agent_did": principal.did,
                    "service_id": service_row["service_id"],
                    "ontology_tag": request.ontology_tag,
                    "sensitivity_tier": service_row["sensitivity_tier"],
                    "request_context": json.dumps(request.request_context),
                    "expires_at": expires_at,
                },
            )
            authorization_request_id = auth_result.scalar_one()
            await db.commit()
            return SessionStatusResponse(
                status="pending_approval",
                authorization_request_id=str(authorization_request_id),
                expires_at=expires_at,
            )

        service_did = _service_did_from_domain(service_row["domain"])
        assertion_jwt, assertion_jti, expires_at = credentials.issue_session_assertion(
            subject_did=principal.did,
            service_did=service_did,
            service_id=str(service_row["service_id"]),
            ontology_tag=request.ontology_tag,
        )
        session_result = await db.execute(
            text(
                """
                INSERT INTO session_assertions (
                    assertion_jti,
                    agent_did,
                    service_id,
                    ontology_tag,
                    assertion_token,
                    expires_at,
                    was_used,
                    issued_at
                )
                VALUES (
                    :assertion_jti,
                    :agent_did,
                    :service_id,
                    :ontology_tag,
                    :assertion_token,
                    :expires_at,
                    false,
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "assertion_jti": assertion_jti,
                "agent_did": principal.did,
                "service_id": service_row["service_id"],
                "ontology_tag": request.ontology_tag,
                "assertion_token": assertion_jwt,
                "expires_at": expires_at,
            },
        )
        session_id = session_result.scalar_one()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to request session assertion: {exc.__class__.__name__}",
        ) from exc

    return SessionStatusResponse(
        status="issued",
        session_id=str(session_id),
        assertion_jwt=assertion_jwt,
        service_did=service_did,
        expires_at=expires_at,
    )


async def get_session_status(
    db: AsyncSession,
    principal: AgentCredentialPrincipal,
    session_id: UUID,
) -> SessionStatusResponse:
    """Return the current status of one issued or pending session flow."""
    session_result = await db.execute(
        text(
            """
            SELECT
                sa.id,
                sa.assertion_token,
                sa.expires_at,
                sa.authorization_ref,
                s.domain
            FROM session_assertions sa
            JOIN services s
                ON s.id = sa.service_id
            WHERE sa.id = :session_id
              AND sa.agent_did = :agent_did
            """
        ),
        {"session_id": session_id, "agent_did": principal.did},
    )
    session_row = session_result.mappings().first()
    now = datetime.now(timezone.utc)
    if session_row:
        if session_row["expires_at"] <= now:
            return SessionStatusResponse(
                status="expired",
                session_id=str(session_row["id"]),
                expires_at=session_row["expires_at"],
            )
        return SessionStatusResponse(
            status="issued",
            session_id=str(session_row["id"]),
            assertion_jwt=session_row["assertion_token"],
            service_did=_service_did_from_domain(session_row["domain"]),
            authorization_request_id=(
                str(session_row["authorization_ref"])
                if session_row["authorization_ref"] is not None
                else None
            ),
            expires_at=session_row["expires_at"],
        )

    authorization_result = await db.execute(
        text(
            """
            SELECT
                id,
                status,
                expires_at
            FROM authorization_requests
            WHERE id = :session_id
              AND agent_did = :agent_did
            """
        ),
        {"session_id": session_id, "agent_did": principal.did},
    )
    authorization_row = authorization_result.mappings().first()
    if not authorization_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session or authorization request not found",
        )

    if (
        authorization_row["status"] == "pending"
        and authorization_row["expires_at"] <= now
    ):
        try:
            await db.execute(
                text(
                    """
                    UPDATE authorization_requests
                    SET status = 'expired'
                    WHERE id = :authorization_request_id
                      AND status = 'pending'
                    """
                ),
                {"authorization_request_id": session_id},
            )
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
        return SessionStatusResponse(
            status="expired",
            authorization_request_id=str(authorization_row["id"]),
            expires_at=authorization_row["expires_at"],
        )

    if authorization_row["status"] == "approved":
        linked_session_result = await db.execute(
            text(
                """
                SELECT
                    sa.id,
                    sa.assertion_token,
                    sa.expires_at,
                    s.domain
                FROM session_assertions sa
                JOIN services s
                    ON s.id = sa.service_id
                WHERE sa.authorization_ref = :authorization_request_id
                  AND sa.agent_did = :agent_did
                """
            ),
            {
                "authorization_request_id": session_id,
                "agent_did": principal.did,
            },
        )
        linked_session = linked_session_result.mappings().first()
        if linked_session:
            return SessionStatusResponse(
                status="issued",
                session_id=str(linked_session["id"]),
                assertion_jwt=linked_session["assertion_token"],
                service_did=_service_did_from_domain(linked_session["domain"]),
                authorization_request_id=str(session_id),
                expires_at=linked_session["expires_at"],
            )

    if authorization_row["status"] == "denied":
        return SessionStatusResponse(
            status="denied",
            authorization_request_id=str(authorization_row["id"]),
            expires_at=authorization_row["expires_at"],
        )

    if authorization_row["status"] == "expired":
        return SessionStatusResponse(
            status="expired",
            authorization_request_id=str(authorization_row["id"]),
            expires_at=authorization_row["expires_at"],
        )

    return SessionStatusResponse(
        status="pending_approval",
        authorization_request_id=str(authorization_row["id"]),
        expires_at=authorization_row["expires_at"],
    )


async def redeem_session(
    db: AsyncSession,
    request: SessionRedeemRequest,
) -> SessionRedeemResponse:
    """Redeem a session assertion exactly once."""
    try:
        claims = credentials.verify_session_assertion(request.assertion_jwt)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or expired session assertion",
        ) from exc

    expected_service_did = _service_did_from_domain(request.service_domain)
    if claims.get("aud") != expected_service_did:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session assertion audience does not match the service",
        )

    try:
        service_result = await db.execute(
            text(
                """
                SELECT id
                FROM services
                WHERE domain = :service_domain
                  AND is_active = true
                  AND is_banned = false
                """
            ),
            {"service_domain": request.service_domain},
        )
        service_row = service_result.mappings().first()
        if not service_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="service not found",
            )
        if str(service_row["id"]) != str(claims.get("service_id")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="session assertion service binding is invalid",
            )

        redeem_result = await db.execute(
            text(
                """
                UPDATE session_assertions
                SET was_used = true,
                    used_at = NOW()
                WHERE assertion_jti = :assertion_jti
                  AND service_id = :service_id
                  AND was_used = false
                  AND expires_at > NOW()
                RETURNING agent_did, ontology_tag, authorization_ref
                """
            ),
            {
                "assertion_jti": claims["jti"],
                "service_id": service_row["id"],
            },
        )
        redeemed_row = redeem_result.mappings().first()
        if redeemed_row:
            await db.execute(
                text(
                    """
                    INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                    VALUES (
                        :service_id,
                        'session_redeemed',
                        :service_domain,
                        '{}'::jsonb,
                        NOW()
                    )
                    """
                ),
                {
                    "service_id": service_row["id"],
                    "service_domain": request.service_domain,
                },
            )
            await db.commit()
            return SessionRedeemResponse(
                status="accepted",
                agent_did=redeemed_row["agent_did"],
                ontology_tag=redeemed_row["ontology_tag"],
                authorization_ref=(
                    str(redeemed_row["authorization_ref"])
                    if redeemed_row["authorization_ref"] is not None
                    else None
                ),
            )

        existing_result = await db.execute(
            text(
                """
                SELECT was_used, expires_at
                FROM session_assertions
                WHERE assertion_jti = :assertion_jti
                  AND service_id = :service_id
                """
            ),
            {
                "assertion_jti": claims["jti"],
                "service_id": service_row["id"],
            },
        )
        existing_row = existing_result.mappings().first()
        if existing_row and existing_row["was_used"]:
            await db.execute(
                text(
                    """
                    INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                    VALUES (
                        :service_id,
                        'session_redeem_rejected',
                        :service_domain,
                        CAST(:details AS JSONB),
                        NOW()
                    )
                    """
                ),
                {
                    "service_id": service_row["id"],
                    "service_domain": request.service_domain,
                    "details": json.dumps({"reason": "already_redeemed"}),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="session assertion has already been redeemed",
            )
        if existing_row:
            await db.execute(
                text(
                    """
                    INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                    VALUES (
                        :service_id,
                        'session_redeem_rejected',
                        :service_domain,
                        CAST(:details AS JSONB),
                        NOW()
                    )
                    """
                ),
                {
                    "service_id": service_row["id"],
                    "service_domain": request.service_domain,
                    "details": json.dumps({"reason": "expired_or_invalid"}),
                },
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or expired session assertion",
        )
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to redeem session assertion: {exc.__class__.__name__}",
        ) from exc
