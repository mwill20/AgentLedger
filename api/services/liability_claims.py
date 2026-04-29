"""Layer 6 liability claim filing, evidence gathering, and status transitions."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from inspect import isawaitable
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.liability import (
    ClaimDetailResponse,
    ClaimResponse,
    EvidenceGatherResponse,
    EvidenceRecord,
    LiabilityDeterminationRecord,
)

CLAIM_FILING_RATE_LIMIT = 10
CLAIM_FILING_RATE_WINDOW_SECONDS = 3600
CLAIM_STATUS_CACHE_TTL_SECONDS = 60


def claim_rate_limit_key(claimant_did: str) -> str:
    """Return the Redis rate-limit key for one claimant."""
    digest = sha256(claimant_did.encode("utf-8")).hexdigest()
    return f"liability:claim_rate:{digest}"


def claim_status_cache_key(claim_id: UUID) -> str:
    """Return the Redis claim status cache key."""
    return f"liability:claim_status:{claim_id}"


async def _maybe_await(value: Any) -> Any:
    """Await async Redis calls while also supporting lightweight test doubles."""
    if isawaitable(value):
        return await value
    return value


async def _redis_call(redis: Any, method_name: str, *args: Any) -> Any:
    """Call one optional Redis method and return None when unavailable."""
    if redis is None:
        return None
    method = getattr(redis, method_name, None)
    if method is None:
        return None
    return await _maybe_await(method(*args))


async def enforce_claim_filing_rate_limit(redis: Any, claimant_did: str) -> None:
    """Limit liability claim filings to 10 per claimant per hour."""
    if redis is None or not hasattr(redis, "incr"):
        return

    key = claim_rate_limit_key(claimant_did)
    try:
        current = await _redis_call(redis, "incr", key)
        current_count = int(current or 0)
        if current_count == 1:
            await _redis_call(redis, "expire", key, CLAIM_FILING_RATE_WINDOW_SECONDS)
    except HTTPException:
        raise
    except Exception:
        return

    if current_count > CLAIM_FILING_RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="claim filing rate limit exceeded",
        )


async def invalidate_claim_status_cache(redis: Any, claim_id: UUID) -> None:
    """Drop one cached claim status if Redis is available."""
    try:
        await _redis_call(redis, "delete", claim_status_cache_key(claim_id))
    except Exception:
        return


async def cache_claim_status(redis: Any, claim_id: UUID, claim_status: str) -> None:
    """Cache one claim status for short polling paths."""
    try:
        await _redis_call(
            redis,
            "setex",
            claim_status_cache_key(claim_id),
            CLAIM_STATUS_CACHE_TTL_SECONDS,
            claim_status,
        )
    except Exception:
        return


async def refresh_claim_status_cache(
    redis: Any,
    claim_id: UUID,
    claim_status: str,
) -> None:
    """Invalidate and repopulate the short-lived claim status cache."""
    await invalidate_claim_status_cache(redis, claim_id)
    await cache_claim_status(redis, claim_id, claim_status)


async def get_cached_claim_status(redis: Any, claim_id: UUID) -> str | None:
    """Return the cached claim status when present."""
    try:
        value = await _redis_call(redis, "get", claim_status_cache_key(claim_id))
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _sync_gather_enabled(verify_sync: bool | None) -> bool:
    """Return whether claim evidence gathering should run inline."""
    if verify_sync is not None:
        return verify_sync
    return os.getenv("WORKFLOW_VERIFY_SYNC", "").lower() in {"1", "true", "yes"}


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


def _array(value: Any) -> list[Any]:
    """Normalize PostgreSQL arrays from real rows and local test doubles."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _to_claim_response(row: Mapping[str, Any]) -> ClaimResponse:
    """Map a claim row to a response model."""
    return ClaimResponse(
        claim_id=row["id"],
        execution_id=row["execution_id"],
        snapshot_id=row["snapshot_id"],
        claimant_did=row["claimant_did"],
        claim_type=row["claim_type"],
        description=row["description"],
        harm_value_usd=row["harm_value_usd"],
        status=row["status"],
        reviewer_did=row["reviewer_did"],
        resolution_note=row["resolution_note"],
        filed_at=row["filed_at"],
        evidence_gathered_at=row["evidence_gathered_at"],
        determined_at=row["determined_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _to_evidence_record(row: Mapping[str, Any]) -> EvidenceRecord:
    """Map a liability evidence row to a response model."""
    return EvidenceRecord(
        evidence_id=row["id"],
        claim_id=row["claim_id"],
        evidence_type=row["evidence_type"],
        source_table=row["source_table"],
        source_id=row["source_id"],
        source_layer=int(row["source_layer"]),
        summary=row["summary"],
        raw_data=_json_dict(row["raw_data"]),
        gathered_at=row["gathered_at"],
        created_at=row["created_at"],
    )


def _to_determination_record(
    row: Mapping[str, Any] | None,
) -> LiabilityDeterminationRecord | None:
    """Map a determination row, if present."""
    if row is None:
        return None
    return LiabilityDeterminationRecord(
        determination_id=row["id"],
        claim_id=row["claim_id"],
        determination_version=row["determination_version"],
        agent_weight=float(row["agent_weight"]),
        service_weight=float(row["service_weight"]),
        workflow_author_weight=float(row["workflow_author_weight"]),
        validator_weight=float(row["validator_weight"]),
        agent_did=row["agent_did"],
        service_id=row["service_id"],
        workflow_author_did=row["workflow_author_did"],
        validator_did=row["validator_did"],
        attribution_factors=_json_list(row["attribution_factors"]),
        confidence=float(row["confidence"]),
        determined_by=row["determined_by"],
        determined_at=row["determined_at"],
        created_at=row["created_at"],
    )


async def _load_execution(db: AsyncSession, execution_id: UUID) -> dict[str, Any]:
    """Load an execution record needed for claim filing and evidence gathering."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                workflow_id,
                agent_did,
                context_bundle_id,
                outcome,
                steps_completed,
                steps_total,
                failure_step_number,
                failure_reason,
                duration_ms,
                reported_at,
                verified,
                created_at
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


async def _load_snapshot_for_execution(
    db: AsyncSession,
    execution_id: UUID,
) -> dict[str, Any]:
    """Load the snapshot required to file a claim."""
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
            WHERE execution_id = :execution_id
            """
        ),
        {"execution_id": execution_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="liability snapshot missing for execution",
        )
    return dict(row)


async def _load_claim_row(db: AsyncSession, claim_id: UUID) -> dict[str, Any]:
    """Load a liability claim row by id."""
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


async def _count_evidence(db: AsyncSession, claim_id: UUID) -> int:
    """Count evidence records attached to a claim."""
    result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS evidence_count
            FROM liability_evidence
            WHERE claim_id = :claim_id
            """
        ),
        {"claim_id": claim_id},
    )
    row = result.mappings().first()
    return int(row["evidence_count"] if row else 0)


async def _insert_evidence_if_missing(
    db: AsyncSession,
    *,
    claim_id: UUID,
    evidence_type: str,
    source_table: str,
    source_id: UUID,
    source_layer: int,
    summary: str,
    raw_data: dict[str, Any],
) -> None:
    """Insert one evidence record unless this source was already attached."""
    existing = await db.execute(
        text(
            """
            SELECT id
            FROM liability_evidence
            WHERE claim_id = :claim_id
              AND source_table = :source_table
              AND source_id = :source_id
            """
        ),
        {
            "claim_id": claim_id,
            "source_table": source_table,
            "source_id": source_id,
        },
    )
    if existing.mappings().first() is not None:
        return

    await db.execute(
        text(
            """
            INSERT INTO liability_evidence (
                claim_id,
                evidence_type,
                source_table,
                source_id,
                source_layer,
                summary,
                raw_data,
                gathered_at,
                created_at
            )
            VALUES (
                :claim_id,
                :evidence_type,
                :source_table,
                :source_id,
                :source_layer,
                :summary,
                CAST(:raw_data AS JSONB),
                NOW(),
                NOW()
            )
            ON CONFLICT (claim_id, source_table, source_id) DO NOTHING
            """
        ),
        {
            "claim_id": claim_id,
            "evidence_type": evidence_type,
            "source_table": source_table,
            "source_id": source_id,
            "source_layer": source_layer,
            "summary": summary,
            "raw_data": json.dumps(raw_data, default=str, sort_keys=True),
        },
    )


def _snapshot_service_ids(snapshot: Mapping[str, Any]) -> list[UUID]:
    """Return unique service IDs from snapshot step trust state JSON."""
    service_ids: list[UUID] = []
    for step in _json_list(snapshot["step_trust_states"]):
        service_id = step.get("service_id")
        if service_id is not None and service_id not in service_ids:
            service_ids.append(service_id)
    return service_ids


def _snapshot_steps(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return normalized snapshot step trust states."""
    return [dict(step) for step in _json_list(snapshot["step_trust_states"])]


def _capability_tags(raw_json: Any) -> list[str]:
    """Extract manifest capability tags without copying the full manifest."""
    raw = _json_dict(raw_json)
    tags: list[str] = []
    for capability in raw.get("capabilities", []) or []:
        if not isinstance(capability, dict):
            continue
        tag = capability.get("ontology_tag") or capability.get("tag") or capability.get("id")
        if tag:
            tags.append(str(tag))
    return sorted(set(tags))


async def _gather_workflow_execution(
    db: AsyncSession,
    *,
    claim_id: UUID,
    execution: Mapping[str, Any],
) -> None:
    """Attach Source 1: workflow_executions."""
    summary = (
        f"Execution {execution['outcome']}: "
        f"{execution['steps_completed']}/{execution['steps_total']} steps, "
        f"{execution['duration_ms']}ms"
    )
    await _insert_evidence_if_missing(
        db,
        claim_id=claim_id,
        evidence_type="workflow_execution",
        source_table="workflow_executions",
        source_id=execution["id"],
        source_layer=5,
        summary=summary,
        raw_data={
            "outcome": execution["outcome"],
            "steps_completed": execution["steps_completed"],
            "steps_total": execution["steps_total"],
            "failure_step_number": execution["failure_step_number"],
            "failure_reason": execution["failure_reason"],
            "duration_ms": execution["duration_ms"],
            "verified": execution["verified"],
        },
    )


async def _gather_workflow_validation(
    db: AsyncSession,
    *,
    claim_id: UUID,
    workflow_id: UUID,
) -> None:
    """Attach Source 2: workflow_validations."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                validator_did,
                validator_domain,
                decision,
                decision_at,
                checklist,
                rejection_reason
            FROM workflow_validations
            WHERE workflow_id = :workflow_id
              AND decision = 'approved'
            ORDER BY decision_at DESC NULLS LAST, assigned_at DESC
            LIMIT 1
            """
        ),
        {"workflow_id": workflow_id},
    )
    row = result.mappings().first()
    if row is None:
        return
    await _insert_evidence_if_missing(
        db,
        claim_id=claim_id,
        evidence_type="validation_record",
        source_table="workflow_validations",
        source_id=row["id"],
        source_layer=5,
        summary=f"Workflow validated by {row['validator_did']} on {row['decision_at']}",
        raw_data={
            "validator_did": row["validator_did"],
            "validator_domain": row["validator_domain"],
            "decision": row["decision"],
            "checklist": _json_dict(row["checklist"]),
            "rejection_reason": row["rejection_reason"],
        },
    )


