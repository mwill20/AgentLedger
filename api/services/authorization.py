"""Layer 2 human approval queue services."""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.identity import (
    AuthorizationDecisionResponse,
    AuthorizationPendingListResponse,
    AuthorizationRequestRecord,
)
from api.services import credentials

logger = logging.getLogger(__name__)


def _service_did_from_domain(domain: str) -> str:
    """Derive the v0.1 service DID from a service domain."""
    return f"did:web:{domain}"


def _authorization_record_from_row(row: dict[str, Any]) -> AuthorizationRequestRecord:
    """Map one DB row into an approval queue record."""
    return AuthorizationRequestRecord(
        id=str(row["id"]),
        agent_did=row["agent_did"],
        service_domain=row["service_domain"],
        service_did=_service_did_from_domain(row["service_domain"]),
        ontology_tag=row["ontology_tag"],
        sensitivity_tier=int(row["sensitivity_tier"]),
        request_context=dict(row["request_context"] or {}),
        status=row["status"],
        approver_id=row["approver_id"],
        decided_at=row["decided_at"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def _webhook_headers(event_type: str, payload_json: str, timestamp: str) -> dict[str, str]:
    """Build optional signed headers for outbound approval webhooks."""
    headers = {
        "X-AgentLedger-Event": event_type,
        "X-AgentLedger-Timestamp": timestamp,
    }
    secret = settings.authorization_webhook_secret.strip()
    if secret:
        signed = f"{timestamp}.{payload_json}".encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), signed, sha256).hexdigest()
        headers["X-AgentLedger-Signature"] = f"sha256={digest}"
    return headers


async def dispatch_authorization_webhook(event_type: str, payload: dict[str, Any]) -> None:
    """Best-effort webhook dispatch for approval queue events."""
    url = settings.authorization_webhook_url.strip()
    if not url:
        return

    envelope = {
        "event": event_type,
        "payload": payload,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    payload_json = json.dumps(envelope, sort_keys=True, default=str)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    headers = _webhook_headers(event_type, payload_json, timestamp)

    try:
        async with httpx.AsyncClient(timeout=settings.authorization_webhook_timeout_seconds) as client:
            response = await client.post(url, json=envelope, headers=headers)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("authorization webhook dispatch failed for %s: %s", event_type, exc)


async def list_pending_authorizations(
    db: AsyncSession,
) -> AuthorizationPendingListResponse:
    """Return the current pending human approval queue."""
    try:
        await db.execute(
            text(
                """
                UPDATE authorization_requests
                SET status = 'expired',
                    decided_at = COALESCE(decided_at, NOW())
                WHERE status = 'pending'
                  AND expires_at <= NOW()
                """
            )
        )
        await db.commit()

        result = await db.execute(
            text(
                """
                SELECT
                    ar.id,
                    ar.agent_did,
                    s.domain AS service_domain,
                    ar.ontology_tag,
                    ar.sensitivity_tier,
                    ar.request_context,
                    ar.status,
                    ar.approver_id,
                    ar.decided_at,
                    ar.expires_at,
                    ar.created_at
                FROM authorization_requests ar
                JOIN services s
                    ON s.id = ar.service_id
                WHERE ar.status = 'pending'
                ORDER BY ar.created_at ASC
                """
            )
        )
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to list pending authorization requests: {exc.__class__.__name__}",
        ) from exc

    rows = [_authorization_record_from_row(dict(row)) for row in result.mappings().all()]
    return AuthorizationPendingListResponse(total=len(rows), results=rows)


