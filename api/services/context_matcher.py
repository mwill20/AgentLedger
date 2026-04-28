"""Layer 4 context matching engine."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.context import ContextMatchRequest, ContextMatchResponse
from api.services import context_disclosure, context_mismatch, context_profiles, credentials
from api.services.context_mismatch import ManifestContextBlock


MATCH_TTL_SECONDS = 300
MATCH_RATE_LIMIT_PER_MINUTE = 100
MATCH_RATE_LIMIT_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class ServiceContext:
    """Service facts needed for profile rule matching and trust gates."""

    service_id: UUID
    domain: str
    did: str
    ontology_tag: str
    ontology_domain: str
    trust_tier: int
    trust_score: float
    declared_required_fields: list[str]
    declared_optional_fields: list[str]
    field_sensitivity_tiers: dict[str, int]


async def _verify_session_assertion(
    db: AsyncSession,
    request: ContextMatchRequest,
) -> tuple[UUID | None, str | None]:
    """Verify or phase-3-stub a Layer 2 assertion and return DB id/tag."""
    fallback_mode = False
    try:
        claims = credentials.verify_session_assertion(request.session_assertion)
    except Exception:
        fallback_mode = True
        claims = _decode_unverified_session_assertion(request.session_assertion)

    if claims.get("sub") and claims.get("sub") != request.agent_did:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session assertion subject does not match agent_did",
        )
    if claims.get("service_id") and str(claims.get("service_id")) != str(request.service_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session assertion service binding is invalid",
        )

    assertion_jti = claims.get("jti")
    ontology_tag = claims.get("ontology_tag")
    if not assertion_jti:
        return None, ontology_tag

    result = await db.execute(
        text(
            """
            SELECT id, ontology_tag, expires_at
            FROM session_assertions
            WHERE assertion_jti = :assertion_jti
              AND agent_did = :agent_did
              AND service_id = :service_id
              AND expires_at > NOW()
            """
        ),
        {
            "assertion_jti": assertion_jti,
            "agent_did": request.agent_did,
            "service_id": request.service_id,
        },
    )
    row = result.mappings().first()
    if row is None:
        if fallback_mode:
            return None, ontology_tag
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session assertion is not active for this service",
        )

    resolved_ontology_tag = ontology_tag or row["ontology_tag"]
    if resolved_ontology_tag != row["ontology_tag"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="session assertion ontology_tag is invalid",
        )
    return row["id"], resolved_ontology_tag


def _decode_unverified_session_assertion(token: str) -> dict[str, Any]:
    """Accept any JWT-shaped token for Phase 3, using claims when readable."""
    segments = token.split(".")
    if len(segments) < 3:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or expired session assertion",
        )
    payload = segments[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{payload}{padding}".encode())
        claims = json.loads(decoded.decode())
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


async def _enforce_match_rate_limit(redis, agent_did: str) -> None:
    """Limit context match attempts per agent DID."""
    if redis is None:
        return
    key = f"context:match:rate:{agent_did}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, MATCH_RATE_LIMIT_WINDOW_SECONDS)
        if current <= MATCH_RATE_LIMIT_PER_MINUTE:
            return
        try:
            retry_after = max(1, int(await redis.ttl(key)))
        except Exception:
            retry_after = MATCH_RATE_LIMIT_WINDOW_SECONDS
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "rate_limited": True,
                "agent_did": agent_did,
                "limit": MATCH_RATE_LIMIT_PER_MINUTE,
                "window_seconds": MATCH_RATE_LIMIT_WINDOW_SECONDS,
                "retry_after_seconds": retry_after,
            },
        )
    except HTTPException:
        raise
    except Exception:
        return


async def _load_service_context(
    db: AsyncSession,
    service_id: UUID,
    ontology_tag: str | None,
) -> ServiceContext:
    """Load service trust state, ontology domain, and declared context fields."""
    ontology_filter = ""
    params: dict[str, Any] = {"service_id": service_id}
    if ontology_tag:
        ontology_filter = "AND sc.ontology_tag = :ontology_tag"
        params["ontology_tag"] = ontology_tag

    service_result = await db.execute(
        text(
            f"""
            SELECT
                s.id,
                s.domain,
                sc.ontology_tag,
                s.trust_tier,
                s.trust_score,
                ot.domain AS ontology_domain
            FROM services s
            JOIN service_capabilities sc
                ON sc.service_id = s.id
               {ontology_filter}
            JOIN ontology_tags ot
                ON ot.tag = sc.ontology_tag
            WHERE s.id = :service_id
              AND s.is_active = true
              AND s.is_banned = false
            ORDER BY sc.ontology_tag ASC
            LIMIT 1
            """
        ),
        params,
    )
    service_row = service_result.mappings().first()
    if service_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="service capability not found",
        )

    context_result = await db.execute(
        text(
            """
            SELECT field_name, is_required, sensitivity
            FROM service_context_requirements
            WHERE service_id = :service_id
            ORDER BY is_required DESC, field_name ASC
            """
        ),
        {"service_id": service_id},
    )
    required: list[str] = []
    optional: list[str] = []
    sensitivity_tiers: dict[str, int] = {}
    for row in context_result.mappings().all():
        field_name = row["field_name"]
        if row["is_required"]:
            required.append(field_name)
        else:
            optional.append(field_name)
        sensitivity_tiers[field_name] = context_mismatch.get_sensitivity_tier(
            field_name,
            row["sensitivity"],
        )

    return ServiceContext(
        service_id=service_row["id"],
        domain=service_row["domain"],
        did=f"did:web:{service_row['domain']}",
        ontology_tag=service_row["ontology_tag"],
        ontology_domain=service_row["ontology_domain"],
        trust_tier=int(service_row["trust_tier"] or 1),
        trust_score=float(service_row["trust_score"] or 0.0),
        declared_required_fields=required,
        declared_optional_fields=optional,
        field_sensitivity_tiers=sensitivity_tiers,
    )


async def _record_mismatch_event(
    db: AsyncSession,
    request: ContextMatchRequest,
    service: ServiceContext,
    mismatch: context_mismatch.MismatchResult,
) -> Any:
    """Persist one mismatch event."""
    declared_fields = service.declared_required_fields + service.declared_optional_fields
    event = await context_mismatch._record_mismatch_event(
        db=db,
        request=request,
        declared_fields=declared_fields,
        mismatch=mismatch,
    )
    await db.commit()
    return event


def _required_trust_tier(sensitivity_tier: int) -> int:
    """Map a sensitivity tier to the minimum service trust tier."""
    if sensitivity_tier >= 4:
        return 4
    if sensitivity_tier >= 3:
        return 3
    return 2


def _check_trust_thresholds(
    requested_fields: list[str],
    service: ServiceContext,
) -> list[str]:
    """Reject required trust failures and return optional fields to withhold."""
    insufficient: list[dict[str, int | str]] = []
    withheld_optional: list[str] = []
    required_fields = set(service.declared_required_fields)
    for field in requested_fields:
        sensitivity_tier = service.field_sensitivity_tiers.get(
            field,
            context_mismatch.get_sensitivity_tier(field),
        )
        required_tier = _required_trust_tier(sensitivity_tier)
        if service.trust_tier < required_tier:
            failure = {
                "field": field,
                "sensitivity_tier": sensitivity_tier,
                "required_trust_tier": required_tier,
            }
            if field in required_fields:
                insufficient.append(failure)
            else:
                withheld_optional.append(field)
    if insufficient:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "trust_threshold_failed": True,
                "trust_tier": service.trust_tier,
                "fields": insufficient,
            },
        )
    return withheld_optional


def rule_matches_service(rule: Any, service: ServiceContext) -> bool:
    """Return whether a profile rule applies to a service."""
    if rule.scope_type == "domain":
        return service.ontology_domain == rule.scope_value
    if rule.scope_type == "trust_tier":
        return service.trust_tier >= int(rule.scope_value)
    if rule.scope_type == "service_did":
        return service.did == rule.scope_value
    if rule.scope_type == "sensitivity":
        return True
    return False


def evaluate_profile(
    rules: list[Any],
    field: str,
    service: ServiceContext,
    default_policy: str,
) -> str:
    """Evaluate one field against profile rules."""
    for rule in sorted(rules, key=lambda item: item.priority):
        if not rule_matches_service(rule, service):
            continue

        if field in rule.denied_fields:
            return "withhold"

        if field in rule.permitted_fields:
            sensitivity = service.field_sensitivity_tiers.get(
                field,
                context_mismatch.get_sensitivity_tier(field),
            )
            if sensitivity >= 3:
                return "commit"
            return "permit"

    if default_policy == "allow":
        sensitivity = service.field_sensitivity_tiers.get(
            field,
            context_mismatch.get_sensitivity_tier(field),
        )
        if sensitivity >= 3:
            return "commit"
        return "permit"

    return "withhold"


def _classify_fields(
    requested_fields: list[str],
    profile: Any,
    service: ServiceContext,
    prewithheld_fields: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Classify requested fields into permitted, withheld, and committed buckets."""
    permitted: list[str] = []
    withheld: list[str] = []
    committed: list[str] = []
    prewithheld = set(prewithheld_fields or [])
    for field in requested_fields:
        if field in prewithheld:
            withheld.append(field)
            continue

        decision = evaluate_profile(profile.rules, field, service, profile.default_policy)
        if decision == "permit":
            permitted.append(field)
        elif decision == "commit":
            committed.append(field)
        else:
            withheld.append(field)
    return permitted, withheld, committed


