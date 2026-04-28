"""Layer 4 context mismatch detection and admin workflows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.context import (
    ContextMatchRequest,
    ContextMismatchListResponse,
    ContextMismatchRecord,
    ContextMismatchResolveRequest,
    ContextMismatchResolveResponse,
)
from api.models.layer3 import RevocationCreateRequest
from api.services import attestation


@dataclass(frozen=True)
class MismatchResult:
    """In-memory mismatch classification result."""

    detected: bool
    over_requested_fields: list[str]
    severity: str = "warning"


@dataclass(frozen=True)
class ManifestContextBlock:
    """Declared context fields from a service manifest."""

    required: list[str]
    optional: list[str]


_FIELD_SENSITIVITY_TIERS = {
    "user.ssn": 4,
    "user.full_medical_history": 4,
    "user.medical_history": 4,
    "user.payment_card": 4,
    "user.bank_account": 4,
    "user.insurance_id": 3,
    "user.dob": 3,
    "user.date_of_birth": 3,
    "user.health_record_id": 3,
    "user.passport_number": 3,
    "user.government_id": 3,
}
_SENSITIVITY_NAME_TO_TIER = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def get_sensitivity_tier(field_name: str, sensitivity: str | None = None) -> int:
    """Return a conservative sensitivity tier for one context field."""
    if sensitivity:
        return _SENSITIVITY_NAME_TO_TIER.get(sensitivity.lower(), 1)
    if field_name in _FIELD_SENSITIVITY_TIERS:
        return _FIELD_SENSITIVITY_TIERS[field_name]
    lowered = field_name.lower()
    if any(token in lowered for token in ("ssn", "medical_history", "payment_card")):
        return 4
    if any(token in lowered for token in ("insurance", "dob", "birth", "passport")):
        return 3
    return 1


def detect_mismatch(
    requested_fields: list[str],
    manifest_context: ManifestContextBlock,
) -> MismatchResult:
    """Detect whether runtime-requested fields exceed manifest declarations."""
    declared = set(manifest_context.required + manifest_context.optional)
    requested = set(requested_fields)
    over_requested = sorted(requested - declared)
    if not over_requested:
        return MismatchResult(detected=False, over_requested_fields=[])

    severity = "warning"
    for field in over_requested:
        if get_sensitivity_tier(field) >= 3:
            severity = "critical"
            break

    return MismatchResult(
        detected=True,
        over_requested_fields=over_requested,
        severity=severity,
    )


def _to_mismatch_record(row: Mapping[str, Any]) -> ContextMismatchRecord:
    """Map one DB row into a mismatch event response."""
    return ContextMismatchRecord(
        id=row["id"],
        service_id=row["service_id"],
        agent_did=row["agent_did"],
        declared_fields=list(row["declared_fields"] or []),
        requested_fields=list(row["requested_fields"] or []),
        over_requested_fields=list(row["over_requested_fields"] or []),
        severity=row["severity"],
        resolved=row["resolved"],
        resolution_note=row["resolution_note"],
        created_at=row["created_at"],
    )


async def _load_service_context(
    db: AsyncSession,
    service_id: UUID,
) -> tuple[str, list[str]]:
    """Load a service's declared context fields from Layer 1 metadata."""
    result = await db.execute(
        text(
            """
            SELECT
                s.domain,
                scr.field_name
            FROM services s
            LEFT JOIN service_context_requirements scr
                ON scr.service_id = s.id
            WHERE s.id = :service_id
              AND s.is_active = true
              AND s.is_banned = false
            ORDER BY scr.is_required DESC, scr.field_name ASC
            """
        ),
        {"service_id": service_id},
    )
    rows = result.mappings().all()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="service not found",
        )

    declared_fields = [
        row["field_name"] for row in rows if row.get("field_name") is not None
    ]
    return rows[0]["domain"], declared_fields


async def _record_mismatch_event(
    db: AsyncSession,
    request: ContextMatchRequest,
    declared_fields: list[str],
    mismatch: MismatchResult,
) -> ContextMismatchRecord:
    """Write one append-only context mismatch event."""
    result = await db.execute(
        text(
            """
            INSERT INTO context_mismatch_events (
                service_id,
                agent_did,
                declared_fields,
                requested_fields,
                over_requested_fields,
                severity,
                resolved,
                created_at
            )
            VALUES (
                :service_id,
                :agent_did,
                :declared_fields,
                :requested_fields,
                :over_requested_fields,
                :severity,
                false,
                NOW()
            )
            RETURNING
                id,
                service_id,
                agent_did,
                declared_fields,
                requested_fields,
                over_requested_fields,
                severity,
                resolved,
                resolution_note,
                created_at
            """
        ),
        {
            "service_id": request.service_id,
            "agent_did": request.agent_did,
            "declared_fields": declared_fields,
            "requested_fields": request.requested_fields,
            "over_requested_fields": mismatch.over_requested_fields,
            "severity": mismatch.severity,
        },
    )
    row = result.mappings().first()
    return _to_mismatch_record(row)


