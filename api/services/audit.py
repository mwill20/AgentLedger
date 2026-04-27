"""Layer 3 audit record creation, anchoring, and verification."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.layer3 import (
    AuditRecordCreateRequest,
    AuditRecordCreateResponse,
    AuditRecordDetailResponse,
    AuditRecordListResponse,
    AuditRecordVerifyResponse,
    AuditRecordView,
)
from . import chain, merkle


def _audit_payload(
    *,
    agent_did: str,
    service_id: str,
    ontology_tag: str,
    session_assertion_id: str | None,
    action_context: dict[str, Any],
    outcome: str,
    outcome_details: dict[str, Any],
) -> dict[str, Any]:
    """Build the canonical payload used for audit hashing."""
    return {
        "agent_did": agent_did,
        "service_id": service_id,
        "ontology_tag": ontology_tag,
        "session_assertion_id": session_assertion_id,
        "action_context": action_context,
        "outcome": outcome,
        "outcome_details": outcome_details,
    }


async def create_audit_record(
    db: AsyncSession,
    request: AuditRecordCreateRequest,
) -> AuditRecordCreateResponse:
    """Create one off-chain audit record awaiting anchor batching."""
    try:
        session_result = await db.execute(
            text(
                """
                SELECT id, agent_did, service_id, ontology_tag
                FROM session_assertions
                WHERE id = :session_assertion_id
                """
            ),
            {"session_assertion_id": request.session_assertion_id},
        )
        session_row = session_result.mappings().first()
        if session_row is None:
            raise HTTPException(status_code=404, detail="session assertion not found")
        if session_row["ontology_tag"] != request.ontology_tag:
            raise HTTPException(
                status_code=422,
                detail="audit ontology_tag must match the source session assertion",
            )

        payload = _audit_payload(
            agent_did=session_row["agent_did"],
            service_id=str(session_row["service_id"]),
            ontology_tag=request.ontology_tag,
            session_assertion_id=str(request.session_assertion_id),
            action_context=request.action_context,
            outcome=request.outcome,
            outcome_details=request.outcome_details,
        )
        record_hash = chain.canonical_hash(payload)
        insert_result = await db.execute(
            text(
                """
                INSERT INTO audit_records (
                    agent_did,
                    service_id,
                    ontology_tag,
                    session_assertion_id,
                    action_context,
                    outcome,
                    outcome_details,
                    record_hash,
                    is_anchored,
                    created_at
                )
                VALUES (
                    :agent_did,
                    :service_id,
                    :ontology_tag,
                    :session_assertion_id,
                    CAST(:action_context AS JSONB),
                    :outcome,
                    CAST(:outcome_details AS JSONB),
                    :record_hash,
                    false,
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "agent_did": session_row["agent_did"],
                "service_id": session_row["service_id"],
                "ontology_tag": request.ontology_tag,
                "session_assertion_id": request.session_assertion_id,
                "action_context": json.dumps(request.action_context),
                "outcome": request.outcome,
                "outcome_details": json.dumps(request.outcome_details),
                "record_hash": record_hash,
            },
        )
        record_id = insert_result.scalar_one()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to create audit record: {exc.__class__.__name__}",
        ) from exc

    return AuditRecordCreateResponse(
        record_id=record_id,
        record_hash=record_hash,
        status="pending_anchor",
    )


def _to_audit_record_view(row: dict[str, Any]) -> AuditRecordView:
    """Map one DB row into an audit record response model."""
    return AuditRecordView(
        id=row["id"],
        agent_did=row["agent_did"],
        service_id=row["service_id"],
        ontology_tag=row["ontology_tag"],
        session_assertion_id=row["session_assertion_id"],
        action_context=row["action_context"] or {},
        outcome=row["outcome"],
        outcome_details=row["outcome_details"] or {},
        record_hash=row["record_hash"],
        batch_id=row["batch_id"],
        merkle_proof=row["merkle_proof"] or [],
        tx_hash=row["tx_hash"],
        block_number=row["block_number"],
        is_anchored=row["is_anchored"],
        anchored_at=row["anchored_at"],
        created_at=row["created_at"],
    )


async def get_audit_record(db: AsyncSession, record_id: UUID) -> AuditRecordDetailResponse:
    """Return one audit record."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                agent_did,
                service_id,
                ontology_tag,
                session_assertion_id,
                action_context,
                outcome,
                outcome_details,
                record_hash,
                batch_id,
                merkle_proof,
                tx_hash,
                block_number,
                is_anchored,
                anchored_at,
                created_at
            FROM audit_records
            WHERE id = :record_id
            """
        ),
        {"record_id": record_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="audit record not found")
    view = _to_audit_record_view(row)
    return AuditRecordDetailResponse(
        record=view,
        record_hash=view.record_hash,
        batch_id=view.batch_id,
        tx_hash=view.tx_hash,
        is_anchored=view.is_anchored,
    )


