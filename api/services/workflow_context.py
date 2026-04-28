"""Layer 5 workflow context bundle creation and approval."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.workflow import (
    BundleApproveRequest,
    BundleApproveResponse,
    BundleCreateRequest,
    BundleFieldBreakdown,
    BundleResponse,
)
from api.services import context_mismatch, context_profiles
from api.services.context_matcher import ServiceContext, evaluate_profile

BUNDLE_TTL_MINUTES = 30


def _utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    """Normalize DB timestamps for comparisons in tests and real sessions."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _dedupe(values: list[str]) -> list[str]:
    """Dedupe field names while preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _json_dict(value: Any) -> dict[str, Any]:
    """Normalize JSONB values returned by DB drivers or test doubles."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


async def _load_workflow_steps(
    db: AsyncSession,
    workflow_id: UUID,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load one published workflow and its ordered steps."""
    result = await db.execute(
        text(
            """
            SELECT
                w.id AS workflow_id,
                w.ontology_domain AS workflow_ontology_domain,
                ws.id AS step_id,
                ws.step_number,
                ws.ontology_tag,
                ws.service_id,
                ws.is_required,
                ws.context_fields_required,
                ws.context_fields_optional,
                ws.min_trust_tier,
                ws.min_trust_score
            FROM workflows w
            JOIN workflow_steps ws ON ws.workflow_id = w.id
            WHERE w.id = :workflow_id
              AND w.status = 'published'
            ORDER BY ws.step_number ASC, ws.id ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    rows = [dict(row) for row in result.mappings().all()]
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow not found or not published",
        )

    workflow = {
        "workflow_id": rows[0]["workflow_id"],
        "ontology_domain": rows[0]["workflow_ontology_domain"],
    }
    return workflow, rows


async def _load_profile_or_default(
    db: AsyncSession,
    agent_did: str,
    redis=None,
) -> Any:
    """Load an active Layer 4 profile, defaulting to deny when none exists."""
    try:
        return await context_profiles.get_active_profile(db, agent_did, redis=redis)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        return SimpleNamespace(
            profile_id=None,
            default_policy="deny",
            rules=[],
        )


def _override_rule(scoped_profile_overrides: dict[str, str]) -> Any | None:
    """Convert field-level scoped overrides into a highest-priority rule."""
    if not scoped_profile_overrides:
        return None
    permitted = sorted(
        field
        for field, action in scoped_profile_overrides.items()
        if action == "permit"
    )
    denied = sorted(
        field
        for field, action in scoped_profile_overrides.items()
        if action in {"deny", "withhold"}
    )
    return SimpleNamespace(
        priority=0,
        scope_type="sensitivity",
        scope_value="1",
        permitted_fields=permitted,
        denied_fields=denied,
        action="permit",
    )


def _apply_scoped_overrides(profile: Any, scoped_profile_overrides: dict[str, str]) -> Any:
    """Return a profile-like object with scoped rules taking priority."""
    rule = _override_rule(scoped_profile_overrides)
    if rule is None:
        return profile
    return SimpleNamespace(
        profile_id=getattr(profile, "profile_id", None),
        default_policy=profile.default_policy,
        rules=[rule, *list(profile.rules)],
    )


async def _create_scoped_profile(
    db: AsyncSession,
    *,
    workflow_id: UUID,
    agent_did: str,
    base_profile_id: UUID | None,
    scoped_profile_overrides: dict[str, str],
) -> UUID:
    """Create or refresh a workflow-scoped profile override record."""
    result = await db.execute(
        text(
            """
            INSERT INTO workflow_scoped_profiles (
                workflow_id,
                agent_did,
                base_profile_id,
                overrides,
                is_active,
                created_at
            )
            VALUES (
                :workflow_id,
                :agent_did,
                :base_profile_id,
                CAST(:overrides AS JSONB),
                true,
                NOW()
            )
            ON CONFLICT (workflow_id, agent_did)
            DO UPDATE SET
                base_profile_id = EXCLUDED.base_profile_id,
                overrides = EXCLUDED.overrides,
                is_active = true,
                created_at = NOW()
            RETURNING id
            """
        ),
        {
            "workflow_id": workflow_id,
            "agent_did": agent_did,
            "base_profile_id": base_profile_id,
            "overrides": json.dumps(scoped_profile_overrides, sort_keys=True),
        },
    )
    row = result.mappings().first()
    return row["id"]