async def _gather_liability_snapshot(
    db: AsyncSession,
    *,
    claim_id: UUID,
    snapshot: Mapping[str, Any],
) -> None:
    """Attach Source 3: liability_snapshots."""
    await _insert_evidence_if_missing(
        db,
        claim_id=claim_id,
        evidence_type="liability_snapshot",
        source_table="liability_snapshots",
        source_id=snapshot["id"],
        source_layer=6,
        summary=(
            "Trust state snapshot: "
            f"quality_score={float(snapshot['workflow_quality_score'])}"
        ),
        raw_data={
            "workflow_quality_score": snapshot["workflow_quality_score"],
            "workflow_author_did": snapshot["workflow_author_did"],
            "workflow_validator_did": snapshot["workflow_validator_did"],
            "step_trust_states": _json_list(snapshot["step_trust_states"]),
            "context_summary": _json_dict(snapshot["context_summary"]),
            "critical_mismatch_count": snapshot["critical_mismatch_count"],
        },
    )


async def _gather_context_disclosures(
    db: AsyncSession,
    *,
    claim_id: UUID,
    execution: Mapping[str, Any],
) -> None:
    """Attach Source 4: context_disclosures in the execution window."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                service_id,
                ontology_tag,
                fields_disclosed,
                fields_withheld,
                fields_committed,
                disclosure_method,
                trust_score_at_disclosure,
                trust_tier_at_disclosure,
                erased,
                created_at
            FROM context_disclosures
            WHERE agent_did = :agent_did
              AND created_at BETWEEN
                    (:reported_at - INTERVAL '35 minutes')
                AND (:reported_at + INTERVAL '5 minutes')
            ORDER BY created_at ASC
            """
        ),
        {
            "agent_did": execution["agent_did"],
            "reported_at": execution["reported_at"],
        },
    )
    for row in result.mappings().all():
        if row["erased"]:
            summary = "[ERASED - field data unavailable]"
            raw_data: dict[str, Any] = {}
        else:
            disclosed = _array(row["fields_disclosed"])
            summary = f"Disclosed {disclosed} to service {row['service_id']}"
            raw_data = {
                "fields_disclosed": disclosed,
                "fields_withheld": _array(row["fields_withheld"]),
                "fields_committed": _array(row["fields_committed"]),
                "disclosure_method": row["disclosure_method"],
                "trust_score_at_disclosure": row["trust_score_at_disclosure"],
                "trust_tier_at_disclosure": row["trust_tier_at_disclosure"],
            }
        await _insert_evidence_if_missing(
            db,
            claim_id=claim_id,
            evidence_type="context_disclosure",
            source_table="context_disclosures",
            source_id=row["id"],
            source_layer=4,
            summary=summary,
            raw_data=raw_data,
        )


