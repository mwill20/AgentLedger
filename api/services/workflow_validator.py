"""Layer 5 human validation queue management."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.workflow import (
    ValidationAssignRequest,
    ValidationResponse,
    ValidatorDecisionRequest,
    WorkflowRecord,
)
from api.services import workflow_registry


def compute_spec_hash(spec: dict[str, Any]) -> str:
    """Return the deterministic SHA-256 hash for a workflow spec."""
    return sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def compute_initial_quality_score(avg_step_trust: float) -> float:
    """Compute the publication-time quality score for a newly published workflow."""
    validation_score = 1.0
    success_rate = 0.0
    verification_rate = 0.0
    volume_factor = 0.0
    raw = (
        validation_score * 0.35
        + success_rate * 0.30 * volume_factor
        + verification_rate * 0.20
        + avg_step_trust * 0.15
    )
    if verification_rate < 0.5:
        raw = min(raw, 0.70)
    return round(raw * 100, 2)


def _json_dict(value: Any) -> dict[str, Any]:
    """Normalize JSONB values from real DB rows and test doubles."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _to_validation_response(row: Mapping[str, Any]) -> ValidationResponse:
    """Map a workflow_validations row to an API model."""
    return ValidationResponse(
        validation_id=row["id"],
        workflow_id=row["workflow_id"],
        validator_did=row["validator_did"],
        validator_domain=row["validator_domain"],
        assigned_at=row["assigned_at"],
        decision=row["decision"],
        decision_at=row["decision_at"],
        rejection_reason=row["rejection_reason"],
        revision_notes=row["revision_notes"],
        checklist=_json_dict(row["checklist"]),
    )


async def _load_workflow_for_validation(
    db: AsyncSession,
    workflow_id: UUID,
) -> Mapping[str, Any]:
    """Load one workflow row needed by validation flows."""
    result = await db.execute(
        text(
            """
            SELECT id, status, spec
            FROM workflows
            WHERE id = :workflow_id
            """
        ),
        {"workflow_id": workflow_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found",
        )
    return row


async def _load_pending_validation(
    db: AsyncSession,
    workflow_id: UUID,
) -> Mapping[str, Any] | None:
    """Load the active pending validation record for a workflow."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                workflow_id,
                validator_did,
                validator_domain,
                assigned_at,
                decision,
                decision_at,
                rejection_reason,
                revision_notes,
                checklist
            FROM workflow_validations
            WHERE workflow_id = :workflow_id
              AND decision IS NULL
            ORDER BY assigned_at DESC, created_at DESC
            LIMIT 1
            """
        ),
        {"workflow_id": workflow_id},
    )
    return result.mappings().first()


