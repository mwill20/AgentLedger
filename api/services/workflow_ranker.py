"""Layer 5 workflow quality scoring and per-step service ranking."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.workflow import RankedStep, ServiceCandidate, WorkflowRankResponse
from api.services import context_mismatch, context_profiles
from api.services.context_matcher import ServiceContext, evaluate_profile

RANK_CACHE_TTL_SECONDS = 60


async def _cache_get_rank(redis, cache_key: str) -> WorkflowRankResponse | None:
    """Best-effort Redis read for workflow rank responses."""
    if redis is None:
        return None
    try:
        cached = await redis.get(cache_key)
    except Exception:
        return None
    if not cached:
        return None
    try:
        return WorkflowRankResponse.model_validate_json(cached)
    except Exception:
        return None


async def _cache_set_rank(
    redis,
    cache_key: str,
    response: WorkflowRankResponse,
) -> None:
    """Best-effort Redis write for workflow rank responses."""
    if redis is None:
        return
    try:
        await redis.set(
            cache_key,
            response.model_dump_json(),
            ex=RANK_CACHE_TTL_SECONDS,
        )
    except Exception:
        return


def rank_cache_key(
    workflow_id: UUID,
    geo: str | None = None,
    pricing_model: str | None = None,
    agent_did: str | None = None,
) -> str:
    """Build a rank cache key that preserves optional filter variants."""
    agent_segment = agent_did or "anonymous"
    return (
        f"workflow:rank:{workflow_id}:{geo or 'any'}:"
        f"{pricing_model or 'any'}:{agent_segment}"
    )


def _validation_score(status_name: str) -> float:
    """Map workflow publication state to the validation score input."""
    if status_name == "published":
        return 1.0
    if status_name == "draft":
        return 0.5
    if status_name == "rejected":
        return 0.0
    return 0.5


async def _avg_step_trust(db: AsyncSession, workflow_id: UUID) -> float:
    """Return normalized average trust for pinned workflow services."""
    result = await db.execute(
        text(
            """
            SELECT s.trust_score
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
        trust_score = row.get("trust_score")
        if trust_score is None:
            scores.append(0.5)
        else:
            scores.append(max(0.0, min(float(trust_score) / 100.0, 1.0)))
    return sum(scores) / len(scores)


async def compute_workflow_quality_score(
    workflow_id: UUID,
    db: AsyncSession,
    redis=None,
) -> float:
    """Compute the workflow quality score from validation and execution signals."""
    del redis
    workflow_result = await db.execute(
        text(
            """
            SELECT status, execution_count, success_count
            FROM workflows
            WHERE id = :workflow_id
            """
        ),
        {"workflow_id": workflow_id},
    )
    workflow = workflow_result.mappings().first()
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found",
        )

    verified_result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS verified_count
            FROM workflow_executions
            WHERE workflow_id = :workflow_id
              AND verified = true
            """
        ),
        {"workflow_id": workflow_id},
    )
    verified_row = verified_result.mappings().first()
    verified_count = int(verified_row["verified_count"] if verified_row else 0)

    execution_count = int(workflow["execution_count"] or 0)
    success_count = int(workflow["success_count"] or 0)
    volume_factor = min(1.0, execution_count / 100)
    success_rate = success_count / execution_count if execution_count else 0.0
    verification_rate = verified_count / execution_count if execution_count else 0.0
    avg_step_trust = await _avg_step_trust(db, workflow_id)
    raw = (
        _validation_score(workflow["status"]) * 0.35
        + success_rate * 0.30 * volume_factor
        + verification_rate * 0.20
        + avg_step_trust * 0.15
    )
    if verification_rate < 0.5:
        raw = min(raw, 0.70)
    return round(raw * 100, 2)


async def _load_published_workflow_steps(
    db: AsyncSession,
    workflow_id: UUID,
) -> list[dict[str, Any]]:
    """Load ordered steps for a published workflow."""
    workflow_result = await db.execute(
        text(
            """
            SELECT id
            FROM workflows
            WHERE id = :workflow_id
              AND status = 'published'
            """
        ),
        {"workflow_id": workflow_id},
    )
    if workflow_result.mappings().first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found or not published",
        )

    step_result = await db.execute(
        text(
            """
            SELECT
                step_number,
                ontology_tag,
                is_required,
                context_fields_required,
                context_fields_optional,
                min_trust_tier,
                min_trust_score
            FROM workflow_steps
            WHERE workflow_id = :workflow_id
            ORDER BY step_number ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    return list(step_result.mappings().all())


async def _load_agent_profile_for_rank(
    db: AsyncSession,
    agent_did: str | None,
    redis=None,
) -> Any | None:
    """Load a profile for can_disclose checks, defaulting to deny if missing."""
    if not agent_did:
        return None
    try:
        return await context_profiles.get_active_profile(db, agent_did, redis=redis)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        return SimpleNamespace(default_policy="deny", rules=[])