async def approve_authorization_request(
    db: AsyncSession,
    authorization_request_id: UUID,
    approver_id: str,
) -> AuthorizationDecisionResponse:
    """Approve one pending HITL request and issue a linked session assertion."""
    try:
        result = await db.execute(
            text(
                """
                SELECT
                    ar.id,
                    ar.agent_did,
                    ar.service_id,
                    ar.ontology_tag,
                    ar.status,
                    ar.expires_at,
                    s.domain AS service_domain,
                    s.is_active,
                    s.is_banned,
                    s.last_verified_at,
                    ai.is_active AS agent_is_active,
                    ai.is_revoked AS agent_is_revoked
                FROM authorization_requests ar
                JOIN services s
                    ON s.id = ar.service_id
                JOIN agent_identities ai
                    ON ai.did = ar.agent_did
                WHERE ar.id = :authorization_request_id
                FOR UPDATE
                """
            ),
            {"authorization_request_id": authorization_request_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="authorization request not found",
            )

        if row["status"] == "approved":
            session_result = await db.execute(
                text(
                    """
                    SELECT id, assertion_token, expires_at
                    FROM session_assertions
                    WHERE authorization_ref = :authorization_request_id
                    ORDER BY issued_at DESC
                    LIMIT 1
                    """
                ),
                {"authorization_request_id": authorization_request_id},
            )
            session_row = session_result.mappings().first()
            if not session_row:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="authorization request is approved but no linked session exists",
                )
            return AuthorizationDecisionResponse(
                authorization_request_id=str(row["id"]),
                status="approved",
                approver_id=approver_id,
                session_id=str(session_row["id"]),
                assertion_jwt=session_row["assertion_token"],
                service_did=_service_did_from_domain(row["service_domain"]),
                expires_at=session_row["expires_at"],
            )

        if row["status"] == "denied":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="authorization request has already been denied",
            )
        if row["status"] == "expired":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="authorization request has already expired",
            )

        if row["expires_at"] <= datetime.now(timezone.utc):
            await db.execute(
                text(
                    """
                    UPDATE authorization_requests
                    SET status = 'expired',
                        decided_at = NOW()
                    WHERE id = :authorization_request_id
                      AND status = 'pending'
                    """
                ),
                {"authorization_request_id": authorization_request_id},
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="authorization request has expired",
            )

        if not row["agent_is_active"] or row["agent_is_revoked"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="agent identity is inactive or revoked",
            )
        if not row["is_active"] or row["is_banned"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="service is inactive or banned",
            )
        if row["last_verified_at"] is None:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail="service identity is not active",
            )

        service_did = _service_did_from_domain(row["service_domain"])
        assertion_jwt, assertion_jti, expires_at = credentials.issue_session_assertion(
            subject_did=row["agent_did"],
            service_did=service_did,
            service_id=str(row["service_id"]),
            ontology_tag=row["ontology_tag"],
            authorization_ref=str(row["id"]),
            ttl_seconds=settings.approved_session_ttl_seconds,
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
                    authorization_ref,
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
                    :authorization_ref,
                    false,
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "assertion_jti": assertion_jti,
                "agent_did": row["agent_did"],
                "service_id": row["service_id"],
                "ontology_tag": row["ontology_tag"],
                "assertion_token": assertion_jwt,
                "expires_at": expires_at,
                "authorization_ref": row["id"],
            },
        )
        session_id = session_result.scalar_one()
        await db.execute(
            text(
                """
                UPDATE authorization_requests
                SET status = 'approved',
                    approver_id = :approver_id,
                    decided_at = NOW()
                WHERE id = :authorization_request_id
                """
            ),
            {
                "authorization_request_id": authorization_request_id,
                "approver_id": approver_id,
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                VALUES (
                    :service_id,
                    'authorization_approved',
                    :service_domain,
                    CAST(:details AS JSONB),
                    NOW()
                )
                """
            ),
            {
                "service_id": row["service_id"],
                "service_domain": row["service_domain"],
                "details": json.dumps(
                    {
                        "authorization_request_id": str(row["id"]),
                        "agent_did": row["agent_did"],
                        "ontology_tag": row["ontology_tag"],
                        "approver_id": approver_id,
                    }
                ),
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
            detail=f"failed to approve authorization request: {exc.__class__.__name__}",
        ) from exc

    response = AuthorizationDecisionResponse(
        authorization_request_id=str(row["id"]),
        status="approved",
        approver_id=approver_id,
        session_id=str(session_id),
        assertion_jwt=assertion_jwt,
        service_did=service_did,
        expires_at=expires_at,
    )
    await dispatch_authorization_webhook(
        "authorization.approved",
        {
            "authorization_request_id": response.authorization_request_id,
            "status": response.status,
            "approver_id": response.approver_id,
            "agent_did": row["agent_did"],
            "service_domain": row["service_domain"],
            "service_did": response.service_did,
            "ontology_tag": row["ontology_tag"],
            "session_id": response.session_id,
            "expires_at": response.expires_at.isoformat(),
        },
    )
    return response


async def deny_authorization_request(
    db: AsyncSession,
    authorization_request_id: UUID,
    approver_id: str,
) -> AuthorizationDecisionResponse:
    """Deny one pending HITL request."""
    try:
        result = await db.execute(
            text(
                """
                SELECT
                    ar.id,
                    ar.agent_did,
                    ar.service_id,
                    ar.ontology_tag,
                    ar.status,
                    ar.expires_at,
                    s.domain AS service_domain
                FROM authorization_requests ar
                JOIN services s
                    ON s.id = ar.service_id
                WHERE ar.id = :authorization_request_id
                FOR UPDATE
                """
            ),
            {"authorization_request_id": authorization_request_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="authorization request not found",
            )
        if row["status"] == "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="authorization request has already been approved",
            )
        if row["status"] == "denied":
            return AuthorizationDecisionResponse(
                authorization_request_id=str(row["id"]),
                status="denied",
                approver_id=approver_id,
                expires_at=row["expires_at"],
            )
        if row["status"] == "expired":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="authorization request has already expired",
            )
        if row["expires_at"] <= datetime.now(timezone.utc):
            await db.execute(
                text(
                    """
                    UPDATE authorization_requests
                    SET status = 'expired',
                        decided_at = NOW()
                    WHERE id = :authorization_request_id
                      AND status = 'pending'
                    """
                ),
                {"authorization_request_id": authorization_request_id},
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="authorization request has expired",
            )

        await db.execute(
            text(
                """
                UPDATE authorization_requests
                SET status = 'denied',
                    approver_id = :approver_id,
                    decided_at = NOW()
                WHERE id = :authorization_request_id
                """
            ),
            {
                "authorization_request_id": authorization_request_id,
                "approver_id": approver_id,
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                VALUES (
                    :service_id,
                    'authorization_denied',
                    :service_domain,
                    CAST(:details AS JSONB),
                    NOW()
                )
                """
            ),
            {
                "service_id": row["service_id"],
                "service_domain": row["service_domain"],
                "details": json.dumps(
                    {
                        "authorization_request_id": str(row["id"]),
                        "agent_did": row["agent_did"],
                        "ontology_tag": row["ontology_tag"],
                        "approver_id": approver_id,
                    }
                ),
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
            detail=f"failed to deny authorization request: {exc.__class__.__name__}",
        ) from exc

    response = AuthorizationDecisionResponse(
        authorization_request_id=str(row["id"]),
        status="denied",
        approver_id=approver_id,
        expires_at=row["expires_at"],
    )
    await dispatch_authorization_webhook(
        "authorization.denied",
        {
            "authorization_request_id": response.authorization_request_id,
            "status": response.status,
            "approver_id": response.approver_id,
            "agent_did": row["agent_did"],
            "service_domain": row["service_domain"],
            "service_did": _service_did_from_domain(row["service_domain"]),
            "ontology_tag": row["ontology_tag"],
            "expires_at": response.expires_at.isoformat(),
        },
    )
    return response
