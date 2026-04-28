"""Layer 4 selective disclosure commitment helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.context import (
    ContextMatchResponse,
    DisclosureListResponse,
    DisclosurePackage,
    DisclosureRecord,
    DisclosureRequest,
    DisclosureRevokeRequest,
    DisclosureRevokeResponse,
)
from api.services import context_mismatch, trust


COMMITMENT_TTL_SECONDS = 300


def generate_commitment(field_value: str) -> tuple[str, str]:
    """Return an HMAC-SHA256 commitment hash and nonce."""
    nonce = secrets.token_hex(32)
    commitment = hmac.new(
        key=nonce.encode(),
        msg=field_value.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return commitment, nonce


def verify_commitment(
    commitment_hash: str,
    nonce: str,
    field_value: str,
) -> bool:
    """Verify that nonce and field value match a commitment."""
    expected = hmac.new(
        key=nonce.encode(),
        msg=field_value.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(commitment_hash, expected)


async def create_commitments(
    db: AsyncSession,
    *,
    match_id: UUID | None = None,
    agent_did: str,
    service_id: UUID,
    session_assertion_id: UUID | None,
    field_names: list[str],
    fields_requested: list[str] | None = None,
    fields_permitted: list[str] | None = None,
    fields_withheld: list[str] | None = None,
    fields_committed: list[str] | None = None,
) -> list[UUID]:
    """Persist HMAC commitments for committed context fields."""
    if not field_names:
        return []

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=COMMITMENT_TTL_SECONDS)
    commitment_ids: list[UUID] = []
    for field_name in field_names:
        commitment_hash, nonce = generate_commitment(field_name)
        result = await db.execute(
            text(
                """
                INSERT INTO context_commitments (
                    match_id,
                    agent_did,
                    service_id,
                    session_assertion_id,
                    field_name,
                    commitment_hash,
                    nonce,
                    nonce_released,
                    expires_at,
                    fields_requested,
                    fields_permitted,
                    fields_withheld,
                    fields_committed,
                    created_at
                )
                VALUES (
                    :match_id,
                    :agent_did,
                    :service_id,
                    :session_assertion_id,
                    :field_name,
                    :commitment_hash,
                    :nonce,
                    false,
                    :expires_at,
                    :fields_requested,
                    :fields_permitted,
                    :fields_withheld,
                    :fields_committed,
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "match_id": match_id,
                "agent_did": agent_did,
                "service_id": service_id,
                "session_assertion_id": session_assertion_id,
                "field_name": field_name,
                "commitment_hash": commitment_hash,
                "nonce": nonce,
                "expires_at": expires_at,
                "fields_requested": fields_requested or [],
                "fields_permitted": fields_permitted or [],
                "fields_withheld": fields_withheld or [],
                "fields_committed": fields_committed or field_names,
            },
        )
        commitment_ids.append(result.scalar_one())
    return commitment_ids


@dataclass(frozen=True)
class _MatchSnapshot:
    """Match result fields needed to execute disclosure."""

    match_id: UUID
    session_assertion_id: UUID | None
    permitted_fields: list[str]
    withheld_fields: list[str]
    committed_fields: list[str]
    commitment_ids: list[UUID]


@dataclass(frozen=True)
class _ServiceTrustState:
    """Current service trust state at disclosure time."""

    ontology_tag: str
    trust_tier: int
    trust_score: float


def _now_utc() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _required_trust_tier(sensitivity_tier: int) -> int:
    """Map a context sensitivity tier to the required service trust tier."""
    if sensitivity_tier >= 4:
        return 4
    if sensitivity_tier >= 3:
        return 3
    return 2


def _fields_requested(snapshot: _MatchSnapshot) -> list[str]:
    """Rebuild the requested field set from match buckets."""
    fields: list[str] = []
    for field in (
        snapshot.permitted_fields
        + snapshot.withheld_fields
        + snapshot.committed_fields
    ):
        if field not in fields:
            fields.append(field)
    return fields


def _disclosure_method(
    permitted_fields: list[str],
    committed_fields: list[str],
) -> str:
    """Represent the disclosure methods used by this audit row."""
    methods = []
    if permitted_fields:
        methods.append("direct")
    if committed_fields:
        methods.append("committed")
    return "+".join(methods) if methods else "none"


