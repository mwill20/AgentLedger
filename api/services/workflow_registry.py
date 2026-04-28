"""Layer 5 workflow registry CRUD and spec validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.workflow import (
    ExecutionReportRequest,
    ExecutionReportResponse,
    WorkflowCreateRequest,
    WorkflowCreateResponse,
    WorkflowListResponse,
    WorkflowRecord,
    WorkflowStepInput,
    WorkflowStepRecord,
    WorkflowSummary,
)

VALIDATION_QUEUE_DID = "did:agentledger:validation-queue"
ESTIMATED_REVIEW_HOURS = 48


async def _ensure_author_exists(db: AsyncSession, author_did: str) -> None:
    """Require workflow authors to be active registered agents."""
    result = await db.execute(
        text(
            """
            SELECT did
            FROM agent_identities
            WHERE did = :author_did
              AND is_active = true
              AND is_revoked = false
            """
        ),
        {"author_did": author_did},
    )
    if result.mappings().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow author identity not found",
        )


async def _load_ontology_rows(
    db: AsyncSession,
    ontology_tags: list[str],
) -> dict[str, Mapping[str, Any]]:
    """Load ontology tags needed by a workflow and reject unknown tags."""
    unique_tags = sorted(set(ontology_tags))
    result = await db.execute(
        text(
            """
            SELECT tag, domain, sensitivity_tier
            FROM ontology_tags
            WHERE tag = ANY(CAST(:tags AS TEXT[]))
            """
        ),
        {"tags": unique_tags},
    )
    rows = {row["tag"]: row for row in result.mappings().all()}
    missing = [tag for tag in unique_tags if tag not in rows]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown ontology tags: {', '.join(missing)}",
        )
    return rows


async def _validate_pinned_service_step(
    db: AsyncSession,
    step: WorkflowStepInput,
) -> None:
    """Validate a step pinned to a specific service against Layer 1 manifest data."""
    if step.service_id is None:
        return

    capability_result = await db.execute(
        text(
            """
            SELECT sc.service_id
            FROM service_capabilities sc
            JOIN services s ON s.id = sc.service_id
            WHERE sc.service_id = :service_id
              AND sc.ontology_tag = :ontology_tag
              AND s.is_active = true
              AND s.is_banned = false
            LIMIT 1
            """
        ),
        {"service_id": step.service_id, "ontology_tag": step.ontology_tag},
    )
    if capability_result.mappings().first() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"service {step.service_id} is not active for ontology tag "
                f"{step.ontology_tag}"
            ),
        )

    required_fields = step.context_fields_required
    if not required_fields:
        return

    context_result = await db.execute(
        text(
            """
            SELECT field_name
            FROM service_context_requirements
            WHERE service_id = :service_id
              AND field_name = ANY(CAST(:fields AS TEXT[]))
            """
        ),
        {"service_id": step.service_id, "fields": required_fields},
    )
    declared = {row["field_name"] for row in context_result.mappings().all()}
    missing = [field for field in required_fields if field not in declared]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"pinned service {step.service_id} does not declare required "
                f"context fields: {', '.join(missing)}"
            ),
        )


async def _validate_workflow_spec(
    db: AsyncSession,
    request: WorkflowCreateRequest,
) -> dict[str, Mapping[str, Any]]:
    """Validate workflow submission rules that depend on stored registry data."""
    step_tags = [step.ontology_tag for step in request.steps]
    missing_from_tags = [tag for tag in sorted(set(step_tags)) if tag not in request.tags]
    if missing_from_tags:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"workflow tags must include step tags: {', '.join(missing_from_tags)}",
        )

    ontology_rows = await _load_ontology_rows(db, request.tags + step_tags)
    for step in request.steps:
        await _validate_pinned_service_step(db, step)
    return ontology_rows


def _validation_domain(
    request: WorkflowCreateRequest,
    ontology_rows: dict[str, Mapping[str, Any]],
) -> str:
    """Choose the validator domain for the initial validation queue record."""
    for step in request.steps:
        row = ontology_rows[step.ontology_tag]
        if int(row["sensitivity_tier"]) >= 3:
            return str(row["domain"])
    return request.ontology_domain


def _spec_payload(request: WorkflowCreateRequest, workflow_id: UUID) -> dict[str, Any]:
    """Return the stored machine-readable workflow spec."""
    payload = request.model_dump(mode="json")
    payload["workflow_id"] = str(workflow_id)
    payload["quality"] = {
        "quality_score": 0.0,
        "execution_count": 0,
        "success_rate": 0.0,
        "validation_status": "draft",
        "validated_by_domain": None,
    }
    payload["accountability"]["published_at"] = None
    payload["accountability"]["spec_hash"] = None
    return payload


def _step_insert_rows(
    workflow_id: UUID,
    steps: list[WorkflowStepInput],
) -> list[dict[str, Any]]:
    """Build bulk insert rows for workflow_steps."""
    return [
        {
            "workflow_id": workflow_id,
            "step_number": step.step_number,
            "name": step.name,
            "ontology_tag": step.ontology_tag,
            "service_id": step.service_id,
            "is_required": step.is_required,
            "fallback_step_number": step.fallback_step_number,
            "context_fields_required": step.context_fields_required,
            "context_fields_optional": step.context_fields_optional,
            "min_trust_tier": step.min_trust_tier,
            "min_trust_score": step.min_trust_score,
            "timeout_seconds": step.timeout_seconds,
        }
        for step in steps
    ]


async def _insert_steps(
    db: AsyncSession,
    workflow_id: UUID,
    steps: list[WorkflowStepInput],
) -> None:
    """Insert all step rows for a workflow."""
    rows = _step_insert_rows(workflow_id, steps)
    await db.execute(
        text(
            """
            INSERT INTO workflow_steps (
                workflow_id,
                step_number,
                name,
                ontology_tag,
                service_id,
                is_required,
                fallback_step_number,
                context_fields_required,
                context_fields_optional,
                min_trust_tier,
                min_trust_score,
                timeout_seconds,
                created_at
            )
            VALUES (
                :workflow_id,
                :step_number,
                :name,
                :ontology_tag,
                :service_id,
                :is_required,
                :fallback_step_number,
                :context_fields_required,
                :context_fields_optional,
                :min_trust_tier,
                :min_trust_score,
                :timeout_seconds,
                NOW()
            )
            """
        ),
        rows,
    )


def _json_dict(value: Any) -> dict[str, Any]:
    """Normalize JSONB values from real DB rows and test doubles."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _to_step_record(row: Mapping[str, Any]) -> WorkflowStepRecord:
    """Map one workflow_steps row to an API model."""
    return WorkflowStepRecord(
        step_id=row["id"],
        step_number=row["step_number"],
        name=row["name"],
        ontology_tag=row["ontology_tag"],
        service_id=row["service_id"],
        is_required=row["is_required"],
        fallback_step_number=row["fallback_step_number"],
        context_fields_required=list(row["context_fields_required"] or []),
        context_fields_optional=list(row["context_fields_optional"] or []),
        min_trust_tier=row["min_trust_tier"],
        min_trust_score=float(row["min_trust_score"]),
        timeout_seconds=row["timeout_seconds"],
        created_at=row["created_at"],
    )