async def _service_context_for_step(
    db: AsyncSession,
    workflow: dict[str, Any],
    step: dict[str, Any],
    requested_fields: list[str],
) -> ServiceContext:
    """Build the Layer 4 service context used by profile evaluation."""
    field_sensitivity_tiers = {
        field: context_mismatch.get_sensitivity_tier(field)
        for field in requested_fields
    }
    if step["service_id"] is None:
        return ServiceContext(
            service_id=UUID(int=0),
            domain="workflow.unpinned",
            did="did:web:workflow.unpinned",
            ontology_tag=step["ontology_tag"],
            ontology_domain=workflow["ontology_domain"],
            trust_tier=int(step["min_trust_tier"] or 1),
            trust_score=float(step["min_trust_score"] or 0.0),
            declared_required_fields=list(step["context_fields_required"] or []),
            declared_optional_fields=list(step["context_fields_optional"] or []),
            field_sensitivity_tiers=field_sensitivity_tiers,
        )

    result = await db.execute(
        text(
            """
            SELECT
                s.id,
                s.domain,
                s.trust_tier,
                s.trust_score,
                ot.domain AS ontology_domain
            FROM services s
            JOIN service_capabilities sc ON sc.service_id = s.id
            JOIN ontology_tags ot ON ot.tag = sc.ontology_tag
            WHERE s.id = :service_id
              AND sc.ontology_tag = :ontology_tag
              AND s.is_active = true
              AND s.is_banned = false
            LIMIT 1
            """
        ),
        {
            "service_id": step["service_id"],
            "ontology_tag": step["ontology_tag"],
        },
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="service capability not found",
        )

    return ServiceContext(
        service_id=row["id"],
        domain=row["domain"],
        did=f"did:web:{row['domain']}",
        ontology_tag=step["ontology_tag"],
        ontology_domain=row["ontology_domain"],
        trust_tier=int(row["trust_tier"] or 1),
        trust_score=float(row["trust_score"] or 0.0),
        declared_required_fields=list(step["context_fields_required"] or []),
        declared_optional_fields=list(step["context_fields_optional"] or []),
        field_sensitivity_tiers=field_sensitivity_tiers,
    )


def _classify_step_fields(
    *,
    profile: Any,
    service: ServiceContext,
    requested_fields: list[str],
) -> BundleFieldBreakdown:
    """Classify one step's fields with the Layer 4 profile evaluator."""
    permitted: list[str] = []
    withheld: list[str] = []
    committed: list[str] = []
    for field in requested_fields:
        decision = evaluate_profile(
            profile.rules,
            field,
            service,
            profile.default_policy,
        )
        if decision == "permit":
            permitted.append(field)
        elif decision == "commit":
            committed.append(field)
        else:
            withheld.append(field)
    return BundleFieldBreakdown(
        permitted=permitted,
        withheld=withheld,
        committed=committed,
    )


def _bundle_payload(
    *,
    by_step: dict[str, BundleFieldBreakdown],
    all_permitted: list[str],
    all_committed: list[str],
    all_withheld: list[str],
) -> dict[str, Any]:
    """Build the JSONB payload persisted on the bundle row."""
    return {
        "by_step": {
            step_key: breakdown.model_dump(mode="json")
            for step_key, breakdown in by_step.items()
        },
        "all_permitted": all_permitted,
        "all_committed": all_committed,
        "all_withheld": all_withheld,
    }


