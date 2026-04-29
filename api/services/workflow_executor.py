"""Layer 5 workflow execution outcome reporting and verification."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.workflow import ExecutionReportRequest, ExecutionReportResponse
from api.services.workflow_ranker import compute_workflow_quality_score


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying a stored workflow execution."""

    execution_id: UUID
    verified: bool
    quality_score: float


def _sync_verification_enabled(verify_sync: bool | None) -> bool:
    """Return whether execution verification should run inline."""
    if verify_sync is not None:
        return verify_sync
    return os.getenv("WORKFLOW_VERIFY_SYNC", "").lower() in {"1", "true", "yes"}


def _scalar_id(result: Any) -> UUID:
    """Read a returned UUID from SQLAlchemy or the local test double."""
    if hasattr(result, "scalar_one"):
        return result.scalar_one()
    row = result.mappings().first()
    return row["id"]


async def _load_published_workflow(db: AsyncSession, workflow_id: UUID) -> dict[str, Any]:
    """Require the workflow to exist and be published."""
    result = await db.execute(
        text(
            """
            SELECT id, status
            FROM workflows
            WHERE id = :workflow_id
            """
        ),
        {"workflow_id": workflow_id},
    )
    row = result.mappings().first()
    if row is None or row["status"] != "published":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found or not published",
        )
    return dict(row)


async def _ensure_agent_exists(db: AsyncSession, agent_did: str) -> None:
    """Require execution reports to come from a registered active agent."""
    result = await db.execute(
        text(
            """
            SELECT did
            FROM agent_identities
            WHERE did = :agent_did
              AND is_active = true
              AND is_revoked = false
            """
        ),
        {"agent_did": agent_did},
    )
    if result.mappings().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent identity not found",
        )


async def _ensure_context_bundle_belongs(
    db: AsyncSession,
    *,
    workflow_id: UUID,
    agent_did: str,
    context_bundle_id: UUID | None,
) -> None:
    """Require a provided context bundle to belong to this workflow and agent."""
    if context_bundle_id is None:
        return
    result = await db.execute(
        text(
            """
            SELECT id
            FROM workflow_context_bundles
            WHERE id = :context_bundle_id
              AND workflow_id = :workflow_id
              AND agent_did = :agent_did
            """
        ),
        {
            "context_bundle_id": context_bundle_id,
            "workflow_id": workflow_id,
            "agent_did": agent_did,
        },
    )
    if result.mappings().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="context bundle not found for this workflow and agent_did",
        )


async def _insert_execution(
    db: AsyncSession,
    *,
    workflow_id: UUID,
    agent_did: str,
    context_bundle_id: UUID | None,
    outcome: str,
    steps_completed: int,
    steps_total: int,
    failure_step_number: int | None,
    failure_reason: str | None,
    duration_ms: int | None,
) -> UUID:
    """Insert an unverified execution report."""
    result = await db.execute(
        text(
            """
            INSERT INTO workflow_executions (
                workflow_id,
                agent_did,
                context_bundle_id,
                outcome,
                steps_completed,
                steps_total,
                failure_step_number,
                failure_reason,
                duration_ms,
                verified,
                verified_at,
                reported_at,
                created_at
            )
            VALUES (
                :workflow_id,
                :agent_did,
                :context_bundle_id,
                :outcome,
                :steps_completed,
                :steps_total,
                :failure_step_number,
                :failure_reason,
                :duration_ms,
                false,
                NULL,
                NOW(),
                NOW()
            )
            RETURNING id
            """
        ),
        {
            "workflow_id": workflow_id,
            "agent_did": agent_did,
            "context_bundle_id": context_bundle_id,
            "outcome": outcome,
            "steps_completed": steps_completed,
            "steps_total": steps_total,
            "failure_step_number": failure_step_number,
            "failure_reason": failure_reason,
            "duration_ms": duration_ms,
        },
    )
    return _scalar_id(result)


async def _increment_workflow_counters(
    db: AsyncSession,
    *,
    workflow_id: UUID,
    outcome: str,
) -> None:
    """Atomically increment execution counters for one workflow."""
    await db.execute(
        text(
            """
            UPDATE workflows
            SET execution_count = execution_count + 1,
                success_count = success_count
                    + CASE WHEN :outcome = 'success' THEN 1 ELSE 0 END,
                failure_count = failure_count
                    + CASE WHEN :outcome = 'failure' THEN 1 ELSE 0 END,
                updated_at = NOW()
            WHERE id = :workflow_id
            """
        ),
        {
            "workflow_id": workflow_id,
            "outcome": outcome,
        },
    )


async def _invalidate_rank_cache(redis, workflow_id: UUID) -> None:
    """Best-effort invalidation of cached workflow rank responses."""
    if redis is None:
        return
    pattern = f"workflow:rank:{workflow_id}:*"
    try:
        if hasattr(redis, "scan_iter"):
            keys = []
            async for key in redis.scan_iter(match=pattern):
                keys.append(key)
            if keys:
                await redis.delete(*keys)
            return
        if hasattr(redis, "keys"):
            keys = await redis.keys(pattern)
            if keys:
                await redis.delete(*keys)
            return
        if hasattr(redis, "store"):
            for key in list(redis.store):
                if fnmatch.fnmatch(str(key), pattern):
                    redis.store.pop(key, None)
    except Exception:
        return