async def verify_audit_record(db: AsyncSession, record_id: UUID) -> AuditRecordVerifyResponse:
    """Verify the stored hash and Merkle proof for one audit record."""
    detail = await get_audit_record(db=db, record_id=record_id)
    record = detail.record
    recomputed_hash = chain.canonical_hash(
        _audit_payload(
            agent_did=record.agent_did,
            service_id=str(record.service_id),
            ontology_tag=record.ontology_tag,
            session_assertion_id=(
                str(record.session_assertion_id) if record.session_assertion_id is not None else None
            ),
            action_context=record.action_context,
            outcome=record.outcome,
            outcome_details=record.outcome_details,
        )
    )
    integrity_valid = recomputed_hash == record.record_hash
    if not integrity_valid:
        raise HTTPException(
            status_code=409,
            detail="record hash mismatch - possible tampering",
        )

    merkle_valid = False
    if record.is_anchored and record.batch_id is not None and record.merkle_proof:
        batch_result = await db.execute(
            text(
                """
                SELECT merkle_root
                FROM audit_batches
                WHERE id = :batch_id
                """
            ),
            {"batch_id": record.batch_id},
        )
        batch_row = batch_result.mappings().first()
        if batch_row is not None:
            merkle_valid = merkle.verify_proof(
                leaf_hash=record.record_hash,
                proof=record.merkle_proof,
                root_hash=batch_row["merkle_root"],
            )

    return AuditRecordVerifyResponse(
        integrity_valid=integrity_valid,
        merkle_proof_valid=merkle_valid,
        tx_hash=record.tx_hash,
        block_number=record.block_number,
    )


async def list_audit_records(
    db: AsyncSession,
    agent_did: str | None = None,
    service_id: UUID | None = None,
    ontology_tag: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AuditRecordListResponse:
    """Query Layer 3 audit history."""
    conditions: list[str] = []
    params: dict[str, object] = {
        "limit": limit,
        "offset": offset,
    }
    if agent_did is not None:
        conditions.append("agent_did = :agent_did")
        params["agent_did"] = agent_did
    if service_id is not None:
        conditions.append("service_id = :service_id")
        params["service_id"] = service_id
    if ontology_tag is not None:
        conditions.append("ontology_tag = :ontology_tag")
        params["ontology_tag"] = ontology_tag
    if from_date is not None:
        conditions.append("created_at >= :from_date")
        params["from_date"] = from_date
    if to_date is not None:
        conditions.append("created_at <= :to_date")
        params["to_date"] = to_date
    if outcome is not None:
        conditions.append("outcome = :outcome")
        params["outcome"] = outcome
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                agent_did,
                service_id,
                ontology_tag,
                session_assertion_id,
                action_context,
                outcome,
                outcome_details,
                record_hash,
                batch_id,
                merkle_proof,
                tx_hash,
                block_number,
                is_anchored,
                anchored_at,
                created_at
            FROM audit_records
            {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    )
    rows = result.mappings().all()
    return AuditRecordListResponse(
        total=len(rows),
        limit=limit,
        offset=offset,
        records=[_to_audit_record_view(row) for row in rows],
    )


async def anchor_pending_records(db: AsyncSession) -> dict[str, object]:
    """Batch-anchor all current unanchored audit records."""
    try:
        result = await db.execute(
            text(
                """
                SELECT
                    id,
                    record_hash
                FROM audit_records
                WHERE is_anchored = false
                ORDER BY created_at ASC
                LIMIT :limit
                """
            ),
            {"limit": settings.audit_anchor_batch_size},
        )
        rows = result.mappings().all()
        if not rows:
            return {"record_count": 0, "status": "noop"}

        leaf_hashes = [row["record_hash"] for row in rows]
        merkle_tree = merkle.build_tree(leaf_hashes)
        batch_insert = await db.execute(
            text(
                """
                INSERT INTO audit_batches (
                    merkle_root,
                    record_count,
                    status,
                    created_at,
                    submitted_at
                )
                VALUES (
                    :merkle_root,
                    :record_count,
                    'submitted',
                    NOW(),
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "merkle_root": merkle_tree["root"],
                "record_count": len(rows),
            },
        )
        batch_id = batch_insert.scalar_one()
        tx_hash, block_number = await chain.record_chain_event(
            db=db,
            event_type="audit_batch",
            event_data={
                "batch_id": str(batch_id),
                "merkle_root": merkle_tree["root"],
                "record_count": len(rows),
            },
        )
        await db.execute(
            text(
                """
                UPDATE audit_batches
                SET tx_hash = :tx_hash,
                    block_number = :block_number
                WHERE id = :batch_id
                """
            ),
            {"batch_id": batch_id, "tx_hash": tx_hash, "block_number": block_number},
        )

        anchored_at = datetime.now(timezone.utc)
        for row, proof in zip(rows, merkle_tree["proofs"], strict=True):
            await db.execute(
                text(
                    """
                    UPDATE audit_records
                    SET batch_id = :batch_id,
                        merkle_proof = CAST(:merkle_proof AS JSONB),
                        tx_hash = :tx_hash,
                        block_number = :block_number,
                        is_anchored = true,
                        anchored_at = :anchored_at
                    WHERE id = :record_id
                    """
                ),
                {
                    "batch_id": batch_id,
                    "merkle_proof": json.dumps(proof),
                    "tx_hash": tx_hash,
                    "block_number": block_number,
                    "anchored_at": anchored_at,
                    "record_id": row["id"],
                },
            )
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to anchor audit batch: {exc.__class__.__name__}",
        ) from exc

    return {
        "record_count": len(rows),
        "status": "submitted",
        "batch_id": str(batch_id),
        "tx_hash": tx_hash,
        "block_number": block_number,
    }