async def _gather_context_mismatches(
    db: AsyncSession,
    *,
    claim_id: UUID,
    execution: Mapping[str, Any],
) -> None:
    """Attach Source 5: context_mismatch_events in the execution window."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                service_id,
                declared_fields,
                requested_fields,
                over_requested_fields,
                severity,
                resolved,
                created_at
            FROM context_mismatch_events
            WHERE agent_did = :agent_did
              AND created_at BETWEEN
                    (:reported_at - INTERVAL '35 minutes')
                AND (:reported_at + INTERVAL '5 minutes')
            ORDER BY created_at ASC
            """
        ),
        {
            "agent_did": execution["agent_did"],
            "reported_at": execution["reported_at"],
        },
    )
    for row in result.mappings().all():
        over_requested = _array(row["over_requested_fields"])
        await _insert_evidence_if_missing(
            db,
            claim_id=claim_id,
            evidence_type="context_mismatch",
            source_table="context_mismatch_events",
            source_id=row["id"],
            source_layer=4,
            summary=(
                f"Mismatch severity={row['severity']}: "
                f"service requested {over_requested}"
            ),
            raw_data={
                "declared_fields": _array(row["declared_fields"]),
                "requested_fields": _array(row["requested_fields"]),
                "over_requested_fields": over_requested,
                "severity": row["severity"],
                "resolved": row["resolved"],
            },
        )