async def _cache_match_result(redis, response: ContextMatchResponse) -> None:
    """Best-effort Redis cache for match results."""
    if redis is None:
        return
    try:
        await redis.set(
            f"context:match:{response.match_id}",
            response.model_dump_json(),
            ex=MATCH_TTL_SECONDS,
        )
    except Exception:
        return


async def match_context_request(
    db: AsyncSession,
    request: ContextMatchRequest,
    redis=None,
) -> ContextMatchResponse:
    """Run the full Layer 4 context matching flow."""
    mismatch_event = None
    try:
        await _enforce_match_rate_limit(redis, request.agent_did)
        session_assertion_id, ontology_tag = await _verify_session_assertion(db, request)
        service = await _load_service_context(db, request.service_id, ontology_tag)

        mismatch = context_mismatch.detect_mismatch(
            request.requested_fields,
            ManifestContextBlock(
                required=service.declared_required_fields,
                optional=service.declared_optional_fields,
            ),
        )
        if mismatch.detected:
            mismatch_event = await _record_mismatch_event(db, request, service, mismatch)
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

        trust_withheld = _check_trust_thresholds(request.requested_fields, service)
        profile = await context_profiles.get_active_profile(
            db,
            request.agent_did,
            redis=redis,
        )
        permitted, withheld, committed = _classify_fields(
            request.requested_fields,
            profile,
            service,
            prewithheld_fields=trust_withheld,
        )
        match_id = uuid4()
        commitment_ids = await context_disclosure.create_commitments(
            db=db,
            match_id=match_id,
            agent_did=request.agent_did,
            service_id=request.service_id,
            session_assertion_id=session_assertion_id,
            field_names=committed,
            fields_requested=request.requested_fields,
            fields_permitted=permitted,
            fields_withheld=withheld,
            fields_committed=committed,
        )
        response = ContextMatchResponse(
            match_id=match_id,
            session_assertion_id=session_assertion_id,
            permitted_fields=permitted,
            withheld_fields=withheld,
            committed_fields=committed,
            commitment_ids=commitment_ids,
            mismatch_detected=False,
            trust_tier_at_match=service.trust_tier,
            trust_score_at_match=service.trust_score,
            can_disclose=bool(permitted or committed),
        )
        await db.commit()
        await _cache_match_result(redis, response)
        return response
    except HTTPException:
        if mismatch_event is None:
            await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to match context request: {exc.__class__.__name__}",
        ) from exc