def _to_workflow_record(
    workflow_row: Mapping[str, Any],
    step_rows: list[Mapping[str, Any]],
) -> WorkflowRecord:
    """Map one workflow row and its ordered steps to an API model."""
    sorted_steps = sorted(
        step_rows,
        key=lambda row: (row["step_number"], str(row["id"])),
    )
    return WorkflowRecord(
        workflow_id=workflow_row["id"],
        name=workflow_row["name"],
        slug=workflow_row["slug"],
        description=workflow_row["description"],
        ontology_domain=workflow_row["ontology_domain"],
        tags=list(workflow_row["tags"] or []),
        spec=_json_dict(workflow_row["spec"]),
        spec_version=workflow_row["spec_version"],
        spec_hash=workflow_row.get("spec_hash"),
        author_did=workflow_row["author_did"],
        status=workflow_row["status"],
        quality_score=float(workflow_row["quality_score"]),
        execution_count=int(workflow_row["execution_count"]),
        success_count=int(workflow_row["success_count"]),
        failure_count=int(workflow_row["failure_count"]),
        parent_workflow_id=workflow_row["parent_workflow_id"],
        published_at=workflow_row["published_at"],
        deprecated_at=workflow_row["deprecated_at"],
        steps=[_to_step_record(row) for row in sorted_steps],
        created_at=workflow_row["created_at"],
        updated_at=workflow_row["updated_at"],
    )