async def _gather_manifests(
    db: AsyncSession,
    *,
    claim_id: UUID,
    snapshot: Mapping[str, Any],
) -> None:
    """Attach Source 6: current manifests for services in the snapshot."""
    for service_id in _snapshot_service_ids(snapshot):
        result = await db.execute(
            text(
                """
                SELECT
                    m.id,
                    m.manifest_hash,
                    m.manifest_version,
                    m.raw_json,
                    m.crawled_at,
                    s.name AS service_name
                FROM manifests m
                LEFT JOIN services s ON s.id = m.service_id
                WHERE m.service_id = :service_id
                  AND m.is_current = true
                LIMIT 1
                """
            ),
            {"service_id": service_id},
        )
        row = result.mappings().first()
        if row is None:
            continue
        await _insert_evidence_if_missing(
            db,
            claim_id=claim_id,
            evidence_type="manifest_version",
            source_table="manifests",
            source_id=row["id"],
            source_layer=1,
            summary=f"Manifest version for {row['service_name']} at execution time",
            raw_data={
                "manifest_hash": row["manifest_hash"],
                "manifest_version": row["manifest_version"],
                "crawled_at": row["crawled_at"],
                "capability_tags": _capability_tags(row["raw_json"]),
            },
        )


async def _gather_service_capabilities(
    db: AsyncSession,
    *,
    claim_id: UUID,
    snapshot: Mapping[str, Any],
) -> None:
    """Attach Source 7: service_capabilities for each snapshot step."""
    for step in _snapshot_steps(snapshot):
        service_id = step.get("service_id")
        if service_id is None:
            continue
        result = await db.execute(
            text(
                """
                SELECT
                    id,
                    ontology_tag,
                    is_verified,
                    verified_at,
                    success_rate_30d
                FROM service_capabilities
                WHERE service_id = :service_id
                  AND ontology_tag = :ontology_tag
                LIMIT 1
                """
            ),
            {
                "service_id": service_id,
                "ontology_tag": step["ontology_tag"],
            },
        )
        row = result.mappings().first()
        if row is None:
            continue
        await _insert_evidence_if_missing(
            db,
            claim_id=claim_id,
            evidence_type="service_capability",
            source_table="service_capabilities",
            source_id=row["id"],
            source_layer=1,
            summary=(
                f"Capability {row['ontology_tag']} for "
                f"{step.get('service_name')}: verified={row['is_verified']}"
            ),
            raw_data={
                "ontology_tag": row["ontology_tag"],
                "is_verified": row["is_verified"],
                "verified_at": row["verified_at"],
                "success_rate_30d": row["success_rate_30d"],
            },
        )


