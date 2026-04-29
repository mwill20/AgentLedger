"""Layer 4 context profile CRUD and rule validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.context import (
    ContextProfileCreateRequest,
    ContextProfileCreateResponse,
    ContextProfileRecord,
    ContextProfileRuleInput,
    ContextProfileRuleRecord,
    ContextProfileUpdateRequest,
)

PROFILE_CACHE_TTL_SECONDS = 60


def _profile_cache_key(agent_did: str) -> str:
    """Build the Redis cache key for one active context profile."""
    return f"context:profile:{agent_did}"


async def _cache_get_profile(redis, agent_did: str) -> ContextProfileRecord | None:
    """Best-effort Redis read for an active context profile."""
    if redis is None:
        return None
    try:
        cached = await redis.get(_profile_cache_key(agent_did))
    except Exception:
        return None
    if not cached:
        return None
    try:
        return ContextProfileRecord.model_validate_json(cached)
    except Exception:
        return None


async def _cache_set_profile(redis, profile: ContextProfileRecord) -> None:
    """Best-effort Redis write for an active context profile."""
    if redis is None:
        return
    try:
        await redis.set(
            _profile_cache_key(profile.agent_did),
            profile.model_dump_json(),
            ex=PROFILE_CACHE_TTL_SECONDS,
        )
    except Exception:
        return


async def _cache_invalidate_profile(redis, agent_did: str) -> None:
    """Best-effort invalidation for an active context profile."""
    if redis is None:
        return
    try:
        await redis.delete(_profile_cache_key(agent_did))
    except Exception:
        return


async def _ensure_agent_exists(db: AsyncSession, agent_did: str) -> None:
    """Require profiles to be attached to an active registered agent DID."""
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


async def _ensure_domain_scopes_exist(
    db: AsyncSession,
    rules: list[ContextProfileRuleInput],
) -> None:
    """Validate domain rules against ontology domains already loaded in Layer 1."""
    domains = sorted({rule.scope_value for rule in rules if rule.scope_type == "domain"})
    if not domains:
        return

    result = await db.execute(
        text(
            """
            SELECT DISTINCT domain
            FROM ontology_tags
            WHERE domain = ANY(CAST(:domains AS TEXT[]))
            """
        ),
        {"domains": domains},
    )
    existing = {row["domain"] for row in result.mappings().all()}
    missing = [domain for domain in domains if domain not in existing]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown ontology domains: {', '.join(missing)}",
        )


def _rule_insert_rows(
    profile_id: Any,
    rules: list[ContextProfileRuleInput],
) -> list[dict[str, Any]]:
    """Build insert rows for one profile's rules."""
    return [
        {
            "profile_id": profile_id,
            "priority": rule.priority,
            "scope_type": rule.scope_type,
            "scope_value": rule.scope_value,
            "permitted_fields": rule.permitted_fields,
            "denied_fields": rule.denied_fields,
            "action": rule.action,
        }
        for rule in rules
    ]


async def _insert_rules(
    db: AsyncSession,
    profile_id: Any,
    rules: list[ContextProfileRuleInput],
) -> None:
    """Insert all rules for a profile."""
    rows = _rule_insert_rows(profile_id, rules)
    if not rows:
        return

    await db.execute(
        text(
            """
            INSERT INTO context_profile_rules (
                profile_id,
                priority,
                scope_type,
                scope_value,
                permitted_fields,
                denied_fields,
                action,
                created_at
            )
            VALUES (
                :profile_id,
                :priority,
                :scope_type,
                :scope_value,
                :permitted_fields,
                :denied_fields,
                :action,
                NOW()
            )
            """
        ),
        rows,
    )


def _to_rule_record(row: Mapping[str, Any]) -> ContextProfileRuleRecord:
    """Map one DB rule row into a response model."""
    return ContextProfileRuleRecord(
        rule_id=row["id"],
        priority=row["priority"],
        scope_type=row["scope_type"],
        scope_value=row["scope_value"],
        permitted_fields=list(row["permitted_fields"] or []),
        denied_fields=list(row["denied_fields"] or []),
        action=row["action"],
        created_at=row["created_at"],
    )


def _build_profile_record(
    profile_row: Mapping[str, Any],
    rule_rows: list[Mapping[str, Any]],
) -> ContextProfileRecord:
    """Map DB profile and rule rows into a sorted profile response."""
    sorted_rules = sorted(
        rule_rows,
        key=lambda row: (row["priority"], row["created_at"], str(row["id"])),
    )
    return ContextProfileRecord(
        profile_id=profile_row["id"],
        agent_did=profile_row["agent_did"],
        profile_name=profile_row["profile_name"],
        is_active=profile_row["is_active"],
        default_policy=profile_row["default_policy"],
        rules=[_to_rule_record(row) for row in sorted_rules],
        created_at=profile_row["created_at"],
        updated_at=profile_row["updated_at"],
    )


