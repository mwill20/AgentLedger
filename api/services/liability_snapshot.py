"""Layer 6 liability snapshot creation and read paths."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.liability import (
    LiabilitySnapshotListResponse,
    LiabilitySnapshotRecord,
    LiabilitySnapshotSummary,
    SnapshotContextSummary,
    SnapshotStepTrustState,
)

EXECUTION_WINDOW_BEFORE_MINUTES = 35
EXECUTION_WINDOW_AFTER_MINUTES = 5


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


def _dedupe_sorted(values: list[str]) -> list[str]:
    """Return deterministic field-name lists for snapshot JSON."""
    return sorted({value for value in values if value})


async def _load_existing_snapshot(
    db: AsyncSession,
    execution_id: UUID,
) -> Mapping[str, Any] | None:
    """Return an existing snapshot row for idempotent creation/read."""
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
    return result.mappings().first()


async def _load_execution(db: AsyncSession, execution_id: UUID) -> dict[str, Any]:
    """Load the workflow execution being snapshotted."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                workflow_id,
                agent_did,
                context_bundle_id,
                reported_at
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


async def _load_workflow_state(
    db: AsyncSession,
    workflow_id: UUID,
) -> dict[str, Any]:
    """Load workflow state captured by a liability snapshot."""
    result = await db.execute(
        text(
            """
            SELECT id, quality_score, author_did
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


async def _load_latest_validation(
    db: AsyncSession,
    workflow_id: UUID,
) -> dict[str, Any] | None:
    """Load the latest approved validation record for a workflow."""
    result = await db.execute(
        text(
            """
            SELECT validator_did, checklist
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
    return None if row is None else dict(row)


async def _load_step_trust_states(
    db: AsyncSession,
    execution: dict[str, Any],
) -> list[dict[str, Any]]:
    """Capture per-step service trust state at execution report time."""
    result = await db.execute(
        text(
            """
            SELECT
                ws.step_number,
                ws.ontology_tag,
                ws.min_trust_tier,
                ws.min_trust_score,
                COALESCE(ws.service_id, cd.service_id) AS service_id,
                s.name AS service_name,
                s.trust_score,
                s.trust_tier
            FROM workflow_steps ws
            LEFT JOIN LATERAL (
                SELECT context_disclosures.service_id
                FROM context_disclosures
                WHERE context_disclosures.agent_did = :agent_did
                  AND context_disclosures.ontology_tag = ws.ontology_tag
                  AND context_disclosures.created_at BETWEEN
                        (:reported_at - INTERVAL '35 minutes')
                    AND (:reported_at + INTERVAL '5 minutes')
                ORDER BY context_disclosures.created_at DESC
                LIMIT 1
            ) cd ON ws.service_id IS NULL
            LEFT JOIN services s ON s.id = COALESCE(ws.service_id, cd.service_id)
            WHERE ws.workflow_id = :workflow_id
            ORDER BY ws.step_number ASC
            """
        ),
        {
            "workflow_id": execution["workflow_id"],
            "agent_did": execution["agent_did"],
            "reported_at": execution["reported_at"],
        },
    )
    states = []
    for row in result.mappings().all():
        service_id = row["service_id"]
        states.append(
            {
                "step_number": row["step_number"],
                "ontology_tag": row["ontology_tag"],
                "service_id": service_id,
                "service_name": row["service_name"],
                "min_trust_tier": row["min_trust_tier"],
                "min_trust_score": float(row["min_trust_score"]),
                "trust_score": (
                    None if row["trust_score"] is None else float(row["trust_score"])
                ),
                "trust_tier": row["trust_tier"],
                "trust_score_source": (
                    "services.trust_score_at_snapshot"
                    if service_id is not None
                    else "unresolved_service"
                ),
            }
        )
    return states


async def _load_context_summary(
    db: AsyncSession,
    execution: dict[str, Any],
    service_ids: list[UUID],
) -> tuple[dict[str, Any], int]:
    """Capture context disclosure fields and mismatch counts."""
    disclosure_result = await db.execute(
        text(
            """
            SELECT fields_disclosed, fields_withheld, fields_committed
            FROM context_disclosures
            WHERE agent_did = :agent_did
              AND created_at BETWEEN
                    (:reported_at - INTERVAL '35 minutes')
                AND (:reported_at + INTERVAL '5 minutes')
            """
        ),
        {
            "agent_did": execution["agent_did"],
            "reported_at": execution["reported_at"],
        },
    )
    disclosed: list[str] = []
    withheld: list[str] = []
    committed: list[str] = []
    for row in disclosure_result.mappings().all():
        disclosed.extend(list(row["fields_disclosed"] or []))
        withheld.extend(list(row["fields_withheld"] or []))
        committed.extend(list(row["fields_committed"] or []))

    mismatch_result = await db.execute(
        text(
            """
            SELECT
                COUNT(*) AS mismatch_count,
                COUNT(*) FILTER (WHERE severity = 'critical') AS critical_count
            FROM context_mismatch_events
            WHERE agent_did = :agent_did
              AND (:service_ids_empty OR service_id = ANY(CAST(:service_ids AS UUID[])))
              AND created_at BETWEEN
                    (:reported_at - INTERVAL '35 minutes')
                AND (:reported_at + INTERVAL '5 minutes')
            """
        ),
        {
            "agent_did": execution["agent_did"],
            "service_ids": service_ids,
            "service_ids_empty": not service_ids,
            "reported_at": execution["reported_at"],
        },
    )
    mismatch_row = mismatch_result.mappings().first()
    mismatch_count = int(mismatch_row["mismatch_count"] if mismatch_row else 0)
    critical_count = int(mismatch_row["critical_count"] if mismatch_row else 0)
    return (
        {
            "fields_disclosed": _dedupe_sorted(disclosed),
            "fields_withheld": _dedupe_sorted(withheld),
            "fields_committed": _dedupe_sorted(committed),
            "mismatch_count": mismatch_count,
        },
        critical_count,
    )


async def _load_agent_profile_default_policy(
    db: AsyncSession,
    agent_did: str,
) -> str | None:
    """Capture the agent profile default policy active at snapshot time."""
    result = await db.execute(
        text(
            """
            SELECT default_policy
            FROM context_profiles
            WHERE agent_did = :agent_did
              AND is_active = true
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"agent_did": agent_did},
    )
    row = result.mappings().first()
    return None if row is None else row["default_policy"]


def _to_snapshot_record(row: Mapping[str, Any]) -> LiabilitySnapshotRecord:
    """Map a stored liability snapshot row to the API model."""
    return LiabilitySnapshotRecord(
        snapshot_id=row["id"],
        execution_id=row["execution_id"],
        workflow_id=row["workflow_id"],
        agent_did=row["agent_did"],
        captured_at=row["captured_at"],
        workflow_quality_score=float(row["workflow_quality_score"]),
        workflow_author_did=row["workflow_author_did"],
        workflow_validator_did=row["workflow_validator_did"],
        workflow_validation_checklist=(
            None
            if row["workflow_validation_checklist"] is None
            else _json_dict(row["workflow_validation_checklist"])
        ),
        step_trust_states=[
            SnapshotStepTrustState.model_validate(item)
            for item in _json_list(row["step_trust_states"])
        ],
        context_summary=SnapshotContextSummary.model_validate(
            _json_dict(row["context_summary"])
        ),
        critical_mismatch_count=int(row["critical_mismatch_count"]),
        agent_profile_default_policy=row["agent_profile_default_policy"],
        created_at=row["created_at"],
    )


def _to_snapshot_summary(row: Mapping[str, Any]) -> LiabilitySnapshotSummary:
    """Map a list query row to a snapshot summary model."""
    return LiabilitySnapshotSummary(
        snapshot_id=row["id"],
        execution_id=row["execution_id"],
        workflow_id=row["workflow_id"],
        agent_did=row["agent_did"],
        workflow_quality_score=float(row["workflow_quality_score"]),
        critical_mismatch_count=int(row["critical_mismatch_count"]),
        captured_at=row["captured_at"],
        created_at=row["created_at"],
    )


async def create_snapshot(
    db: AsyncSession,
    execution_id: UUID,
) -> LiabilitySnapshotRecord:
    """Synchronously create the liability snapshot for one execution.

    This function intentionally does not commit. It is called inside the Layer 5
    execution-report transaction so a failed snapshot rolls back the execution.
    """
    existing = await _load_existing_snapshot(db, execution_id)
    if existing is not None:
        return _to_snapshot_record(existing)

    execution = await _load_execution(db, execution_id)
    workflow = await _load_workflow_state(db, execution["workflow_id"])
    validation = await _load_latest_validation(db, execution["workflow_id"])
    step_trust_states = await _load_step_trust_states(db, execution)
    service_ids = [
        state["service_id"]
        for state in step_trust_states
        if state["service_id"] is not None
    ]
    context_summary, critical_mismatch_count = await _load_context_summary(
        db,
        execution,
        service_ids,
    )
    agent_policy = await _load_agent_profile_default_policy(db, execution["agent_did"])

    result = await db.execute(
        text(
            """
            INSERT INTO liability_snapshots (
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
            )
            VALUES (
                :execution_id,
                :workflow_id,
                :agent_did,
                NOW(),
                :workflow_quality_score,
                :workflow_author_did,
                :workflow_validator_did,
                CAST(:workflow_validation_checklist AS JSONB),
                CAST(:step_trust_states AS JSONB),
                CAST(:context_summary AS JSONB),
                :critical_mismatch_count,
                :agent_profile_default_policy,
                NOW()
            )
            RETURNING
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
            """
        ),
        {
            "execution_id": execution_id,
            "workflow_id": execution["workflow_id"],
            "agent_did": execution["agent_did"],
            "workflow_quality_score": float(workflow["quality_score"]),
            "workflow_author_did": workflow["author_did"],
            "workflow_validator_did": (
                None if validation is None else validation["validator_did"]
            ),
            "workflow_validation_checklist": (
                None
                if validation is None
                else json.dumps(_json_dict(validation["checklist"]), sort_keys=True)
            ),
            "step_trust_states": json.dumps(step_trust_states, default=str, sort_keys=True),
            "context_summary": json.dumps(context_summary, sort_keys=True),
            "critical_mismatch_count": critical_mismatch_count,
            "agent_profile_default_policy": agent_policy,
        },
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to create liability snapshot",
        )
    return _to_snapshot_record(row)


async def get_snapshot_by_execution(
    db: AsyncSession,
    execution_id: UUID,
) -> LiabilitySnapshotRecord:
    """Return the liability snapshot for one execution."""
    row = await _load_existing_snapshot(db, execution_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="liability snapshot not found",
        )
    return _to_snapshot_record(row)


async def list_snapshots(
    db: AsyncSession,
    *,
    workflow_id: UUID | None = None,
    agent_did: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> LiabilitySnapshotListResponse:
    """List liability snapshots with optional admin filters."""
    where = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if workflow_id is not None:
        where.append("workflow_id = :workflow_id")
        params["workflow_id"] = workflow_id
    if agent_did is not None:
        where.append("agent_did = :agent_did")
        params["agent_did"] = agent_did
    if from_date is not None:
        where.append("captured_at >= :from_date")
        params["from_date"] = from_date
    if to_date is not None:
        where.append("captured_at <= :to_date")
        params["to_date"] = to_date

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    count_result = await db.execute(
        text(f"SELECT COUNT(*) AS total FROM liability_snapshots {where_sql}"),
        params,
    )
    count_row = count_result.mappings().first()
    total = int(count_row["total"] if count_row else 0)

    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                execution_id,
                workflow_id,
                agent_did,
                workflow_quality_score,
                critical_mismatch_count,
                captured_at,
                created_at
            FROM liability_snapshots
            {where_sql}
            ORDER BY captured_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    )
    return LiabilitySnapshotListResponse(
        total=total,
        limit=limit,
        offset=offset,
        snapshots=[_to_snapshot_summary(row) for row in result.mappings().all()],
    )
