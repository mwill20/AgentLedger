"""Layer 6 liability attribution engine."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.liability import AttributionFactor, DeterminationResponse
from api.services import liability_claims

ACTORS = ("agent", "service", "workflow_author", "validator")

ATTRIBUTION_FACTORS: dict[str, dict[str, Any]] = {
    "service_trust_below_step_minimum": {
        "shifts_weight_to": "agent",
        "base_contribution": 0.15,
        "evidence_source": "liability_snapshots.step_trust_states",
    },
    "service_trust_tier_below_step_minimum": {
        "shifts_weight_to": "agent",
        "base_contribution": 0.20,
        "evidence_source": "liability_snapshots.step_trust_states",
    },
    "service_revoked_before_execution": {
        "shifts_weight_to": "agent",
        "base_contribution": 0.25,
        "evidence_source": "revocation_events + workflow_executions.reported_at",
    },
    "critical_context_mismatch_ignored": {
        "shifts_weight_to": "agent",
        "base_contribution": 0.20,
        "evidence_source": "context_mismatch_events",
    },
    "service_capability_not_verified": {
        "shifts_weight_to": "service",
        "base_contribution": 0.15,
        "evidence_source": "service_capabilities.is_verified",
    },
    "service_context_over_request": {
        "shifts_weight_to": "service",
        "base_contribution": 0.20,
        "evidence_source": "context_mismatch_events",
    },
    "service_revoked_after_execution_for_related_reason": {
        "shifts_weight_to": "service",
        "base_contribution": 0.15,
        "evidence_source": "revocation_events",
    },
    "workflow_quality_score_low_at_execution": {
        "shifts_weight_to": "workflow_author",
        "base_contribution": 0.10,
        "threshold": 60.0,
        "evidence_source": "liability_snapshots.workflow_quality_score",
    },
    "workflow_trust_threshold_inadequate": {
        "shifts_weight_to": "workflow_author",
        "base_contribution": 0.15,
        "evidence_source": "workflow_steps + ontology_tags.sensitivity_tier",
    },
    "workflow_no_fallback_for_critical_step": {
        "shifts_weight_to": "workflow_author",
        "base_contribution": 0.10,
        "evidence_source": "workflow_steps",
    },
    "validator_approved_inadequate_trust_threshold": {
        "shifts_weight_to": "validator",
        "base_contribution": 0.10,
        "evidence_source": "workflow_validations.checklist",
    },
    "validator_approved_non_minimal_context": {
        "shifts_weight_to": "validator",
        "base_contribution": 0.10,
        "evidence_source": "workflow_validations.checklist + context_mismatch_events",
    },
}

CLAIM_REASON_KEYWORDS = {
    "data_misuse": ["data", "privacy", "context"],
    "service_failure": ["capability", "performance", "failure"],
    "wrong_outcome": ["outcome", "result"],
    "unauthorized_action": ["scope", "unauthorized"],
    "workflow_design_flaw": ["design", "workflow"],
}


@dataclass(frozen=True)
class AttributionResult:
    """In-memory result of attribution factor evaluation."""

    weights: dict[str, float]
    applied_factors: list[AttributionFactor]
    confidence: float


def _json_dict(value: Any) -> dict[str, Any]:
    """Normalize JSONB dict values from DB rows and test doubles."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    """Normalize JSONB list values from DB rows and test doubles."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    return []


def _parse_datetime(value: Any) -> datetime | None:
    """Parse DB or JSON datetime values."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _evidence_raw(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized raw_data from an evidence row/model."""
    return _json_dict(evidence.get("raw_data"))


def _evidence_id(evidence: Mapping[str, Any]) -> UUID:
    """Return the evidence id from dict rows or API-shaped records."""
    return evidence.get("id") or evidence["evidence_id"]


def _evidence_by_type(
    evidence: list[Mapping[str, Any]],
    *types: str,
) -> list[Mapping[str, Any]]:
    """Filter evidence rows by evidence_type."""
    wanted = set(types)
    return [row for row in evidence if row.get("evidence_type") in wanted]