async def _avg_step_trust(db: AsyncSession, workflow_id: UUID) -> float:
    """Return normalized average trust for pinned workflow services."""
    result = await db.execute(
        text(
            """
            SELECT ws.service_id, s.trust_score
            FROM workflow_steps ws
            LEFT JOIN services s ON s.id = ws.service_id
            WHERE ws.workflow_id = :workflow_id
              AND ws.service_id IS NOT NULL
            ORDER BY ws.step_number ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    rows = list(result.mappings().all())
    if not rows:
        return 0.5

    scores: list[float] = []
    for row in rows:
        score = row.get("trust_score")
        if score is None:
            scores.append(0.5)
        else:
            scores.append(max(0.0, min(float(score) / 100.0, 1.0)))
    return sum(scores) / len(scores)


async def assign_workflow_to_validator(
    db: AsyncSession,
    workflow_id: UUID,
    request: ValidationAssignRequest,
) -> ValidationResponse:
    """Assign a draft workflow to a human validator."""
    try:
        workflow = await _load_workflow_for_validation(db, workflow_id)
        if workflow["status"] != "draft":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="workflow must be draft to assign validation",
            )

        pending = await _load_pending_validation(db, workflow_id)
        if pending is not None and (
            pending["validator_did"] != workflow_registry.VALIDATION_QUEUE_DID
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="active validation already assigned",
            )

        if pending is None:
            validation_result = await db.execute(
                text(
                    """
                    INSERT INTO workflow_validations (
                        workflow_id,
                        validator_did,
                        validator_domain,
                        checklist,
                        created_at
                    )
                    VALUES (
                        :workflow_id,
                        :validator_did,
                        :validator_domain,
                        '{}'::jsonb,
                        NOW()
                    )
                    RETURNING
                        id,
                        workflow_id,
                        validator_did,
                        validator_domain,
                        assigned_at,
                        decision,
                        decision_at,
                        rejection_reason,
                        revision_notes,
                        checklist
                    """
                ),
                {
                    "workflow_id": workflow_id,
                    "validator_did": request.validator_did,
                    "validator_domain": request.validator_domain,
                },
            )
        else:
            validation_result = await db.execute(
                text(
                    """
                    UPDATE workflow_validations
                    SET validator_did = :validator_did,
                        validator_domain = :validator_domain,
                        assigned_at = NOW()
                    WHERE id = :validation_id
                    RETURNING
                        id,
                        workflow_id,
                        validator_did,
                        validator_domain,
                        assigned_at,
                        decision,
                        decision_at,
                        rejection_reason,
                        revision_notes,
                        checklist
                    """
                ),
                {
                    "validation_id": pending["id"],
                    "validator_did": request.validator_did,
                    "validator_domain": request.validator_domain,
                },
            )

        validation_row = validation_result.mappings().first()
        await db.execute(
            text(
                """
                UPDATE workflows
                SET status = 'in_review',
                    updated_at = NOW()
                WHERE id = :workflow_id
                """
            ),
            {"workflow_id": workflow_id},
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to assign workflow validation: {exc.__class__.__name__}",
        ) from exc

    return _to_validation_response(validation_row)


async def record_validator_decision(
    db: AsyncSession,
    workflow_id: UUID,
    request: ValidatorDecisionRequest,
) -> WorkflowRecord:
    """Record a validator decision and transition workflow state."""
    try:
        validation = await _load_pending_validation(db, workflow_id)
        if validation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no active validation for this workflow",
            )
        if validation["validator_did"] != request.validator_did:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="validator_did does not match assigned validator",
            )

        workflow = await _load_workflow_for_validation(db, workflow_id)
        spec = _json_dict(workflow["spec"])
        workflow_status = "draft"
        workflow_params: dict[str, Any] = {"workflow_id": workflow_id}
        validation_params: dict[str, Any] = {
            "validation_id": validation["id"],
            "decision": request.decision,
            "checklist": json.dumps(request.checklist, sort_keys=True),
            "rejection_reason": request.rejection_reason,
            "revision_notes": request.revision_notes,
        }

        if request.decision == "approved":
            spec_hash = compute_spec_hash(spec)
            quality_score = compute_initial_quality_score(
                await _avg_step_trust(db, workflow_id)
            )
            workflow_status = "published"
            workflow_params.update(
                {
                    "spec_hash": spec_hash,
                    "quality_score": quality_score,
                    "publish": True,
                }
            )
            validation_params["rejection_reason"] = None
            validation_params["revision_notes"] = None
        elif request.decision == "rejected":
            workflow_status = "rejected"
            workflow_params.update(
                {"spec_hash": None, "quality_score": None, "publish": False}
            )
            validation_params["revision_notes"] = None
        else:
            workflow_status = "draft"
            workflow_params.update(
                {"spec_hash": None, "quality_score": None, "publish": False}
            )
            validation_params["rejection_reason"] = None

        await db.execute(
            text(
                """
                UPDATE workflow_validations
                SET decision = :decision,
                    decision_at = NOW(),
                    checklist = CAST(:checklist AS JSONB),
                    rejection_reason = :rejection_reason,
                    revision_notes = :revision_notes
                WHERE id = :validation_id
                """
            ),
            validation_params,
        )

        if request.decision == "approved":
            await db.execute(
                text(
                    """
                    UPDATE workflows
                    SET status = 'published',
                        spec_hash = :spec_hash,
                        quality_score = :quality_score,
                        published_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :workflow_id
                    """
                ),
                workflow_params,
            )
        else:
            await db.execute(
                text(
                    """
                    UPDATE workflows
                    SET status = :status,
                        updated_at = NOW()
                    WHERE id = :workflow_id
                    """
                ),
                {"workflow_id": workflow_id, "status": workflow_status},
            )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to record validator decision: {exc.__class__.__name__}",
        ) from exc

    return await workflow_registry.get_workflow(db=db, workflow_id=workflow_id)