async def _gather_revocations(
    db: AsyncSession,
    *,
    claim_id: UUID,
    execution: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    """Attach Source 8: revocation_events for services in the snapshot."""
    for service_id in _snapshot_service_ids(snapshot):
        result = await db.execute(
            text(
                """
                SELECT
                    id,
                    reason_code,
                    revoked_by,
                    evidence,
                    created_at
                FROM revocation_events
                WHERE target_type = 'service'
                  AND target_id = :target_id
                ORDER BY created_at ASC
                """
            ),
            {"target_id": str(service_id)},
        )
        for row in result.mappings().all():
            timing = "after"
            if row["created_at"] < execution["reported_at"]:
                timing = "before"
            await _insert_evidence_if_missing(
                db,
                claim_id=claim_id,
                evidence_type="trust_revocation",
                source_table="revocation_events",
                source_id=row["id"],
                source_layer=3,
                summary=f"Revocation ({timing} execution): {row['reason_code']}",
                raw_data={
                    "reason_code": row["reason_code"],
                    "revoked_at": row["created_at"],
                    "auditor_did": row["revoked_by"],
                    "evidence": _json_dict(row["evidence"]),
                },
            )


async def _load_latest_determination(
    db: AsyncSession,
    claim_id: UUID,
) -> Mapping[str, Any] | None:
    """Load the latest determination for a claim, if Phase 3 has created one."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
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
            FROM liability_determinations
            WHERE claim_id = :claim_id
            ORDER BY determination_version DESC, determined_at DESC
            LIMIT 1
            """
        ),
        {"claim_id": claim_id},
    )
    return result.mappings().first()


async def create_claim(
    *,
    execution_id: UUID,
    claimant_did: str,
    claim_type: str,
    description: str,
    harm_value_usd: float | None,
    db: AsyncSession,
    redis: Any = None,
    background_tasks: BackgroundTasks | None = None,
    verify_sync: bool | None = None,
) -> ClaimResponse:
    """File a liability claim and trigger evidence gathering."""
    await enforce_claim_filing_rate_limit(redis, claimant_did)
    try:
        execution = await _load_execution(db, execution_id)
        snapshot = await _load_snapshot_for_execution(db, execution_id)
        if claimant_did != execution["agent_did"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="claimant_did must match execution agent_did",
            )

        existing = await db.execute(
            text(
                """
                SELECT id
                FROM liability_claims
                WHERE execution_id = :execution_id
                  AND claimant_did = :claimant_did
                """
            ),
            {
                "execution_id": execution_id,
                "claimant_did": claimant_did,
            },
        )
        if existing.mappings().first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="claim already filed for this execution and claimant_did",
            )

        result = await db.execute(
            text(
                """
                INSERT INTO liability_claims (
                    execution_id,
                    snapshot_id,
                    claimant_did,
                    claim_type,
                    description,
                    harm_value_usd,
                    status,
                    filed_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    :execution_id,
                    :snapshot_id,
                    :claimant_did,
                    :claim_type,
                    :description,
                    :harm_value_usd,
                    'filed',
                    NOW(),
                    NOW(),
                    NOW()
                )
                RETURNING
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
                """
            ),
            {
                "execution_id": execution_id,
                "snapshot_id": snapshot["id"],
                "claimant_did": claimant_did,
                "claim_type": claim_type,
                "description": description,
                "harm_value_usd": harm_value_usd,
            },
        )
        claim_row = result.mappings().first()
        if claim_row is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to create liability claim",
            )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to create liability claim: {exc.__class__.__name__}",
        ) from exc

    response = _to_claim_response(claim_row)
    await cache_claim_status(redis, response.claim_id, response.status)
    if _sync_gather_enabled(verify_sync) or background_tasks is None:
        await gather_evidence(response.claim_id, db=db, redis=redis)
    else:
        background_tasks.add_task(gather_evidence, response.claim_id, db, redis)
    return response


