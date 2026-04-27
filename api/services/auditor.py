"""Layer 3 auditor registry service logic."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.layer3 import (
    AuditorRecord,
    AuditorRegistrationRequest,
    AuditorRegistrationResponse,
)
from .chain import canonical_hash


async def register_auditor(
    db: AsyncSession,
    request: AuditorRegistrationRequest,
) -> AuditorRegistrationResponse:
    """Register or refresh one active auditor."""
    credential_expires_at = datetime.now(timezone.utc) + timedelta(days=365)
    credential_hash = canonical_hash(
        {
            "did": request.did,
            "name": request.name,
            "ontology_scope": request.ontology_scope,
            "chain_address": request.chain_address,
        }
    )

    try:
        result = await db.execute(
            text(
                """
                INSERT INTO auditors (
                    did,
                    name,
                    ontology_scope,
                    accreditation_refs,
                    chain_address,
                    credential_hash,
                    is_active,
                    approved_at,
                    credential_expires_at,
                    created_at
                )
                VALUES (
                    :did,
                    :name,
                    :ontology_scope,
                    CAST(:accreditation_refs AS JSONB),
                    :chain_address,
                    :credential_hash,
                    true,
                    NOW(),
                    :credential_expires_at,
                    NOW()
                )
                ON CONFLICT (did) DO UPDATE
                SET name = EXCLUDED.name,
                    ontology_scope = EXCLUDED.ontology_scope,
                    accreditation_refs = EXCLUDED.accreditation_refs,
                    chain_address = EXCLUDED.chain_address,
                    credential_hash = EXCLUDED.credential_hash,
                    is_active = true,
                    approved_at = NOW(),
                    credential_expires_at = EXCLUDED.credential_expires_at
                RETURNING id
                """
            ),
            {
                "did": request.did,
                "name": request.name,
                "ontology_scope": request.ontology_scope,
                "accreditation_refs": json.dumps(request.accreditation_refs),
                "chain_address": request.chain_address,
                "credential_hash": credential_hash,
                "credential_expires_at": credential_expires_at,
            },
        )
        application_id = result.scalar_one()
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to register auditor: {exc.__class__.__name__}",
        ) from exc

    return AuditorRegistrationResponse(application_id=application_id, status="active")


async def list_auditors(db: AsyncSession) -> list[AuditorRecord]:
    """Return all active auditors."""
    result = await db.execute(
        text(
            """
            SELECT
                did,
                name,
                ontology_scope,
                accreditation_refs,
                chain_address,
                is_active,
                approved_at,
                credential_expires_at
            FROM auditors
            WHERE is_active = true
            ORDER BY name ASC
            """
        )
    )
    return [AuditorRecord.model_validate(row) for row in result.mappings().all()]


async def get_auditor(db: AsyncSession, did: str) -> AuditorRecord:
    """Resolve one active or inactive auditor record."""
    result = await db.execute(
        text(
            """
            SELECT
                did,
                name,
                ontology_scope,
                accreditation_refs,
                chain_address,
                is_active,
                approved_at,
                credential_expires_at
            FROM auditors
            WHERE did = :did
            """
        ),
        {"did": did},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="auditor not found")
    return AuditorRecord.model_validate(row)