def _candidate_service_context(
    *,
    row: Any,
    step: dict[str, Any],
) -> ServiceContext:
    """Build the Layer 4 service context used for rank-time fit checks."""
    required_fields = list(step.get("context_fields_required") or [])
    optional_fields = list(step.get("context_fields_optional") or [])
    field_sensitivity_tiers = {
        field: context_mismatch.get_sensitivity_tier(field)
        for field in required_fields + optional_fields
    }
    domain = row["domain"]
    return ServiceContext(
        service_id=row["service_id"],
        domain=domain,
        did=f"did:web:{domain}",
        ontology_tag=step["ontology_tag"],
        ontology_domain=row["ontology_domain"],
        trust_tier=int(row["trust_tier"] or 1),
        trust_score=float(row["trust_score"] or 0.0),
        declared_required_fields=required_fields,
        declared_optional_fields=optional_fields,
        field_sensitivity_tiers=field_sensitivity_tiers,
    )


def _candidate_can_disclose(
    *,
    profile: Any | None,
    step: dict[str, Any],
    service: ServiceContext,
) -> bool:
    """Return whether all required step fields can be disclosed to a candidate."""
    if profile is None:
        return True
    for field in list(step.get("context_fields_required") or []):
        decision = evaluate_profile(
            profile.rules,
            field,
            service,
            profile.default_policy,
        )
        if decision not in {"permit", "commit"}:
            return False
    return True


async def _rank_candidates_for_step(
    db: AsyncSession,
    step: dict[str, Any],
    geo: str | None,
    pricing_model: str | None,
    profile: Any | None,
) -> list[ServiceCandidate]:
    """Return ranked services for one workflow step."""
    filters = [
        "sc.ontology_tag = :ontology_tag",
        "s.trust_tier >= :min_trust_tier",
        "s.trust_score >= :min_trust_score",
        "s.is_active = true",
        "s.is_banned = false",
    ]
    params: dict[str, Any] = {
        "ontology_tag": step["ontology_tag"],
        "min_trust_tier": step["min_trust_tier"],
        "min_trust_score": step["min_trust_score"],
    }
    if geo:
        filters.append(
            """
            (
                so.geo_restrictions IS NULL
                OR cardinality(so.geo_restrictions) = 0
                OR :geo = ANY(so.geo_restrictions)
            )
            """
        )
        params["geo"] = geo
    if pricing_model:
        filters.append("sp.pricing_model = :pricing_model")
        params["pricing_model"] = pricing_model

    result = await db.execute(
        text(
            f"""
            SELECT
                s.id AS service_id,
                s.name,
                s.domain,
                s.trust_score,
                s.trust_tier,
                ot.domain AS ontology_domain,
                sp.pricing_model
            FROM service_capabilities sc
            JOIN services s ON s.id = sc.service_id
            JOIN ontology_tags ot ON ot.tag = sc.ontology_tag
            LEFT JOIN service_operations so ON so.service_id = s.id
            LEFT JOIN service_pricing sp ON sp.service_id = s.id
            WHERE {' AND '.join(filters)}
            ORDER BY s.trust_score DESC, s.id ASC
            LIMIT 10
            """
        ),
        params,
    )

    candidates: list[ServiceCandidate] = []
    for row in result.mappings().all():
        trust_score = float(row["trust_score"] or 0.0)
        service = _candidate_service_context(row=row, step=step)
        candidates.append(
            ServiceCandidate(
                service_id=row["service_id"],
                name=row["name"],
                trust_score=trust_score,
                trust_tier=int(row["trust_tier"] or 1),
                rank_score=round(trust_score / 100.0, 4),
                can_disclose=_candidate_can_disclose(
                    profile=profile,
                    step=step,
                    service=service,
                ),
            )
        )
    return candidates


async def rank_workflow_steps(
    workflow_id: UUID,
    geo: str | None,
    pricing_model: str | None,
    db: AsyncSession,
    agent_did: str | None = None,
    redis=None,
) -> list[RankedStep]:
    """Rank service candidates for each published workflow step."""
    steps = await _load_published_workflow_steps(db, workflow_id)
    profile = await _load_agent_profile_for_rank(db, agent_did, redis=redis)
    ranked_steps: list[RankedStep] = []
    for step in steps:
        candidates = await _rank_candidates_for_step(
            db=db,
            step=dict(step),
            geo=geo,
            pricing_model=pricing_model,
            profile=profile,
        )
        ranked_steps.append(
            RankedStep(
                step_number=step["step_number"],
                ontology_tag=step["ontology_tag"],
                is_required=step["is_required"],
                min_trust_tier=step["min_trust_tier"],
                min_trust_score=float(step["min_trust_score"]),
                candidates=candidates,
            )
        )
    return ranked_steps


async def get_workflow_rank(
    workflow_id: UUID,
    *,
    geo: str | None,
    pricing_model: str | None,
    agent_did: str | None = None,
    db: AsyncSession,
    redis=None,
) -> WorkflowRankResponse:
    """Return cached or computed workflow rank response."""
    cache_key = rank_cache_key(workflow_id, geo, pricing_model, agent_did)
    cached = await _cache_get_rank(redis, cache_key)
    if cached is not None:
        return cached

    response = WorkflowRankResponse(
        workflow_id=workflow_id,
        ranked_steps=await rank_workflow_steps(
            workflow_id=workflow_id,
            geo=geo,
            pricing_model=pricing_model,
            db=db,
            agent_did=agent_did,
            redis=redis,
        ),
    )
    await _cache_set_rank(redis, cache_key, response)
    return response