async def create_context_bundle(
    *,
    workflow_id: UUID,
    agent_did: str,
    scoped_profile_overrides: dict[str, str],
    db: AsyncSession,
    redis=None,
) -> BundleResponse:
    """Create a pending workflow-level context bundle."""
    try:
        workflow, steps = await _load_workflow_steps(db, workflow_id)
        base_profile = await _load_profile_or_default(db, agent_did, redis=redis)
        profile = _apply_scoped_overrides(base_profile, scoped_profile_overrides)
        scoped_profile_id = None
        if scoped_profile_overrides:
            scoped_profile_id = await _create_scoped_profile(
                db=db,
                workflow_id=workflow_id,
                agent_did=agent_did,
                base_profile_id=getattr(base_profile, "profile_id", None),
                scoped_profile_overrides=scoped_profile_overrides,
            )

        by_step: dict[str, BundleFieldBreakdown] = {}
        permitted_union: list[str] = []
        committed_union: list[str] = []
        withheld_union: list[str] = []
        for step in steps:
            requested_fields = _dedupe(
                list(step["context_fields_required"] or [])
                + list(step["context_fields_optional"] or [])
            )
            service = await _service_context_for_step(
                db=db,
                workflow=workflow,
                step=step,
                requested_fields=requested_fields,
            )
            breakdown = _classify_step_fields(
                profile=profile,
                service=service,
                requested_fields=requested_fields,
            )
            step_key = f"step_{step['step_number']}"
            by_step[step_key] = breakdown
            permitted_union.extend(breakdown.permitted)
            committed_union.extend(breakdown.committed)
            withheld_union.extend(breakdown.withheld)

        all_permitted = _dedupe(permitted_union)
        all_committed = _dedupe(committed_union)
        all_withheld = _dedupe(withheld_union)
        payload = _bundle_payload(
            by_step=by_step,
            all_permitted=all_permitted,
            all_committed=all_committed,
            all_withheld=all_withheld,
        )

        result = await db.execute(
            text(
                """
                INSERT INTO workflow_context_bundles (
                    workflow_id,
                    agent_did,
                    scoped_profile_id,
                    status,
                    approved_fields,
                    expires_at,
                    created_at
                )
                VALUES (
                    :workflow_id,
                    :agent_did,
                    :scoped_profile_id,
                    'pending',
                    CAST(:approved_fields AS JSONB),
                    NOW() + INTERVAL '30 minutes',
                    NOW()
                )
                RETURNING id, expires_at
                """
            ),
            {
                "workflow_id": workflow_id,
                "agent_did": agent_did,
                "scoped_profile_id": scoped_profile_id,
                "approved_fields": json.dumps(payload, sort_keys=True),
            },
        )
        bundle_row = result.mappings().first()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to create context bundle: {exc.__class__.__name__}",
        ) from exc

    return BundleResponse(
        bundle_id=bundle_row["id"],
        workflow_id=workflow_id,
        status="pending",
        by_step=by_step,
        all_permitted=all_permitted,
        all_committed=all_committed,
        all_withheld=all_withheld,
        expires_at=bundle_row["expires_at"],
    )


async def create_context_bundle_from_request(
    *,
    request: BundleCreateRequest,
    db: AsyncSession,
    redis=None,
) -> BundleResponse:
    """Create a context bundle from an API request model."""
    return await create_context_bundle(
        workflow_id=request.workflow_id,
        agent_did=request.agent_did,
        scoped_profile_overrides=dict(request.scoped_profile_overrides),
        db=db,
        redis=redis,
    )


async def approve_context_bundle(
    *,
    bundle_id: UUID,
    request: BundleApproveRequest,
    db: AsyncSession,
) -> BundleApproveResponse:
    """Approve a pending, unexpired workflow context bundle."""
    try:
        result = await db.execute(
            text(
                """
                SELECT id, agent_did, status, expires_at
                FROM workflow_context_bundles
                WHERE id = :bundle_id
                  AND agent_did = :agent_did
                """
            ),
            {
                "bundle_id": bundle_id,
                "agent_did": request.agent_did,
            },
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="bundle not found for this agent_did",
            )

        expires_at = _ensure_aware(row["expires_at"])
        if _utc_now() > expires_at:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="bundle expired",
            )
        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="bundle already approved/rejected/consumed",
            )

        update_result = await db.execute(
            text(
                """
                UPDATE workflow_context_bundles
                SET status = 'approved',
                    user_approved_at = NOW()
                WHERE id = :bundle_id
                  AND agent_did = :agent_did
                RETURNING user_approved_at
                """
            ),
            {
                "bundle_id": bundle_id,
                "agent_did": request.agent_did,
            },
        )
        updated = update_result.mappings().first()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to approve context bundle: {exc.__class__.__name__}",
        ) from exc

    return BundleApproveResponse(
        bundle_id=bundle_id,
        status="approved",
        approved_at=updated["user_approved_at"],
    )