async def _recompute_and_store_quality(
    *,
    db: AsyncSession,
    workflow_id: UUID,
    redis=None,
) -> float:
    """Recompute quality score, persist it, and invalidate stale rank cache."""
    from api.services import workflow_registry

    quality_score = await compute_workflow_quality_score(
        workflow_id=workflow_id,
        db=db,
        redis=redis,
    )
    await db.execute(
        text(
            """
            UPDATE workflows
            SET quality_score = :quality_score,
                updated_at = NOW()
            WHERE id = :workflow_id
            """
        ),
        {
            "workflow_id": workflow_id,
            "quality_score": quality_score,
        },
    )
    await db.commit()
    await _invalidate_rank_cache(redis, workflow_id)
    await workflow_registry.invalidate_workflow_caches(redis, workflow_id=workflow_id)
    return quality_score


async def report_execution_outcome(
    *,
    workflow_id: UUID,
    agent_did: str,
    context_bundle_id: UUID | None,
    outcome: str,
    steps_completed: int,
    steps_total: int,
    failure_step_number: int | None,
    failure_reason: str | None,
    duration_ms: int | None,
    db: AsyncSession,
    redis=None,
    background_tasks: BackgroundTasks | None = None,
    verify_sync: bool | None = None,
) -> ExecutionReportResponse:
    """Record an execution outcome and schedule Layer 4 evidence verification."""
    try:
        await _load_published_workflow(db, workflow_id)
        await _ensure_agent_exists(db, agent_did)
        await _ensure_context_bundle_belongs(
            db=db,
            workflow_id=workflow_id,
            agent_did=agent_did,
            context_bundle_id=context_bundle_id,
        )
        execution_id = await _insert_execution(
            db=db,
            workflow_id=workflow_id,
            agent_did=agent_did,
            context_bundle_id=context_bundle_id,
            outcome=outcome,
            steps_completed=steps_completed,
            steps_total=steps_total,
            failure_step_number=failure_step_number,
            failure_reason=failure_reason,
            duration_ms=duration_ms,
        )
        await _increment_workflow_counters(
            db=db,
            workflow_id=workflow_id,
            outcome=outcome,
        )
        from api.services import liability_snapshot

        await liability_snapshot.create_snapshot(
            db=db,
            execution_id=execution_id,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to report execution: {exc.__class__.__name__}",
        ) from exc

    quality_score = await _recompute_and_store_quality(
        db=db,
        workflow_id=workflow_id,
        redis=redis,
    )

    if _sync_verification_enabled(verify_sync):
        verification = await verify_execution(execution_id, db=db, redis=redis)
        quality_score = verification.quality_score
    elif background_tasks is not None:
        background_tasks.add_task(verify_execution, execution_id, db, redis)

    return ExecutionReportResponse(
        execution_id=execution_id,
        verified=False,
        quality_score=quality_score,
    )


async def report_execution_from_request(
    *,
    workflow_id: UUID,
    request: ExecutionReportRequest,
    db: AsyncSession,
    redis=None,
    background_tasks: BackgroundTasks | None = None,
    verify_sync: bool | None = None,
) -> ExecutionReportResponse:
    """Record an execution outcome from an API request model."""
    return await report_execution_outcome(
        workflow_id=workflow_id,
        agent_did=request.agent_did,
        context_bundle_id=request.context_bundle_id,
        outcome=request.outcome,
        steps_completed=request.steps_completed,
        steps_total=request.steps_total,
        failure_step_number=request.failure_step_number,
        failure_reason=request.failure_reason,
        duration_ms=request.duration_ms,
        db=db,
        redis=redis,
        background_tasks=background_tasks,
        verify_sync=verify_sync,
    )


async def _load_execution(db: AsyncSession, execution_id: UUID) -> dict[str, Any]:
    """Load one execution record for verification."""
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


async def _load_required_step_tags(
    db: AsyncSession,
    workflow_id: UUID,
) -> list[str]:
    """Return required ontology tags for one workflow."""
    result = await db.execute(
        text(
            """
            SELECT ontology_tag
            FROM workflow_steps
            WHERE workflow_id = :workflow_id
              AND is_required = true
            ORDER BY step_number ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    return [row["ontology_tag"] for row in result.mappings().all()]


async def _load_disclosure_tags_for_execution(
    db: AsyncSession,
    execution: dict[str, Any],
) -> set[str]:
    """Return disclosure ontology tags around the execution report timestamp."""
    result = await db.execute(
        text(
            """
            SELECT DISTINCT ontology_tag
            FROM context_disclosures
            WHERE agent_did = :agent_did
              AND created_at BETWEEN
                    (CAST(:reported_at AS TIMESTAMPTZ) - INTERVAL '35 minutes')
                AND (CAST(:reported_at AS TIMESTAMPTZ) + INTERVAL '5 minutes')
            """
        ),
        {
            "agent_did": execution["agent_did"],
            "reported_at": execution["reported_at"],
        },
    )
    return {row["ontology_tag"] for row in result.mappings().all()}


async def verify_execution(
    execution_id: UUID,
    db: AsyncSession,
    redis=None,
) -> VerificationResult:
    """Verify an execution against Layer 4 context disclosure audit evidence."""
    try:
        execution = await _load_execution(db, execution_id)
        required_tags = await _load_required_step_tags(db, execution["workflow_id"])
        verified = False
        if execution["context_bundle_id"] is not None:
            disclosed_tags = await _load_disclosure_tags_for_execution(db, execution)
            verified = all(tag in disclosed_tags for tag in required_tags)

        await db.execute(
            text(
                """
                UPDATE workflow_executions
                SET verified = :verified,
                    verified_at = NOW()
                WHERE id = :execution_id
                """
            ),
            {
                "execution_id": execution_id,
                "verified": verified,
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
            detail=f"failed to verify execution: {exc.__class__.__name__}",
        ) from exc

    quality_score = await _recompute_and_store_quality(
        db=db,
        workflow_id=execution["workflow_id"],
        redis=redis,
    )
    return VerificationResult(
        execution_id=execution_id,
        verified=verified,
        quality_score=quality_score,
    )