async def _get_rules_for_profile(
    db: AsyncSession,
    profile_id: Any,
) -> list[Mapping[str, Any]]:
    """Return stored rules for one profile in priority order."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                priority,
                scope_type,
                scope_value,
                permitted_fields,
                denied_fields,
                action,
                created_at
            FROM context_profile_rules
            WHERE profile_id = :profile_id
            ORDER BY priority ASC, created_at ASC, id ASC
            """
        ),
        {"profile_id": profile_id},
    )
    return list(result.mappings().all())


async def create_profile(
    db: AsyncSession,
    request: ContextProfileCreateRequest,
) -> ContextProfileCreateResponse:
    """Create a context profile and its rules for one registered agent."""
    try:
        await _ensure_agent_exists(db, request.agent_did)
        await _ensure_domain_scopes_exist(db, request.rules)

        result = await db.execute(
            text(
                """
                INSERT INTO context_profiles (
                    agent_did,
                    profile_name,
                    is_active,
                    default_policy,
                    created_at,
                    updated_at
                )
                VALUES (
                    :agent_did,
                    :profile_name,
                    true,
                    :default_policy,
                    NOW(),
                    NOW()
                )
                RETURNING id, created_at
                """
            ),
            {
                "agent_did": request.agent_did,
                "profile_name": request.profile_name,
                "default_policy": request.default_policy,
            },
        )
        profile_row = result.mappings().first()
        await _insert_rules(db, profile_row["id"], request.rules)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="context profile already exists for this agent and profile_name",
        ) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to create context profile: {exc.__class__.__name__}",
        ) from exc

    return ContextProfileCreateResponse(
        profile_id=profile_row["id"],
        agent_did=request.agent_did,
        profile_name=request.profile_name,
        default_policy=request.default_policy,
        rule_count=len(request.rules),
        created_at=profile_row["created_at"],
    )


async def get_active_profile(
    db: AsyncSession,
    agent_did: str,
    redis=None,
) -> ContextProfileRecord:
    """Retrieve the active context profile for one agent DID."""
    cached = await _cache_get_profile(redis, agent_did)
    if cached is not None:
        return cached

    result = await db.execute(
        text(
            """
            SELECT
                id,
                agent_did,
                profile_name,
                is_active,
                default_policy,
                created_at,
                updated_at
            FROM context_profiles
            WHERE agent_did = :agent_did
              AND is_active = true
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"agent_did": agent_did},
    )
    profile_row = result.mappings().first()
    if profile_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="active context profile not found",
        )

    rule_rows = await _get_rules_for_profile(db, profile_row["id"])
    profile = _build_profile_record(profile_row, rule_rows)
    await _cache_set_profile(redis, profile)
    return profile


async def update_active_profile(
    db: AsyncSession,
    agent_did: str,
    request: ContextProfileUpdateRequest,
    redis=None,
) -> ContextProfileRecord:
    """Replace the active profile's rules without creating a new profile."""
    try:
        await _ensure_domain_scopes_exist(db, request.rules)

        result = await db.execute(
            text(
                """
                UPDATE context_profiles
                SET profile_name = :profile_name,
                    default_policy = :default_policy,
                    updated_at = NOW()
                WHERE id = (
                    SELECT id
                    FROM context_profiles
                    WHERE agent_did = :agent_did
                      AND is_active = true
                    ORDER BY updated_at DESC
                    LIMIT 1
                )
                RETURNING
                    id,
                    agent_did,
                    profile_name,
                    is_active,
                    default_policy,
                    created_at,
                    updated_at
                """
            ),
            {
                "agent_did": agent_did,
                "profile_name": request.profile_name,
                "default_policy": request.default_policy,
            },
        )
        profile_row = result.mappings().first()
        if profile_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="active context profile not found",
            )

        await db.execute(
            text("DELETE FROM context_profile_rules WHERE profile_id = :profile_id"),
            {"profile_id": profile_row["id"]},
        )
        await _insert_rules(db, profile_row["id"], request.rules)
        rule_rows = await _get_rules_for_profile(db, profile_row["id"])
        await db.commit()
        await _cache_invalidate_profile(redis, agent_did)
    except HTTPException:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="context profile already exists for this agent and profile_name",
        ) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to update context profile: {exc.__class__.__name__}",
        ) from exc

    return _build_profile_record(profile_row, rule_rows)