def _workflow_step_by_number(
    workflow_steps: list[Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    """Index workflow steps by step_number."""
    return {int(step["step_number"]): step for step in workflow_steps}


def _snapshot_steps(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return normalized step trust state from a snapshot row."""
    return [dict(step) for step in _json_list(snapshot["step_trust_states"])]


def _snapshot_checklist(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized validation checklist captured in the snapshot."""
    return _json_dict(snapshot.get("workflow_validation_checklist"))


def _service_trust_below_step_minimum(
    *,
    snapshot: Mapping[str, Any],
    workflow_steps: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    steps = _workflow_step_by_number(workflow_steps)
    for step_state in _snapshot_steps(snapshot):
        trust_score = step_state.get("trust_score")
        workflow_step = steps.get(int(step_state["step_number"]))
        if trust_score is None or workflow_step is None:
            continue
        if float(trust_score) < float(workflow_step["min_trust_score"]):
            return True, []
    return False, []


def _service_trust_tier_below_step_minimum(
    *,
    snapshot: Mapping[str, Any],
    workflow_steps: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    steps = _workflow_step_by_number(workflow_steps)
    for step_state in _snapshot_steps(snapshot):
        trust_tier = step_state.get("trust_tier")
        workflow_step = steps.get(int(step_state["step_number"]))
        if trust_tier is None or workflow_step is None:
            continue
        if int(trust_tier) < int(workflow_step["min_trust_tier"]):
            return True, []
    return False, []


def _service_revoked_before_execution(
    *,
    evidence: list[Mapping[str, Any]],
    execution: Mapping[str, Any],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    evidence_ids: list[UUID] = []
    reported_at = _parse_datetime(execution["reported_at"])
    for row in _evidence_by_type(evidence, "revocation_event", "trust_revocation"):
        revoked_at = _parse_datetime(_evidence_raw(row).get("revoked_at"))
        if reported_at is not None and revoked_at is not None and revoked_at < reported_at:
            evidence_ids.append(_evidence_id(row))
    return bool(evidence_ids), evidence_ids


def _critical_context_mismatch_ignored(
    *,
    evidence: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    evidence_ids = [
        _evidence_id(row)
        for row in _evidence_by_type(evidence, "context_mismatch")
        if _evidence_raw(row).get("severity") == "critical"
    ]
    return bool(evidence_ids), evidence_ids


def _service_capability_not_verified(
    *,
    evidence: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    evidence_ids = [
        _evidence_id(row)
        for row in _evidence_by_type(evidence, "service_capability")
        if _evidence_raw(row).get("is_verified") is False
    ]
    return bool(evidence_ids), evidence_ids


def _service_context_over_request(
    *,
    evidence: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    evidence_ids = [
        _evidence_id(row) for row in _evidence_by_type(evidence, "context_mismatch")
    ]
    return bool(evidence_ids), evidence_ids


def _service_revoked_after_execution_for_related_reason(
    *,
    claim: Mapping[str, Any],
    evidence: list[Mapping[str, Any]],
    execution: Mapping[str, Any],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    keywords = CLAIM_REASON_KEYWORDS.get(claim["claim_type"], [])
    reported_at = _parse_datetime(execution["reported_at"])
    evidence_ids: list[UUID] = []
    for row in _evidence_by_type(evidence, "revocation_event", "trust_revocation"):
        raw = _evidence_raw(row)
        revoked_at = _parse_datetime(raw.get("revoked_at"))
        reason = str(raw.get("reason_code") or "").lower()
        if (
            reported_at is not None
            and revoked_at is not None
            and revoked_at > reported_at
            and any(keyword in reason for keyword in keywords)
        ):
            evidence_ids.append(_evidence_id(row))
    return bool(evidence_ids), evidence_ids


def _workflow_quality_score_low_at_execution(
    *,
    snapshot: Mapping[str, Any],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    return float(snapshot["workflow_quality_score"]) < 60.0, []


def _workflow_trust_threshold_inadequate(
    *,
    workflow_steps: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    for step in workflow_steps:
        if not step["is_required"]:
            continue
        if int(step.get("sensitivity_tier") or 1) >= 3 and int(step["min_trust_tier"]) < 3:
            return True, []
    return False, []


def _workflow_no_fallback_for_critical_step(
    *,
    workflow_steps: list[Mapping[str, Any]],
    **_: Any,
) -> tuple[bool, list[UUID]]:
    for step in workflow_steps:
        if not step["is_required"]:
            continue
        if (
            int(step.get("sensitivity_tier") or 1) >= 3
            and step.get("fallback_step_number") is None
        ):
            return True, []
    return False, []


def _validator_approved_inadequate_trust_threshold(
    *,
    snapshot: Mapping[str, Any],
    workflow_steps: list[Mapping[str, Any]],
    **kwargs: Any,
) -> tuple[bool, list[UUID]]:
    checklist = _snapshot_checklist(snapshot)
    if checklist.get("trust_thresholds_appropriate") is not True:
        return False, []
    applies, _ = _workflow_trust_threshold_inadequate(
        workflow_steps=workflow_steps,
        **kwargs,
    )
    return applies, []


def _validator_approved_non_minimal_context(
    *,
    snapshot: Mapping[str, Any],
    evidence: list[Mapping[str, Any]],
    **kwargs: Any,
) -> tuple[bool, list[UUID]]:
    checklist = _snapshot_checklist(snapshot)
    if checklist.get("context_minimal") is not True:
        return False, []
    return _service_context_over_request(evidence=evidence, **kwargs)


FACTOR_EVALUATORS = {
    "service_trust_below_step_minimum": _service_trust_below_step_minimum,
    "service_trust_tier_below_step_minimum": _service_trust_tier_below_step_minimum,
    "service_revoked_before_execution": _service_revoked_before_execution,
    "critical_context_mismatch_ignored": _critical_context_mismatch_ignored,
    "service_capability_not_verified": _service_capability_not_verified,
    "service_context_over_request": _service_context_over_request,
    "service_revoked_after_execution_for_related_reason": (
        _service_revoked_after_execution_for_related_reason
    ),
    "workflow_quality_score_low_at_execution": _workflow_quality_score_low_at_execution,
    "workflow_trust_threshold_inadequate": _workflow_trust_threshold_inadequate,
    "workflow_no_fallback_for_critical_step": _workflow_no_fallback_for_critical_step,
    "validator_approved_inadequate_trust_threshold": (
        _validator_approved_inadequate_trust_threshold
    ),
    "validator_approved_non_minimal_context": _validator_approved_non_minimal_context,
}


def factor_applies(
    factor_name: str,
    *,
    claim: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    evidence: list[Mapping[str, Any]],
    workflow: Mapping[str, Any],
    workflow_steps: list[Mapping[str, Any]],
    execution: Mapping[str, Any],
    db: AsyncSession | None = None,
) -> tuple[bool, list[UUID]]:
    """Evaluate whether one attribution factor applies."""
    del db
    evaluator = FACTOR_EVALUATORS[factor_name]
    return evaluator(
        claim=claim,
        snapshot=snapshot,
        evidence=evidence,
        workflow=workflow,
        workflow_steps=workflow_steps,
        execution=execution,
    )


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Normalize rounded weights and keep the final sum exactly 1.0."""
    total = sum(weights.values())
    normalized = {actor: round(value / total, 4) for actor, value in weights.items()}
    delta = round(1.0 - sum(normalized.values()), 4)
    if delta:
        actor = max(normalized, key=normalized.get)
        normalized[actor] = round(normalized[actor] + delta, 4)
    return normalized


def compute_attribution(
    *,
    claim: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    evidence: list[Mapping[str, Any]],
    workflow: Mapping[str, Any],
    workflow_steps: list[Mapping[str, Any]],
    execution: Mapping[str, Any],
    db: AsyncSession | None = None,
) -> AttributionResult:
    """Compute attribution weights using the Layer 6 factor catalog."""
    weights = {
        "agent": 0.25,
        "service": 0.25,
        "workflow_author": 0.25,
        "validator": 0.25,
    }
    applied_factors: list[AttributionFactor] = []

    for factor_name, factor_def in ATTRIBUTION_FACTORS.items():
        applies, evidence_ids = factor_applies(
            factor_name,
            claim=claim,
            snapshot=snapshot,
            evidence=evidence,
            workflow=workflow,
            workflow_steps=workflow_steps,
            execution=execution,
            db=db,
        )
        if not applies:
            continue

        actor = factor_def["shifts_weight_to"]
        contribution = float(factor_def["base_contribution"])
        other_actors = [candidate for candidate in ACTORS if candidate != actor]
        per_other = contribution / len(other_actors)
        weights[actor] += contribution
        for other in other_actors:
            weights[other] = max(0.0, weights[other] - per_other)
        applied_factors.append(
            AttributionFactor(
                factor=factor_name,
                actor=actor,
                weight_contribution=contribution,
                evidence_ids=evidence_ids,
            )
        )

    confidence = min(1.0, 0.3 + len(applied_factors) * 0.1)
    return AttributionResult(
        weights=_normalize_weights(weights),
        applied_factors=applied_factors,
        confidence=round(confidence, 2),
    )


async def _load_claim(db: AsyncSession, claim_id: UUID) -> dict[str, Any]:
    """Load a claim for determination."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                execution_id,
                snapshot_id,
                claimant_did,
                claim_type,
                description,
                harm_value_usd,
                status,
                reviewer_did,
                resolution_note,
                filed_at,
                evidence_gathered_at,
                determined_at,
                resolved_at,
                created_at,
                updated_at
            FROM liability_claims
            WHERE id = :claim_id
            """
        ),
        {"claim_id": claim_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="liability claim not found",
        )
    return dict(row)


async def _load_snapshot(db: AsyncSession, snapshot_id: UUID) -> dict[str, Any]:
    """Load the liability snapshot for a claim."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                execution_id,
                workflow_id,
                agent_did,
                captured_at,
                workflow_quality_score,
                workflow_author_did,
                workflow_validator_did,
                workflow_validation_checklist,
                step_trust_states,
                context_summary,
                critical_mismatch_count,
                agent_profile_default_policy,
                created_at
            FROM liability_snapshots
            WHERE id = :snapshot_id
            """
        ),
        {"snapshot_id": snapshot_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="liability snapshot missing for claim",
        )
    return dict(row)


async def _load_evidence(db: AsyncSession, claim_id: UUID) -> list[dict[str, Any]]:
    """Load all evidence for a claim."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                claim_id,
                evidence_type,
                source_table,
                source_id,
                source_layer,
                summary,
                raw_data,
                gathered_at,
                created_at
            FROM liability_evidence
            WHERE claim_id = :claim_id
            ORDER BY gathered_at ASC, id ASC
            """
        ),
        {"claim_id": claim_id},
    )
    return [dict(row) for row in result.mappings().all()]


async def _load_workflow(db: AsyncSession, workflow_id: UUID) -> dict[str, Any]:
    """Load workflow metadata for attribution."""
    result = await db.execute(
        text(
            """
            SELECT id, quality_score, author_did, status
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
    return dict(row)


async def _load_workflow_steps(
    db: AsyncSession,
    workflow_id: UUID,
) -> list[dict[str, Any]]:
    """Load workflow steps with ontology sensitivity tiers."""
    result = await db.execute(
        text(
            """
            SELECT
                ws.id,
                ws.step_number,
                ws.ontology_tag,
                ws.service_id,
                ws.is_required,
                ws.fallback_step_number,
                ws.min_trust_tier,
                ws.min_trust_score,
                COALESCE(ot.sensitivity_tier, 1) AS sensitivity_tier
            FROM workflow_steps ws
            LEFT JOIN ontology_tags ot ON ot.tag = ws.ontology_tag
            WHERE ws.workflow_id = :workflow_id
            ORDER BY ws.step_number ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    return [dict(row) for row in result.mappings().all()]


async def _load_execution(db: AsyncSession, execution_id: UUID) -> dict[str, Any]:
    """Load execution metadata for attribution."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                workflow_id,
                agent_did,
                outcome,
                steps_completed,
                steps_total,
                failure_step_number,
                failure_reason,
                duration_ms,
                reported_at,
                verified
            FROM workflow_executions
            WHERE id = :execution_id
            """
        ),
        {"execution_id": execution_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="execution not found",
        )
    return dict(row)


async def _next_determination_version(db: AsyncSession, claim_id: UUID) -> int:
    """Return the next determination version for a claim."""
    result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS determination_count
            FROM liability_determinations
            WHERE claim_id = :claim_id
            """
        ),
        {"claim_id": claim_id},
    )
    row = result.mappings().first()
    return int(row["determination_count"] if row else 0) + 1


def _first_snapshot_service_id(snapshot: Mapping[str, Any]) -> UUID | None:
    """Return the first service_id in snapshot step trust states."""
    for step in _snapshot_steps(snapshot):
        service_id = step.get("service_id")
        if service_id is not None:
            return service_id
    return None


def _response_from_inserted_row(
    row: Mapping[str, Any],
    result: AttributionResult,
) -> DeterminationResponse:
    """Build the public response from an inserted determination row."""
    return DeterminationResponse(
        determination_id=row["id"],
        claim_id=row["claim_id"],
        determination_version=row["determination_version"],
        attribution={
            "agent": float(row["agent_weight"]),
            "service": float(row["service_weight"]),
            "workflow_author": float(row["workflow_author_weight"]),
            "validator": float(row["validator_weight"]),
        },
        applied_factors=result.applied_factors,
        confidence=float(row["confidence"]),
        determined_by=row["determined_by"],
        determined_at=row["determined_at"],
    )


async def determine_claim(
    *,
    claim_id: UUID,
    reviewer_did: str | None,
    db: AsyncSession,
    redis: Any = None,
) -> DeterminationResponse:
    """Compute attribution for a claim and mark it determined."""
    try:
        claim = await _load_claim(db, claim_id)
        if claim["status"] not in {"evidence_gathered", "under_review"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="claim must be evidence_gathered or under_review",
            )

        snapshot = await _load_snapshot(db, claim["snapshot_id"])
        evidence = await _load_evidence(db, claim_id)
        workflow = await _load_workflow(db, snapshot["workflow_id"])
        workflow_steps = await _load_workflow_steps(db, snapshot["workflow_id"])
        execution = await _load_execution(db, claim["execution_id"])
        result = compute_attribution(
            claim=claim,
            snapshot=snapshot,
            evidence=evidence,
            workflow=workflow,
            workflow_steps=workflow_steps,
            execution=execution,
            db=db,
        )
        determination_version = await _next_determination_version(db, claim_id)
        determined_by = "reviewer" if reviewer_did else "system"

        insert_result = await db.execute(
            text(
                """
                INSERT INTO liability_determinations (
                    claim_id,
                    determination_version,
                    agent_weight,
                    service_weight,
                    workflow_author_weight,
                    validator_weight,
                    agent_did,
                    service_id,
                    workflow_author_did,
                    validator_did,
                    attribution_factors,
                    confidence,
                    determined_by,
                    determined_at,
                    created_at
                )
                VALUES (
                    :claim_id,
                    :determination_version,
                    :agent_weight,
                    :service_weight,
                    :workflow_author_weight,
                    :validator_weight,
                    :agent_did,
                    :service_id,
                    :workflow_author_did,
                    :validator_did,
                    CAST(:attribution_factors AS JSONB),
                    :confidence,
                    :determined_by,
                    NOW(),
                    NOW()
                )
                RETURNING
                    id,
                    claim_id,
                    determination_version,
                    agent_weight,
                    service_weight,
                    workflow_author_weight,
                    validator_weight,
                    confidence,
                    determined_by,
                    determined_at
                """
            ),
            {
                "claim_id": claim_id,
                "determination_version": determination_version,
                "agent_weight": result.weights["agent"],
                "service_weight": result.weights["service"],
                "workflow_author_weight": result.weights["workflow_author"],
                "validator_weight": result.weights["validator"],
                "agent_did": claim["claimant_did"],
                "service_id": _first_snapshot_service_id(snapshot),
                "workflow_author_did": snapshot["workflow_author_did"],
                "validator_did": snapshot["workflow_validator_did"],
                "attribution_factors": json.dumps(
                    [
                        factor.model_dump(mode="json")
                        for factor in result.applied_factors
                    ],
                    sort_keys=True,
                ),
                "confidence": result.confidence,
                "determined_by": determined_by,
            },
        )
        row = insert_result.mappings().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to create liability determination",
            )

        await db.execute(
            text(
                """
                UPDATE liability_claims
                SET status = 'determined',
                    reviewer_did = COALESCE(:reviewer_did, reviewer_did),
                    determined_at = NOW(),
                    updated_at = NOW()
                WHERE id = :claim_id
                """
            ),
            {"claim_id": claim_id, "reviewer_did": reviewer_did},
        )
        await db.commit()
        await liability_claims.refresh_claim_status_cache(redis, claim_id, "determined")
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to determine liability claim: {exc.__class__.__name__}",
        ) from exc

    return _response_from_inserted_row(row, result)