async def _load_match_from_redis(
    redis,
    match_id: UUID,
) -> _MatchSnapshot | None:
    """Load the cached match result if Redis still has it."""
    if redis is None:
        return None
    try:
        raw = await redis.get(f"context:match:{match_id}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        response = ContextMatchResponse.model_validate_json(raw)
    except Exception:
        try:
            response = ContextMatchResponse.model_validate(json.loads(raw))
        except Exception:
            return None
    return _MatchSnapshot(
        match_id=response.match_id,
        session_assertion_id=response.session_assertion_id,
        permitted_fields=response.permitted_fields,
        withheld_fields=response.withheld_fields,
        committed_fields=response.committed_fields,
        commitment_ids=response.commitment_ids,
    )


async def _load_commitment_rows(
    db: AsyncSession,
    request: DisclosureRequest,
) -> list[dict[str, Any]]:
    """Load commitment rows for this match from Postgres."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                match_id,
                agent_did,
                service_id,
                session_assertion_id,
                field_name,
                nonce,
                expires_at,
                fields_requested,
                fields_permitted,
                fields_withheld,
                fields_committed
            FROM context_commitments
            WHERE match_id = :match_id
              AND agent_did = :agent_did
              AND service_id = :service_id
            ORDER BY field_name ASC
            """
        ),
        {
            "match_id": request.match_id,
            "agent_did": request.agent_did,
            "service_id": request.service_id,
        },
    )
    rows = [dict(row) for row in result.mappings().all()]
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="match_id expired or not found",
        )

    if request.commitment_ids:
        requested_ids = set(request.commitment_ids)
        row_ids = {row["id"] for row in rows}
        if not requested_ids.issubset(row_ids):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="match_id expired or not found",
            )
        rows = [row for row in rows if row["id"] in requested_ids]

    now = _now_utc()
    if any(_as_utc(row["expires_at"]) <= now for row in rows):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="match_id expired or not found",
        )
    return rows