def _to_summary(row: Mapping[str, Any]) -> WorkflowSummary:
    """Map a list query row to a workflow summary."""
    return WorkflowSummary(
        workflow_id=row["id"],
        name=row["name"],
        slug=row["slug"],
        description=row["description"],
        ontology_domain=row["ontology_domain"],
        tags=list(row["tags"] or []),
        status=row["status"],
        quality_score=float(row["quality_score"]),
        execution_count=int(row["execution_count"]),
        step_count=int(row["step_count"]),
        published_at=row["published_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _get_steps_for_workflow(
    db: AsyncSession,
    workflow_id: UUID,
) -> list[Mapping[str, Any]]:
    """Return stored workflow steps in execution order."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                step_number,
                name,
                ontology_tag,
                service_id,
                is_required,
                fallback_step_number,
                context_fields_required,
                context_fields_optional,
                min_trust_tier,
                min_trust_score,
                timeout_seconds,
                created_at
            FROM workflow_steps
            WHERE workflow_id = :workflow_id
            ORDER BY step_number ASC, id ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    return list(result.mappings().all())


async def create_workflow(
    db: AsyncSession,
    request: WorkflowCreateRequest,
) -> WorkflowCreateResponse:
    """Create a draft workflow, its steps, and initial validation queue row."""
    workflow_id = request.workflow_id or uuid4()
    spec_payload = _spec_payload(request, workflow_id)
    try:
        await _ensure_author_exists(db, request.accountability.author_did)
        ontology_rows = await _validate_workflow_spec(db, request)

        await db.execute(
            text(
                """
                INSERT INTO workflows (
                    id,
                    name,
                    slug,
                    description,
                    ontology_domain,
                    tags,
                    spec,
                    spec_version,
                    spec_hash,
                    author_did,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (
                    :id,
                    :name,
                    :slug,
                    :description,
                    :ontology_domain,
                    :tags,
                    CAST(:spec AS JSONB),
                    :spec_version,
                    :spec_hash,
                    :author_did,
                    'draft',
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "id": workflow_id,
                "name": request.name,
                "slug": request.slug,
                "description": request.description,
                "ontology_domain": request.ontology_domain,
                "tags": request.tags,
                "spec": json.dumps(spec_payload, sort_keys=True),
                "spec_version": request.spec_version,
                "spec_hash": None,
                "author_did": request.accountability.author_did,
            },
        )
        await _insert_steps(db, workflow_id, request.steps)

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
                RETURNING id
                """
            ),
            {
                "workflow_id": workflow_id,
                "validator_did": VALIDATION_QUEUE_DID,
                "validator_domain": _validation_domain(request, ontology_rows),
            },
        )
        validation_row = validation_result.mappings().first()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow slug or id already exists",
        ) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to create workflow: {exc.__class__.__name__}",
        ) from exc

    return WorkflowCreateResponse(
        workflow_id=workflow_id,
        slug=request.slug,
        status="draft",
        validation_id=validation_row["id"],
        estimated_review_hours=ESTIMATED_REVIEW_HOURS,
    )


async def update_workflow_spec(
    db: AsyncSession,
    workflow_id: UUID,
    request: WorkflowCreateRequest,
) -> WorkflowRecord:
    """Replace a draft workflow spec while rejecting published spec changes."""
    workflow_row = await _get_workflow_row_by_id(db, workflow_id)
    if workflow_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found",
        )
    if workflow_row["status"] == "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "published workflow spec is immutable; submit a new workflow "
                "to create an updated version"
            ),
        )
    if workflow_row["status"] not in {"draft", "rejected"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow spec can only be updated while draft or rejected",
        )

    spec_payload = _spec_payload(request, workflow_id)
    try:
        await _ensure_author_exists(db, request.accountability.author_did)
        await _validate_workflow_spec(db, request)
        await db.execute(
            text(
                """
                UPDATE workflows
                SET name = :name,
                    slug = :slug,
                    description = :description,
                    ontology_domain = :ontology_domain,
                    tags = :tags,
                    spec = CAST(:spec AS JSONB),
                    spec_version = :spec_version,
                    spec_hash = NULL,
                    author_did = :author_did,
                    status = 'draft',
                    updated_at = NOW()
                WHERE id = :workflow_id
                """
            ),
            {
                "workflow_id": workflow_id,
                "name": request.name,
                "slug": request.slug,
                "description": request.description,
                "ontology_domain": request.ontology_domain,
                "tags": request.tags,
                "spec": json.dumps(spec_payload, sort_keys=True),
                "spec_version": request.spec_version,
                "author_did": request.accountability.author_did,
            },
        )
        await db.execute(
            text("DELETE FROM workflow_steps WHERE workflow_id = :workflow_id"),
            {"workflow_id": workflow_id},
        )
        await _insert_steps(db, workflow_id, request.steps)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow slug or id already exists",
        ) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to update workflow: {exc.__class__.__name__}",
        ) from exc

    return await get_workflow(db=db, workflow_id=workflow_id)


async def _get_workflow_row_by_id(
    db: AsyncSession,
    workflow_id: UUID,
) -> Mapping[str, Any] | None:
    """Load one workflow row by id."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                name,
                slug,
                description,
                ontology_domain,
                tags,
                spec,
                spec_version,
                spec_hash,
                author_did,
                status,
                quality_score,
                execution_count,
                success_count,
                failure_count,
                parent_workflow_id,
                published_at,
                deprecated_at,
                created_at,
                updated_at
            FROM workflows
            WHERE id = :workflow_id
            """
        ),
        {"workflow_id": workflow_id},
    )
    return result.mappings().first()


async def _get_workflow_row_by_slug(
    db: AsyncSession,
    slug: str,
) -> Mapping[str, Any] | None:
    """Load one workflow row by slug."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                name,
                slug,
                description,
                ontology_domain,
                tags,
                spec,
                spec_version,
                spec_hash,
                author_did,
                status,
                quality_score,
                execution_count,
                success_count,
                failure_count,
                parent_workflow_id,
                published_at,
                deprecated_at,
                created_at,
                updated_at
            FROM workflows
            WHERE slug = :slug
            """
        ),
        {"slug": slug},
    )
    return result.mappings().first()


async def get_workflow(
    db: AsyncSession,
    workflow_id: UUID,
) -> WorkflowRecord:
    """Return full workflow detail by id."""
    workflow_row = await _get_workflow_row_by_id(db, workflow_id)
    if workflow_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found",
        )
    step_rows = await _get_steps_for_workflow(db, workflow_row["id"])
    return _to_workflow_record(workflow_row, step_rows)


async def get_workflow_by_slug(
    db: AsyncSession,
    slug: str,
) -> WorkflowRecord:
    """Return full workflow detail by slug."""
    workflow_row = await _get_workflow_row_by_slug(db, slug)
    if workflow_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found",
        )
    step_rows = await _get_steps_for_workflow(db, workflow_row["id"])
    return _to_workflow_record(workflow_row, step_rows)


async def list_workflows(
    db: AsyncSession,
    *,
    domain: str | None = None,
    tags: list[str] | None = None,
    status_filter: str = "published",
    quality_min: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> WorkflowListResponse:
    """List workflows with optional filters."""
    count_where: list[str] = ["status = :status"]
    list_where: list[str] = ["w.status = :status"]
    params: dict[str, Any] = {
        "status": status_filter,
        "limit": limit,
        "offset": offset,
    }
    if domain:
        count_where.append("ontology_domain = :domain")
        list_where.append("w.ontology_domain = :domain")
        params["domain"] = domain.upper()
    if tags:
        count_where.append("tags @> CAST(:tags AS TEXT[])")
        list_where.append("w.tags @> CAST(:tags AS TEXT[])")
        params["tags"] = tags
    if quality_min is not None:
        count_where.append("quality_score >= :quality_min")
        list_where.append("w.quality_score >= :quality_min")
        params["quality_min"] = quality_min

    count_where_sql = " AND ".join(count_where)
    list_where_sql = " AND ".join(list_where)
    total_result = await db.execute(
        text(f"SELECT COUNT(*) AS total FROM workflows WHERE {count_where_sql}"),
        params,
    )
    total_row = total_result.mappings().first()
    total = int(total_row["total"] if total_row is not None else 0)

    result = await db.execute(
        text(
            f"""
            SELECT
                w.id,
                w.name,
                w.slug,
                w.description,
                w.ontology_domain,
                w.tags,
                w.status,
                w.quality_score,
                w.execution_count,
                w.published_at,
                w.created_at,
                w.updated_at,
                COUNT(ws.id)::int AS step_count
            FROM workflows w
            LEFT JOIN workflow_steps ws ON ws.workflow_id = w.id
            WHERE {list_where_sql}
            GROUP BY w.id
            ORDER BY w.quality_score DESC, w.updated_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    )

    return WorkflowListResponse(
        total=total,
        limit=limit,
        offset=offset,
        workflows=[_to_summary(row) for row in result.mappings().all()],
    )