async def match_context_request(
    db: AsyncSession,
    request: ContextMatchRequest,
) -> dict[str, object]:
    """Run the Phase 2 mismatch gate for a context match request."""
    mismatch_event: ContextMismatchRecord | None = None
    try:
        _, declared_fields = await _load_service_context(db, request.service_id)
        mismatch = detect_mismatch(
            request.requested_fields,
            ManifestContextBlock(required=declared_fields, optional=[]),
        )
        if mismatch.detected:
            mismatch_event = await _record_mismatch_event(
                db=db,
                request=request,
                declared_fields=declared_fields,
                mismatch=mismatch,
            )
            await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to evaluate context mismatch: {exc.__class__.__name__}",
        ) from exc

    if mismatch_event is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "mismatch_detected": True,
                "mismatch_id": str(mismatch_event.id),
                "service_id": str(mismatch_event.service_id),
                "declared_fields": mismatch_event.declared_fields,
                "requested_fields": mismatch_event.requested_fields,
                "over_requested_fields": mismatch_event.over_requested_fields,
                "severity": mismatch_event.severity,
            },
        )

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="context matching engine is implemented in Phase 3",
    )


async def list_mismatches(
    db: AsyncSession,
    service_id: UUID | None = None,
    severity: str | None = None,
    resolved: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ContextMismatchListResponse:
    """List context mismatch events for admin review."""
    conditions: list[str] = []
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if service_id is not None:
        conditions.append("service_id = :service_id")
        params["service_id"] = service_id
    if severity is not None:
        conditions.append("severity = :severity")
        params["severity"] = severity
    if resolved is not None:
        conditions.append("resolved = :resolved")
        params["resolved"] = resolved

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                service_id,
                agent_did,
                declared_fields,
                requested_fields,
                over_requested_fields,
                severity,
                resolved,
                resolution_note,
                created_at
            FROM context_mismatch_events
            {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    )
    rows = result.mappings().all()
    return ContextMismatchListResponse(
        total=len(rows),
        limit=limit,
        offset=offset,
        events=[_to_mismatch_record(row) for row in rows],
    )


async def _load_mismatch_for_resolution(
    db: AsyncSession,
    mismatch_id: UUID,
) -> Mapping[str, Any]:
    """Load one mismatch event and service domain for resolution."""
    result = await db.execute(
        text(
            """
            SELECT
                cme.id,
                cme.service_id,
                cme.agent_did,
                cme.declared_fields,
                cme.requested_fields,
                cme.over_requested_fields,
                cme.severity,
                cme.resolved,
                cme.resolution_note,
                cme.created_at,
                s.domain AS service_domain
            FROM context_mismatch_events cme
            JOIN services s
                ON s.id = cme.service_id
            WHERE cme.id = :mismatch_id
            """
        ),
        {"mismatch_id": mismatch_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="context mismatch event not found",
        )
    return row


async def _select_revocation_auditor(db: AsyncSession) -> str:
    """Pick an active auditor for automatic trust escalation."""
    result = await db.execute(
        text(
            """
            SELECT did
            FROM auditors
            WHERE is_active = true
            ORDER BY approved_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        )
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no active auditor available for trust escalation",
        )
    return row["did"]


async def resolve_mismatch(
    db: AsyncSession,
    mismatch_id: UUID,
    request: ContextMismatchResolveRequest,
) -> ContextMismatchResolveResponse:
    """Mark a mismatch resolved and optionally escalate it to Layer 3 revocation."""
    revocation_id = None
    tx_hash = None
    resolution_note = request.resolution_note

    try:
        mismatch_row = await _load_mismatch_for_resolution(db, mismatch_id)
        if request.escalate_to_trust:
            auditor_did = await _select_revocation_auditor(db)
            revocation = await attestation.submit_revocation(
                db=db,
                request=RevocationCreateRequest(
                    auditor_did=auditor_did,
                    service_domain=mismatch_row["service_domain"],
                    reason_code="context_mismatch",
                    evidence_package={
                        "mismatch_id": str(mismatch_row["id"]),
                        "agent_did": mismatch_row["agent_did"],
                        "severity": mismatch_row["severity"],
                        "over_requested_fields": list(
                            mismatch_row["over_requested_fields"] or []
                        ),
                    },
                ),
            )
            revocation_id = revocation.revocation_id
            tx_hash = revocation.tx_hash
            resolution_note = (
                f"{resolution_note} revocation_id={revocation.revocation_id} "
                f"tx_hash={revocation.tx_hash}"
            )

        update_result = await db.execute(
            text(
                """
                UPDATE context_mismatch_events
                SET resolved = true,
                    resolution_note = :resolution_note
                WHERE id = :mismatch_id
                RETURNING id, resolved, resolution_note
                """
            ),
            {
                "mismatch_id": mismatch_id,
                "resolution_note": resolution_note,
            },
        )
        updated_row = update_result.mappings().first()
        if updated_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="context mismatch event not found",
            )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to resolve context mismatch: {exc.__class__.__name__}",
        ) from exc

    return ContextMismatchResolveResponse(
        mismatch_id=updated_row["id"],
        resolved=updated_row["resolved"],
        resolution_note=updated_row["resolution_note"],
        escalated_to_trust=request.escalate_to_trust,
        revocation_id=revocation_id,
        tx_hash=tx_hash,
    )