def _as_utc(value: datetime) -> datetime:
    """Normalize timestamps from database rows for expiry comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _snapshot_from_commitments(rows: list[dict[str, Any]]) -> _MatchSnapshot:
    """Reconstruct a match snapshot from commitment metadata rows."""
    first = rows[0]
    return _MatchSnapshot(
        match_id=first["match_id"],
        session_assertion_id=first["session_assertion_id"],
        permitted_fields=list(first.get("fields_permitted") or []),
        withheld_fields=list(first.get("fields_withheld") or []),
        committed_fields=list(first.get("fields_committed") or []),
        commitment_ids=[row["id"] for row in rows],
    )


async def _load_service_trust_state(
    db: AsyncSession,
    service_id: UUID,
    redis=None,
) -> _ServiceTrustState:
    """Load current service trust state for disclose-time re-verification."""
    cached = await trust.get_cached_service_trust(redis, str(service_id))
    if cached is not None and cached.get("ontology_tag"):
        return _ServiceTrustState(
            ontology_tag=str(cached["ontology_tag"]),
            trust_tier=int(cached["trust_tier"]),
            trust_score=float(cached["trust_score"]),
        )

    result = await db.execute(
        text(
            """
            SELECT
                sc.ontology_tag,
                s.trust_tier,
                s.trust_score
            FROM services s
            JOIN service_capabilities sc ON sc.service_id = s.id
            WHERE s.id = :service_id
              AND s.is_active = true
              AND s.is_banned = false
            ORDER BY sc.ontology_tag ASC
            LIMIT 1
            """
        ),
        {"service_id": service_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="service is not active for disclosure",
        )
    service = _ServiceTrustState(
        ontology_tag=row["ontology_tag"],
        trust_tier=int(row["trust_tier"] or 1),
        trust_score=float(row["trust_score"] or 0.0),
    )
    await trust.cache_service_trust(
        redis,
        str(service_id),
        {
            "ontology_tag": service.ontology_tag,
            "trust_tier": service.trust_tier,
            "trust_score": service.trust_score,
        },
    )
    return service


async def _load_field_sensitivity_tiers(
    db: AsyncSession,
    service_id: UUID,
    field_names: list[str],
) -> dict[str, int]:
    """Load sensitivity tiers for committed fields from the manifest context."""
    if not field_names:
        return {}
    result = await db.execute(
        text(
            """
            SELECT field_name, sensitivity
            FROM service_context_requirements
            WHERE service_id = :service_id
              AND field_name = ANY(CAST(:field_names AS TEXT[]))
            """
        ),
        {"service_id": service_id, "field_names": field_names},
    )
    rows = result.mappings().all()
    tiers = {
        row["field_name"]: context_mismatch.get_sensitivity_tier(
            row["field_name"],
            row["sensitivity"],
        )
        for row in rows
    }
    return {
        field: tiers.get(field, context_mismatch.get_sensitivity_tier(field))
        for field in field_names
    }


def _enforce_disclose_trust(
    service: _ServiceTrustState,
    field_sensitivity_tiers: dict[str, int],
) -> None:
    """Hard-block disclosure when trust dropped below committed-field thresholds."""
    failures = []
    for field, sensitivity_tier in field_sensitivity_tiers.items():
        required_tier = _required_trust_tier(sensitivity_tier)
        if service.trust_tier < required_tier:
            failures.append(
                {
                    "field": field,
                    "sensitivity_tier": sensitivity_tier,
                    "required_trust_tier": required_tier,
                }
            )
    if failures:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "trust_threshold_failed": True,
                "trust_tier": service.trust_tier,
                "fields": failures,
            },
        )


async def _release_nonces(
    db: AsyncSession,
    request: DisclosureRequest,
    commitment_ids: list[UUID],
) -> dict[str, str]:
    """Mark commitments released and return field-to-nonce mappings."""
    if not commitment_ids:
        return {}
    result = await db.execute(
        text(
            """
            UPDATE context_commitments
            SET nonce_released = true,
                nonce_released_at = NOW()
            WHERE match_id = :match_id
              AND agent_did = :agent_did
              AND service_id = :service_id
              AND id = ANY(CAST(:commitment_ids AS UUID[]))
            RETURNING field_name, nonce
            """
        ),
        {
            "match_id": request.match_id,
            "agent_did": request.agent_did,
            "service_id": request.service_id,
            "commitment_ids": commitment_ids,
        },
    )
    rows = result.mappings().all()
    return {row["field_name"]: row["nonce"] for row in rows}


async def _insert_disclosure(
    db: AsyncSession,
    request: DisclosureRequest,
    snapshot: _MatchSnapshot,
    service: _ServiceTrustState,
) -> tuple[UUID, datetime]:
    """Write the append-only disclosure audit record."""
    result = await db.execute(
        text(
            """
            INSERT INTO context_disclosures (
                agent_did,
                service_id,
                session_assertion_id,
                ontology_tag,
                fields_requested,
                fields_disclosed,
                fields_withheld,
                fields_committed,
                disclosure_method,
                trust_score_at_disclosure,
                trust_tier_at_disclosure,
                erased,
                created_at
            )
            VALUES (
                :agent_did,
                :service_id,
                :session_assertion_id,
                :ontology_tag,
                :fields_requested,
                :fields_disclosed,
                :fields_withheld,
                :fields_committed,
                :disclosure_method,
                :trust_score_at_disclosure,
                :trust_tier_at_disclosure,
                false,
                NOW()
            )
            RETURNING id, created_at
            """
        ),
        {
            "agent_did": request.agent_did,
            "service_id": request.service_id,
            "session_assertion_id": snapshot.session_assertion_id,
            "ontology_tag": service.ontology_tag,
            "fields_requested": _fields_requested(snapshot),
            "fields_disclosed": snapshot.permitted_fields,
            "fields_withheld": snapshot.withheld_fields,
            "fields_committed": snapshot.committed_fields,
            "disclosure_method": _disclosure_method(
                snapshot.permitted_fields,
                snapshot.committed_fields,
            ),
            "trust_score_at_disclosure": service.trust_score,
            "trust_tier_at_disclosure": service.trust_tier,
        },
    )
    row = result.mappings().first()
    return row["id"], row["created_at"]


async def disclose_context(
    db: AsyncSession,
    request: DisclosureRequest,
    redis=None,
) -> DisclosurePackage:
    """Release committed-field nonces and write a field-name-only audit record."""
    try:
        cached_snapshot = await _load_match_from_redis(redis, request.match_id)
        commitment_rows = await _load_commitment_rows(db, request)
        snapshot = cached_snapshot or _snapshot_from_commitments(commitment_rows)
        commitment_ids = [row["id"] for row in commitment_rows]
        committed_fields = [row["field_name"] for row in commitment_rows]

        service = await _load_service_trust_state(db, request.service_id, redis=redis)
        sensitivity_tiers = await _load_field_sensitivity_tiers(
            db,
            request.service_id,
            committed_fields,
        )
        _enforce_disclose_trust(service, sensitivity_tiers)

        nonces = await _release_nonces(db, request, commitment_ids)
        disclosure_id, disclosed_at = await _insert_disclosure(
            db,
            request,
            snapshot,
            service,
        )
        await db.commit()

        expires_at = min(_as_utc(row["expires_at"]) for row in commitment_rows)
        return DisclosurePackage(
            disclosure_id=disclosure_id,
            permitted_fields={
                field: request.field_values[field]
                for field in snapshot.permitted_fields
                if field in request.field_values
            },
            committed_field_nonces=nonces,
            disclosed_at=disclosed_at,
            expires_at=expires_at,
        )
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to disclose context: {exc.__class__.__name__}",
        ) from exc


def _build_disclosure_record(row: dict[str, Any]) -> DisclosureRecord:
    """Build a field-name-only disclosure response record."""
    return DisclosureRecord(
        disclosure_id=row["id"],
        agent_did=row["agent_did"],
        service_id=row["service_id"],
        ontology_tag=row["ontology_tag"],
        fields_requested=row.get("fields_requested"),
        fields_disclosed=row.get("fields_disclosed"),
        fields_withheld=row.get("fields_withheld"),
        fields_committed=row.get("fields_committed"),
        disclosure_method=row["disclosure_method"],
        trust_score_at_disclosure=row["trust_score_at_disclosure"],
        trust_tier_at_disclosure=row["trust_tier_at_disclosure"],
        erased=row["erased"],
        erased_at=row["erased_at"],
        created_at=row["created_at"],
    )


async def list_disclosures(
    db: AsyncSession,
    *,
    agent_did: str,
    service_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DisclosureListResponse:
    """List field-name-only disclosure audit records."""
    filters = ["agent_did = :agent_did"]
    params: dict[str, Any] = {
        "agent_did": agent_did,
        "limit": limit,
        "offset": offset,
    }
    if service_id is not None:
        filters.append("service_id = :service_id")
        params["service_id"] = service_id
    if from_date is not None:
        filters.append("created_at >= :from_date")
        params["from_date"] = from_date
    if to_date is not None:
        filters.append("created_at <= :to_date")
        params["to_date"] = to_date

    where_clause = " AND ".join(filters)
    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                agent_did,
                service_id,
                ontology_tag,
                fields_requested,
                fields_disclosed,
                fields_withheld,
                fields_committed,
                disclosure_method,
                trust_score_at_disclosure,
                trust_tier_at_disclosure,
                erased,
                erased_at,
                created_at,
                COUNT(*) OVER() AS total_count
            FROM context_disclosures
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    )
    rows = [dict(row) for row in result.mappings().all()]
    total = int(rows[0]["total_count"]) if rows else 0
    return DisclosureListResponse(
        total=total,
        limit=limit,
        offset=offset,
        disclosures=[_build_disclosure_record(row) for row in rows],
    )


async def revoke_disclosure(
    db: AsyncSession,
    *,
    disclosure_id: UUID,
    request: DisclosureRevokeRequest,
) -> DisclosureRevokeResponse:
    """Mark one disclosure erased while retaining the audit record."""
    try:
        result = await db.execute(
            text(
                """
                UPDATE context_disclosures
                SET erased = true,
                    erased_at = NOW(),
                    fields_requested = '{}',
                    fields_disclosed = '{}',
                    fields_withheld = '{}',
                    fields_committed = '{}'
                WHERE id = :disclosure_id
                  AND agent_did = :agent_did
                RETURNING id, erased_at
                """
            ),
            {
                "disclosure_id": disclosure_id,
                "agent_did": request.agent_did,
            },
        )
        row = result.mappings().first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="disclosure not found for agent_did",
            )
        await db.commit()
        return DisclosureRevokeResponse(
            disclosure_id=row["id"],
            erased_at=row["erased_at"],
        )
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to revoke disclosure: {exc.__class__.__name__}",
        ) from exc