async def gather_evidence(
    claim_id: UUID,
    db: AsyncSession,
    redis: Any = None,
) -> EvidenceGatherResponse:
    """Gather all available evidence for a liability claim idempotently."""
    try:
        claim = await _load_claim_row(db, claim_id)
        execution = await _load_execution(db, claim["execution_id"])
        snapshot = await _load_snapshot_for_execution(db, claim["execution_id"])

        await _gather_workflow_execution(db, claim_id=claim_id, execution=execution)
        await _gather_workflow_validation(
            db,
            claim_id=claim_id,
            workflow_id=execution["workflow_id"],
        )
        await _gather_liability_snapshot(db, claim_id=claim_id, snapshot=snapshot)
        await _gather_context_disclosures(db, claim_id=claim_id, execution=execution)
        await _gather_context_mismatches(db, claim_id=claim_id, execution=execution)
        await _gather_manifests(db, claim_id=claim_id, snapshot=snapshot)
        await _gather_service_capabilities(db, claim_id=claim_id, snapshot=snapshot)
        await _gather_revocations(
            db,
            claim_id=claim_id,
            execution=execution,
            snapshot=snapshot,
        )

        await db.execute(
            text(
                """
                UPDATE liability_claims
                SET status = 'evidence_gathered',
                    evidence_gathered_at = NOW(),
                    updated_at = NOW()
                WHERE id = :claim_id
                """
            ),
            {"claim_id": claim_id},
        )
        evidence_count = await _count_evidence(db, claim_id)
        await db.commit()
        await refresh_claim_status_cache(redis, claim_id, "evidence_gathered")
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to gather liability evidence: {exc.__class__.__name__}",
        ) from exc

    return EvidenceGatherResponse(
        claim_id=claim_id,
        evidence_count=evidence_count,
        status="evidence_gathered",
    )


async def get_claim_detail(
    *,
    claim_id: UUID,
    db: AsyncSession,
    redis: Any = None,
) -> ClaimDetailResponse:
    """Return a claim with evidence and latest determination."""
    claim = await _load_claim_row(db, claim_id)
    cached_status = await get_cached_claim_status(redis, claim_id)
    if cached_status:
        claim["status"] = cached_status
    evidence_result = await db.execute(
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
    evidence = [_to_evidence_record(row) for row in evidence_result.mappings().all()]
    determination = _to_determination_record(
        await _load_latest_determination(db, claim_id)
    )
    base = _to_claim_response(claim).model_dump()
    return ClaimDetailResponse(
        **base,
        evidence_count=len(evidence),
        evidence=evidence,
        determination=determination,
    )


async def resolve_claim(
    *,
    claim_id: UUID,
    resolution_note: str,
    reviewer_did: str,
    db: AsyncSession,
    redis: Any = None,
) -> ClaimResponse:
    """Resolve a determined liability claim."""
    try:
        claim = await _load_claim_row(db, claim_id)
        if claim["status"] != "determined":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="claim must be determined before it can be resolved",
            )
        result = await db.execute(
            text(
                """
                UPDATE liability_claims
                SET status = 'resolved',
                    reviewer_did = :reviewer_did,
                    resolution_note = :resolution_note,
                    resolved_at = NOW(),
                    updated_at = NOW()
                WHERE id = :claim_id
                RETURNING
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
                """
            ),
            {
                "claim_id": claim_id,
                "reviewer_did": reviewer_did,
                "resolution_note": resolution_note,
            },
        )
        row = result.mappings().first()
        await db.commit()
        await refresh_claim_status_cache(redis, claim_id, "resolved")
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to resolve liability claim: {exc.__class__.__name__}",
        ) from exc
    return _to_claim_response(row)


async def appeal_claim(
    *,
    claim_id: UUID,
    appeal_reason: str,
    claimant_did: str,
    db: AsyncSession,
    redis: Any = None,
) -> ClaimResponse:
    """Appeal a determined claim and return it to review."""
    try:
        claim = await _load_claim_row(db, claim_id)
        if claim["status"] != "determined":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="claim must be determined before it can be appealed",
            )
        if claim["claimant_did"] != claimant_did:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="claimant_did does not match claim",
            )
        result = await db.execute(
            text(
                """
                UPDATE liability_claims
                SET status = 'under_review',
                    resolution_note = :appeal_reason,
                    updated_at = NOW()
                WHERE id = :claim_id
                RETURNING
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
                """
            ),
            {
                "claim_id": claim_id,
                "appeal_reason": f"Appeal: {appeal_reason}",
            },
        )
        row = result.mappings().first()
        await db.commit()
        await refresh_claim_status_cache(redis, claim_id, "under_review")
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to appeal liability claim: {exc.__class__.__name__}",
        ) from exc
    return _to_claim_response(row)
