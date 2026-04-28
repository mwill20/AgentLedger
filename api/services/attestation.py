"""Layer 3 attestation storage and verification flows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.layer3 import (
    AttestationCreateRequest,
    AttestationCreateResponse,
    AttestationRecord,
    AttestationVerifyResponse,
    AuditorRecord,
    RevocationCreateRequest,
    RevocationCreateResponse,
)
from . import chain, ranker, runtime_cache, workflow_registry


_ATTESTATION_READ_TTL_SECONDS = 2.0


def _scope_allows(allowed_scopes: list[str], requested_scope: str) -> bool:
    """Return whether an auditor scope authorizes one attestation scope."""
    for scope in allowed_scopes:
        if scope == "*" or scope == requested_scope:
            return True
        if scope.endswith(".*"):
            prefix = scope[:-2]
            if requested_scope == prefix or requested_scope.startswith(prefix + "."):
                return True
    return False


async def submit_attestation(
    db: AsyncSession,
    request: AttestationCreateRequest,
) -> AttestationCreateResponse:
    """Submit one auditor attestation for a registered service."""
    try:
        auditor_result = await db.execute(
            text(
                """
                SELECT id, did, name, ontology_scope, is_active
                FROM auditors
                WHERE did = :did
                """
            ),
            {"did": request.auditor_did},
        )
        auditor_row = auditor_result.mappings().first()
        if auditor_row is None or not auditor_row["is_active"]:
            raise HTTPException(status_code=403, detail="auditor is not active")
        if not _scope_allows(list(auditor_row["ontology_scope"] or []), request.ontology_scope):
            raise HTTPException(
                status_code=403,
                detail="attestation scope is outside the auditor's approved ontology scope",
            )

        service_result = await db.execute(
            text("SELECT id, domain FROM services WHERE domain = :domain"),
            {"domain": request.service_domain},
        )
        service_row = service_result.mappings().first()
        if service_row is None:
            raise HTTPException(status_code=404, detail="service not found")

        evidence_hash = chain.canonical_hash(request.evidence_package)
        recorded_at = datetime.now(timezone.utc)
        tx_hash, block_number = await chain.record_chain_event(
            db=db,
            event_type="attestation",
            service_id=service_row["id"],
            event_data={
                "service_domain": request.service_domain,
                "auditor_did": request.auditor_did,
                "ontology_scope": request.ontology_scope,
                "certification_ref": request.certification_ref,
                "expires_at": (
                    request.expires_at.astimezone(timezone.utc).isoformat()
                    if request.expires_at is not None
                    else None
                ),
                "evidence_hash": evidence_hash,
                "service_chain_id": chain.hash_identifier(request.service_domain),
                "auditor_chain_id": chain.hash_identifier(request.auditor_did),
            },
        )

        await db.execute(
            text(
                """
                UPDATE attestation_records
                SET is_active = false
                WHERE service_id = :service_id
                  AND auditor_id = :auditor_id
                  AND ontology_scope = :ontology_scope
                  AND is_active = true
                """
            ),
            {
                "service_id": service_row["id"],
                "auditor_id": auditor_row["id"],
                "ontology_scope": request.ontology_scope,
            },
        )
        insert_result = await db.execute(
            text(
                """
                INSERT INTO attestation_records (
                    service_id,
                    auditor_id,
                    ontology_scope,
                    certification_ref,
                    evidence_hash,
                    tx_hash,
                    block_number,
                    chain_id,
                    is_confirmed,
                    expires_at,
                    is_active,
                    recorded_at
                )
                VALUES (
                    :service_id,
                    :auditor_id,
                    :ontology_scope,
                    :certification_ref,
                    :evidence_hash,
                    :tx_hash,
                    :block_number,
                    :chain_id,
                    false,
                    :expires_at,
                    true,
                    :recorded_at
                )
                RETURNING id
                """
            ),
            {
                "service_id": service_row["id"],
                "auditor_id": auditor_row["id"],
                "ontology_scope": request.ontology_scope,
                "certification_ref": request.certification_ref,
                "evidence_hash": evidence_hash,
                "tx_hash": tx_hash,
                "block_number": block_number,
                "chain_id": settings.chain_id,
                "expires_at": request.expires_at,
                "recorded_at": recorded_at,
            },
        )
        attestation_id = insert_result.scalar_one()
        await db.commit()
        service_cache_prefix = f"attestations:{service_row['id']}"
        runtime_cache.invalidate_prefix(service_cache_prefix)
        runtime_cache.invalidate_prefix(f"attestation-verify:{service_row['id']}")
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to submit attestation: {exc.__class__.__name__}",
        ) from exc

    return AttestationCreateResponse(
        attestation_id=attestation_id,
        tx_hash=tx_hash,
        block_number=block_number,
    )


async def list_attestations_for_service(
    db: AsyncSession,
    service_id: UUID,
) -> list[AttestationRecord]:
    """List all active attestations for one service."""
    cache_key = f"attestations:{service_id}"
    cached = runtime_cache.get(cache_key)
    if cached is not None:
        return cached

    result = await db.execute(
        text(
            """
            SELECT
                ar.id AS attestation_id,
                a.did,
                a.name,
                a.ontology_scope AS auditor_ontology_scope,
                a.accreditation_refs,
                a.chain_address,
                a.is_active AS auditor_is_active,
                a.approved_at,
                a.credential_expires_at,
                ar.ontology_scope AS scope,
                ar.certification_ref,
                ar.expires_at,
                ar.tx_hash,
                ar.is_confirmed,
                ar.recorded_at
            FROM attestation_records ar
            JOIN auditors a
                ON a.id = ar.auditor_id
            WHERE ar.service_id = :service_id
              AND ar.is_active = true
              AND (ar.expires_at IS NULL OR ar.expires_at > NOW())
            ORDER BY ar.recorded_at DESC
            """
        ),
        {"service_id": service_id},
    )
    rows = result.mappings().all()
    records: list[AttestationRecord] = []
    for row in rows:
        records.append(
            AttestationRecord(
                attestation_id=row["attestation_id"],
                auditor=AuditorRecord(
                    did=row["did"],
                    name=row["name"],
                    ontology_scope=row["auditor_ontology_scope"] or [],
                    accreditation_refs=row["accreditation_refs"] or [],
                    chain_address=row["chain_address"],
                    is_active=row["auditor_is_active"],
                    approved_at=row["approved_at"],
                    credential_expires_at=row["credential_expires_at"],
                ),
                scope=row["scope"],
                certification_ref=row["certification_ref"],
                expires_at=row["expires_at"],
                tx_hash=row["tx_hash"],
                is_confirmed=row["is_confirmed"],
                recorded_at=row["recorded_at"],
            )
        )
    runtime_cache.set(cache_key, records, ttl_seconds=_ATTESTATION_READ_TTL_SECONDS)
    return records


async def verify_service_attestations(
    db: AsyncSession,
    service_id: UUID,
) -> AttestationVerifyResponse:
    """Compare active attestation records to the indexed chain event log."""
    cache_key = f"attestation-verify:{service_id}"
    cached = runtime_cache.get(cache_key)
    if cached is not None:
        return cached

    db_result = await db.execute(
        text(
            """
            SELECT
                ar.tx_hash,
                ar.evidence_hash,
                ar.ontology_scope,
                a.did AS auditor_did
            FROM attestation_records ar
            JOIN auditors a
                ON a.id = ar.auditor_id
            WHERE ar.service_id = :service_id
              AND ar.is_active = true
            """
        ),
        {"service_id": service_id},
    )
    db_rows = db_result.mappings().all()

    chain_result = await db.execute(
        text(
            """
            SELECT tx_hash, event_data
            FROM chain_events
            WHERE service_id = :service_id
              AND event_type = 'attestation'
            """
        ),
        {"service_id": service_id},
    )
    chain_rows = chain_result.mappings().all()
    chain_by_tx = {row["tx_hash"]: row["event_data"] for row in chain_rows}

    matches = len(db_rows) == len(chain_rows)
    confirmed_attestations: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    for row in db_rows:
        event_data = chain_by_tx.get(row["tx_hash"])
        if event_data is None:
            matches = False
            continue
        if (
            event_data.get("evidence_hash") != row["evidence_hash"]
            or event_data.get("ontology_scope") != row["ontology_scope"]
            or event_data.get("auditor_did") != row["auditor_did"]
        ):
            matches = False
        confirmed_attestations.append(
            {
                "ontology_scope": row["ontology_scope"],
                "recorded_at": now,
                "is_expired": False,
                "auditor_org_id": str(row["auditor_did"]).rsplit(":", 1)[-1],
            }
        )

    response = AttestationVerifyResponse(
        on_chain_matches_db=matches,
        attestation_count=len(db_rows),
        trust_tier_eligible=ranker.evaluate_trust_tier_4(
            confirmed_attestations,
            is_globally_revoked=False,
        ),
    )
    runtime_cache.set(cache_key, response, ttl_seconds=_ATTESTATION_READ_TTL_SECONDS)
    return response


async def submit_revocation(
    db: AsyncSession,
    request: RevocationCreateRequest,
    redis=None,
) -> RevocationCreateResponse:
    """Submit one service revocation from an active auditor."""
    try:
        auditor_result = await db.execute(
            text(
                """
                SELECT id, is_active
                FROM auditors
                WHERE did = :did
                """
            ),
            {"did": request.auditor_did},
        )
        auditor_row = auditor_result.mappings().first()
        if auditor_row is None or not auditor_row["is_active"]:
            raise HTTPException(status_code=403, detail="auditor is not active")

        service_result = await db.execute(
            text("SELECT id, domain FROM services WHERE domain = :domain"),
            {"domain": request.service_domain},
        )
        service_row = service_result.mappings().first()
        if service_row is None:
            raise HTTPException(status_code=404, detail="service not found")

        evidence_hash = chain.canonical_hash(request.evidence_package)
        tx_hash, block_number = await chain.record_chain_event(
            db=db,
            event_type="revocation",
            service_id=service_row["id"],
            event_data={
                "service_domain": request.service_domain,
                "auditor_did": request.auditor_did,
                "reason_code": request.reason_code,
                "evidence_hash": evidence_hash,
                "service_chain_id": chain.hash_identifier(request.service_domain),
                "auditor_chain_id": chain.hash_identifier(request.auditor_did),
            },
        )
        revocation_result = await db.execute(
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
                    'service',
                    :target_id,
                    :reason_code,
                    :revoked_by,
                    CAST(:evidence AS JSONB),
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "target_id": str(service_row["id"]),
                "reason_code": request.reason_code,
                "revoked_by": request.auditor_did,
                "evidence": json.dumps({"evidence_hash": evidence_hash, "tx_hash": tx_hash}),
            },
        )
        revocation_id = revocation_result.scalar_one()
        await db.commit()
        runtime_cache.invalidate_prefix(f"attestations:{service_row['id']}")
        runtime_cache.invalidate_prefix(f"attestation-verify:{service_row['id']}")
        runtime_cache.invalidate_prefix("blocklist:")
        await workflow_registry.flag_workflows_for_revoked_service(
            db=db,
            service_id=service_row["id"],
            redis=redis,
        )
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to submit revocation: {exc.__class__.__name__}",
        ) from exc

    return RevocationCreateResponse(
        revocation_id=revocation_id,
        tx_hash=tx_hash,
        block_number=block_number,
    )