async def _verify_context_bundle(
    db: AsyncSession,
    *,
    workflow_id: UUID,
    agent_did: str,
    context_bundle_id: UUID | None,
) -> bool:
    """Return True if a context bundle confirms this agent ran this workflow."""
    if context_bundle_id is None:
        return False
    result = await db.execute(
        text(
            """
            SELECT id
            FROM workflow_context_bundles
            WHERE id = :bundle_id
              AND workflow_id = :workflow_id
              AND agent_did = :agent_did
              AND status IN ('approved', 'consumed')
              AND expires_at > NOW()
            """
        ),
        {
            "bundle_id": context_bundle_id,
            "workflow_id": workflow_id,
            "agent_did": agent_did,
        },
    )
    return result.mappings().first() is not None


async def report_execution(
    db: AsyncSession,
    workflow_id: UUID,
    request: ExecutionReportRequest,
    redis=None,
) -> ExecutionReportResponse:
    """Record an execution outcome, update counters, and recompute quality score."""
    from api.services.workflow_ranker import compute_workflow_quality_score

    try:
        workflow_row = await _get_workflow_row_by_id(db, workflow_id)
        if workflow_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="workflow not found",
            )
        if workflow_row["status"] != "published":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="execution outcomes can only be reported for published workflows",
            )

        verified = await _verify_context_bundle(
            db=db,
            workflow_id=workflow_id,
            agent_did=request.agent_did,
            context_bundle_id=request.context_bundle_id,
        )

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
                    :verified,
                    CASE WHEN :verified THEN NOW() ELSE NULL END,
                    NOW(),
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "workflow_id": workflow_id,
                "agent_did": request.agent_did,
                "context_bundle_id": request.context_bundle_id,
                "outcome": request.outcome,
                "steps_completed": request.steps_completed,
                "steps_total": request.steps_total,
                "failure_step_number": request.failure_step_number,
                "failure_reason": request.failure_reason,
                "duration_ms": request.duration_ms,
                "verified": verified,
            },
        )
        execution_id = result.scalar_one()

        if request.outcome == "success":
            counter_sql = """
                UPDATE workflows
                SET execution_count = execution_count + 1,
                    success_count = success_count + 1,
                    updated_at = NOW()
                WHERE id = :workflow_id
            """
        else:
            counter_sql = """
                UPDATE workflows
                SET execution_count = execution_count + 1,
                    failure_count = failure_count + 1,
                    updated_at = NOW()
                WHERE id = :workflow_id
            """
        await db.execute(text(counter_sql), {"workflow_id": workflow_id})
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

    quality_score = await compute_workflow_quality_score(
        workflow_id=workflow_id,
        db=db,
        redis=redis,
    )

    return ExecutionReportResponse(
        execution_id=execution_id,
        verified=verified,
        quality_score=quality_score,
    )
