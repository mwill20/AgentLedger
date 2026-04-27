"""Core registry CRUD logic."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.manifest import ContextField, ServiceManifest
from api.models.query import ManifestRegistrationResponse, SearchRequest
from api.models.service import (
    ContextRequirementRecord,
    MatchedCapability,
    OntologyResponse,
    OntologyTagRecord,
    OperationsRecord,
    PricingRecord,
    ServiceDetail,
    ServiceSearchResponse,
    ServiceSummary,
)
from api.services.embedder import (
    embed_batch,
    embed_text,
    semantic_similarity,
    serialize_embedding,
)
from api.services import service_identity
from api.services.typosquat import find_similar_domains
from api.services.ranker import (
    compute_attestation_score,
    compute_cost_score,
    compute_latency_score,
    compute_rank_score,
    compute_reliability_score,
    compute_trust_score,
    normalize_trust_score,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60

_ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "ontology" / "v0.1.json"


async def _cache_get(redis, key: str) -> str | None:
    """Try to read a cached response from Redis."""
    try:
        return await redis.get(key)
    except Exception:
        return None


async def _cache_set(redis, key: str, value: str) -> None:
    """Write a response to Redis with TTL."""
    try:
        await redis.set(key, value, ex=CACHE_TTL_SECONDS)
    except Exception:
        pass  # cache is best-effort


@lru_cache
def load_ontology_payload() -> dict[str, Any]:
    """Load the ontology source-of-truth file."""
    return json.loads(_ONTOLOGY_PATH.read_text(encoding="utf-8"))


@lru_cache
def load_ontology_index() -> dict[str, dict[str, Any]]:
    """Index ontology tags by tag string."""
    payload = load_ontology_payload()
    return {tag["tag"]: tag for tag in payload["tags"]}


def build_ontology_response() -> OntologyResponse:
    """Build the GET /ontology response payload."""
    payload = load_ontology_payload()
    tags = [OntologyTagRecord(**tag) for tag in payload["tags"]]

    by_domain: dict[str, list[OntologyTagRecord]] = {}
    for tag in tags:
        by_domain.setdefault(tag.domain, []).append(tag)

    return OntologyResponse(
        ontology_version=payload["ontology_version"],
        total_tags=len(tags),
        domains=payload["domains"],
        tags=tags,
        by_domain=by_domain,
    )


def ensure_ontology_tag_exists(tag: str) -> None:
    """Validate a caller-supplied ontology tag."""
    if tag not in load_ontology_index():
        raise HTTPException(
            status_code=422,
            detail=f"unknown ontology tag: {tag}",
        )


def _resolve_context_rows(fields: list[ContextField], is_required: bool) -> list[dict[str, Any]]:
    """Map manifest context fields into DB rows."""
    rows: list[dict[str, Any]] = []
    for index, field in enumerate(fields, start=1):
        rows.append(
            {
                "field_name": field.resolved_name(index),
                "field_type": field.resolved_type(),
                "is_required": is_required,
                "sensitivity": field.sensitivity,
            }
        )
    return rows


def _manifest_hash(manifest: ServiceManifest) -> str:
    """Hash the manifest payload for change tracking."""
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def _manifest_url(domain: str) -> str:
    """Build the canonical manifest URL."""
    return f"https://{domain}/.well-known/agent-manifest.json"


def _status_for_manifest(manifest: ServiceManifest) -> str:
    """Flag sensitive manifests for manual review."""
    ontology = load_ontology_index()
    sensitive = any(
        ontology[capability.ontology_tag]["sensitivity_tier"] >= 3
        for capability in manifest.capabilities
    )
    return "pending_review" if sensitive else "registered"


def _trust_score_for_manifest(manifest: ServiceManifest) -> float:
    """Derive an initial trust score for a newly registered service."""
    uptime = manifest.operations.uptime_sla_percent
    operational_score = 0.5 if uptime is None else min(max(uptime / 100.0, 0.0), 1.0)
    return compute_trust_score(0.0, 0.0, operational_score, 0.0)


def _service_summary_from_row(row: dict[str, Any], match_score: float) -> ServiceSummary:
    """Convert a query row into a ranked summary model."""
    rank_score = compute_rank_score(
        capability_match=match_score,
        trust_score=normalize_trust_score(row["trust_score"]),
        latency_score=compute_latency_score(row["avg_latency_ms"]),
        cost_score=compute_cost_score(row["pricing_model"]),
        reliability_score=compute_reliability_score(row["success_rate_30d"]),
        context_fit=1.0,
    )
    return ServiceSummary(
        service_id=row["service_id"],
        name=row["name"],
        domain=row["domain"],
        trust_tier=row["trust_tier"],
        trust_score=row["trust_score"],
        rank_score=rank_score,
        pricing_model=row["pricing_model"],
        is_active=row["is_active"],
        matched_capabilities=[
            MatchedCapability(
                ontology_tag=row["ontology_tag"],
                description=row["description"],
                is_verified=row["is_verified"],
                avg_latency_ms=row["avg_latency_ms"],
                success_rate_30d=row["success_rate_30d"],
                match_score=round(match_score, 6),
            )
        ],
    )


async def register_manifest(
    db: AsyncSession,
    manifest: ServiceManifest,
) -> ManifestRegistrationResponse:
    """Create or update a service and its current manifest."""
    invalid_tags = [
        capability.ontology_tag
        for capability in manifest.capabilities
        if capability.ontology_tag not in load_ontology_index()
    ]
    if invalid_tags:
        joined = ", ".join(sorted(set(invalid_tags)))
        raise HTTPException(
            status_code=422,
            detail=f"unknown ontology_tag values: {joined}",
        )

    if manifest.identity is not None or manifest.signature is not None:
        await service_identity.validate_signed_manifest(manifest=manifest)

    manifest_hash = _manifest_hash(manifest)
    raw_manifest_json = json.dumps(manifest.model_dump(mode="json"))
    trust_score = _trust_score_for_manifest(manifest)
    status_name = _status_for_manifest(manifest)
    is_active = status_name != "pending_review"
    typosquat_warnings: list[str] = []

    try:
        existing_result = await db.execute(
            text(
                """
                SELECT
                    s.id,
                    s.domain,
                    m.manifest_hash
                FROM services s
                LEFT JOIN manifests m
                    ON m.service_id = s.id
                   AND m.is_current = true
                WHERE s.id = :service_id OR s.domain = :domain
                """
            ),
            {"service_id": manifest.service_id, "domain": manifest.domain},
        )
        existing_rows = existing_result.mappings().all()
        existing_service = next(
            (row for row in existing_rows if row["id"] == manifest.service_id),
            None,
        )
        existing_domain = next(
            (row for row in existing_rows if row["domain"] == manifest.domain),
            None,
        )
        if existing_domain and existing_domain["id"] != manifest.service_id:
            raise HTTPException(
                status_code=422,
                detail="domain is already registered to a different service_id",
            )

        is_update = existing_service is not None
        domain_changed = not is_update or existing_service["domain"] != manifest.domain

        # Typosquat detection — compare against all registered domains
        if (
            is_update
            and not domain_changed
            and existing_service["manifest_hash"] == manifest_hash
        ):
            await db.rollback()
            return ManifestRegistrationResponse(
                service_id=manifest.service_id,
                trust_tier=1,
                trust_score=trust_score,
                status="updated",
                capabilities_indexed=len(manifest.capabilities),
                typosquat_warnings=[],
            )

        if domain_changed:
            all_domains_result = await db.execute(
                text("SELECT domain FROM services WHERE id != :service_id"),
                {"service_id": manifest.service_id},
            )
            all_domains = [row["domain"] for row in all_domains_result.mappings().all()]
            typosquat_matches = find_similar_domains(manifest.domain, all_domains)
            typosquat_warnings = [
                f"domain '{manifest.domain}' is similar to existing domain "
                f"'{m['domain']}' (edit distance {m['distance']})"
                for m in typosquat_matches
            ]
            if typosquat_warnings:
                logger.warning(
                    "Typosquat warning for %s: %s",
                    manifest.domain,
                    "; ".join(typosquat_warnings),
                )

        await db.execute(
            text(
                """
                INSERT INTO services (
                    id, name, domain, legal_entity, manifest_url, public_key,
                    trust_tier, trust_score, is_active, created_at, updated_at, first_seen_at
                )
                VALUES (
                    :service_id, :name, :domain, :legal_entity, :manifest_url, :public_key,
                    :trust_tier, :trust_score, :is_active, NOW(), NOW(), NOW()
                )
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    domain = EXCLUDED.domain,
                    legal_entity = EXCLUDED.legal_entity,
                    manifest_url = EXCLUDED.manifest_url,
                    public_key = EXCLUDED.public_key,
                    trust_tier = EXCLUDED.trust_tier,
                    trust_score = EXCLUDED.trust_score,
                    is_active = EXCLUDED.is_active,
                    last_verified_at = NULL,
                    last_crawled_at = NOW(),
                    updated_at = NOW()
                """
            ),
            {
                "service_id": manifest.service_id,
                "name": manifest.name,
                "domain": manifest.domain,
                "legal_entity": manifest.legal_entity,
                "manifest_url": _manifest_url(manifest.domain),
                "public_key": manifest.public_key,
                "trust_tier": 1,
                "trust_score": trust_score,
                "is_active": is_active,
            },
        )

        await db.execute(
            text(
                """
                UPDATE manifests
                SET is_current = false
                WHERE service_id = :service_id AND is_current = true
                """
            ),
            {"service_id": manifest.service_id},
        )
        await db.execute(
            text(
                """
                INSERT INTO manifests (
                    service_id, raw_json, manifest_hash, manifest_version, is_current, crawled_at
                )
                VALUES (
                    :service_id,
                    CAST(:raw_json AS JSONB),
                    :manifest_hash,
                    :manifest_version,
                    true,
                    NOW()
                )
                """
            ),
            {
                "service_id": manifest.service_id,
                "raw_json": raw_manifest_json,
                "manifest_hash": manifest_hash,
                "manifest_version": manifest.manifest_version,
            },
        )
        from . import chain

        await chain.record_chain_event(
            db=db,
            event_type="version",
            service_id=manifest.service_id,
            event_data={
                "service_domain": manifest.domain,
                "service_chain_id": chain.hash_identifier(manifest.domain),
                "manifest_hash": (
                    manifest_hash if manifest_hash.startswith("0x") else f"0x{manifest_hash}"
                ),
                "manifest_version": manifest.manifest_version,
            },
        )

        await db.execute(
            text("DELETE FROM service_capabilities WHERE service_id = :service_id"),
            {"service_id": manifest.service_id},
        )
        capability_rows = []
        capability_embeddings = embed_batch(
            [capability.description for capability in manifest.capabilities]
        )
        for capability, embedding_vector in zip(
            manifest.capabilities,
            capability_embeddings,
            strict=True,
        ):
            capability_rows.append(
                {
                    "service_id": manifest.service_id,
                    "ontology_tag": capability.ontology_tag,
                    "description": capability.description,
                    "embedding": serialize_embedding(embedding_vector),
                    "input_schema_url": (
                        str(capability.input_schema_url) if capability.input_schema_url else None
                    ),
                    "output_schema_url": (
                        str(capability.output_schema_url) if capability.output_schema_url else None
                    ),
                }
            )
        if capability_rows:
            await db.execute(
                text(
                    """
                    INSERT INTO service_capabilities (
                        service_id, ontology_tag, description, embedding, input_schema_url,
                        output_schema_url, is_verified, created_at
                    )
                    VALUES (
                        :service_id,
                        :ontology_tag,
                        :description,
                        CAST(:embedding AS vector),
                        :input_schema_url,
                        :output_schema_url,
                        false,
                        NOW()
                    )
                    """
                ),
                capability_rows,
            )

        await db.execute(
            text(
                """
                WITH deleted AS (
                    DELETE FROM service_pricing
                    WHERE service_id = :service_id
                )
                INSERT INTO service_pricing (
                    service_id, pricing_model, tiers, billing_method, currency, created_at, updated_at
                )
                VALUES (
                    :service_id,
                    :pricing_model,
                    CAST(:tiers AS JSONB),
                    :billing_method,
                    'USD',
                    NOW(),
                    NOW()
                )
                """
            ),
            {
                "service_id": manifest.service_id,
                "pricing_model": manifest.pricing.model,
                "tiers": json.dumps(manifest.pricing.tiers),
                "billing_method": manifest.pricing.billing_method,
            },
        )

        await db.execute(
            text("DELETE FROM service_context_requirements WHERE service_id = :service_id"),
            {"service_id": manifest.service_id},
        )
        context_rows = [
            {"service_id": manifest.service_id, **row}
            for row in (
                _resolve_context_rows(manifest.context.required, True)
                + _resolve_context_rows(manifest.context.optional, False)
            )
        ]
        if context_rows:
            await db.execute(
                text(
                    """
                    INSERT INTO service_context_requirements (
                        service_id, field_name, field_type, is_required, sensitivity, created_at
                    )
                    VALUES (
                        :service_id, :field_name, :field_type, :is_required, :sensitivity, NOW()
                    )
                    """
                ),
                context_rows,
            )
        await db.execute(
            text(
                """
                INSERT INTO service_operations (
                    service_id, uptime_sla_percent, rate_limit_rpm, rate_limit_rpd, sandbox_url,
                    created_at, updated_at
                )
                VALUES (
                    :service_id,
                    :uptime_sla_percent,
                    :rate_limit_rpm,
                    :rate_limit_rpd,
                    :sandbox_url,
                    NOW(),
                    NOW()
                )
                ON CONFLICT (service_id) DO UPDATE
                SET uptime_sla_percent = EXCLUDED.uptime_sla_percent,
                    rate_limit_rpm = EXCLUDED.rate_limit_rpm,
                    rate_limit_rpd = EXCLUDED.rate_limit_rpd,
                    sandbox_url = EXCLUDED.sandbox_url,
                    updated_at = NOW()
                """
            ),
            {
                "service_id": manifest.service_id,
                "uptime_sla_percent": manifest.operations.uptime_sla_percent,
                "rate_limit_rpm": manifest.operations.rate_limits.rpm,
                "rate_limit_rpd": manifest.operations.rate_limits.rpd,
                "sandbox_url": (
                    str(manifest.operations.sandbox_url) if manifest.operations.sandbox_url else None
                ),
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
            detail=f"failed to register manifest: {exc.__class__.__name__}",
        ) from exc

    return ManifestRegistrationResponse(
        service_id=manifest.service_id,
        trust_tier=1,
        trust_score=trust_score,
        status="updated" if is_update else status_name,
        capabilities_indexed=len(manifest.capabilities),
        typosquat_warnings=typosquat_warnings,
    )


async def query_services(
    db: AsyncSession,
    ontology: str,
    trust_min: float = 0,
    trust_tier_min: int = 1,
    geo: str | None = None,
    pricing_model: str | None = None,
    latency_max_ms: int | None = None,
    limit: int = 10,
    offset: int = 0,
    redis=None,
) -> ServiceSearchResponse:
    """Return structured query results for a single ontology tag."""
    ensure_ontology_tag_exists(ontology)

    # Check Redis cache
    cache_key = f"query:{sha256(json.dumps({'ontology': ontology, 'trust_min': trust_min, 'trust_tier_min': trust_tier_min, 'geo': geo, 'pricing_model': pricing_model, 'latency_max_ms': latency_max_ms, 'limit': limit, 'offset': offset}, sort_keys=True).encode()).hexdigest()}"
    if redis is not None:
        cached = await _cache_get(redis, cache_key)
        if cached is not None:
            return ServiceSearchResponse.model_validate_json(cached)

    result = await db.execute(
        text(
            """
            SELECT
                s.id AS service_id,
                s.name,
                s.domain,
                s.trust_tier,
                s.trust_score,
                s.is_active,
                c.ontology_tag,
                c.description,
                c.avg_latency_ms,
                c.success_rate_30d,
                c.is_verified,
                p.pricing_model
            FROM services s
            JOIN service_capabilities c ON c.service_id = s.id
            LEFT JOIN service_operations o ON o.service_id = s.id
            LEFT JOIN service_pricing p ON p.service_id = s.id
            WHERE c.ontology_tag = :ontology
              AND s.is_active = true
              AND s.is_banned = false
              AND s.trust_score >= :trust_min
              AND s.trust_tier >= :trust_tier_min
              AND (CAST(:pricing_model AS TEXT) IS NULL OR p.pricing_model = :pricing_model)
              AND (CAST(:latency_max_ms AS INTEGER) IS NULL OR c.avg_latency_ms IS NULL OR c.avg_latency_ms <= :latency_max_ms)
              AND (
                    CAST(:geo AS TEXT) IS NULL
                    OR o.geo_restrictions IS NULL
                    OR COALESCE(array_length(o.geo_restrictions, 1), 0) = 0
                    OR :geo = ANY(o.geo_restrictions)
              )
            ORDER BY s.trust_score DESC, s.name ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        {
            "ontology": ontology,
            "trust_min": trust_min,
            "trust_tier_min": trust_tier_min,
            "geo": geo,
            "pricing_model": pricing_model,
            "latency_max_ms": latency_max_ms,
            "limit": limit,
            "offset": offset,
        },
    )
    rows = [dict(row) for row in result.mappings().all()]
    results = [_service_summary_from_row(row, match_score=1.0) for row in rows]
    response = ServiceSearchResponse(total=len(results), limit=limit, offset=offset, results=results)

    # Write to cache
    if redis is not None:
        await _cache_set(redis, cache_key, response.model_dump_json())

    return response


async def search_services(db: AsyncSession, request: SearchRequest, redis=None) -> ServiceSearchResponse:
    """Return semantic search results using pgvector cosine similarity."""
    # Check Redis cache
    cache_key = f"search:{sha256(json.dumps({'query': request.query, 'trust_min': request.trust_min, 'geo': request.geo, 'limit': request.limit, 'offset': request.offset}, sort_keys=True).encode()).hexdigest()}"
    if redis is not None:
        cached = await _cache_get(redis, cache_key)
        if cached is not None:
            return ServiceSearchResponse.model_validate_json(cached)

    # Embed query once, push vector into pgvector for DB-side cosine search
    query_embedding = serialize_embedding(embed_text(request.query))

    # pgvector <=> returns cosine distance (0 = identical, 2 = opposite)
    # We fetch a generous candidate set and apply the full ranking algorithm
    candidate_limit = max(request.limit * 5, 50)

    result = await db.execute(
        text(
            """
            SELECT
                s.id AS service_id,
                s.name,
                s.domain,
                s.trust_tier,
                s.trust_score,
                s.is_active,
                c.ontology_tag,
                c.description,
                c.avg_latency_ms,
                c.success_rate_30d,
                c.is_verified,
                p.pricing_model,
                1.0 - (c.embedding <=> CAST(:query_embedding AS vector)) AS cosine_similarity
            FROM services s
            JOIN service_capabilities c ON c.service_id = s.id
            LEFT JOIN service_operations o ON o.service_id = s.id
            LEFT JOIN service_pricing p ON p.service_id = s.id
            WHERE s.is_active = true
              AND s.is_banned = false
              AND s.trust_score >= :trust_min
              AND c.embedding IS NOT NULL
              AND (
                    CAST(:geo AS TEXT) IS NULL
                    OR o.geo_restrictions IS NULL
                    OR COALESCE(array_length(o.geo_restrictions, 1), 0) = 0
                    OR :geo = ANY(o.geo_restrictions)
              )
            ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
            LIMIT :candidate_limit
            """
        ),
        {
            "query_embedding": query_embedding,
            "trust_min": request.trust_min,
            "geo": request.geo,
            "candidate_limit": candidate_limit,
        },
    )

    # Group capabilities by service_id so each service appears once
    service_map: dict[UUID, ServiceSummary] = {}
    for row in (dict(item) for item in result.mappings().all()):
        match_score = max(0.0, min(1.0, float(row["cosine_similarity"])))
        if match_score <= 0:
            continue
        sid = UUID(str(row["service_id"]))
        if sid not in service_map:
            service_map[sid] = _service_summary_from_row(row, match_score=match_score)
        else:
            # Append this capability to the existing service entry
            cap = MatchedCapability(
                ontology_tag=row["ontology_tag"],
                description=row["description"],
                is_verified=row.get("is_verified", False),
                avg_latency_ms=row.get("avg_latency_ms"),
                success_rate_30d=row.get("success_rate_30d"),
                match_score=match_score,
            )
            service_map[sid].matched_capabilities.append(cap)
            # Update rank_score to reflect the best capability match
            best_match = max(
                c.match_score for c in service_map[sid].matched_capabilities
            )
            service_map[sid].rank_score = compute_rank_score(
                capability_match=best_match,
                trust_score=normalize_trust_score(service_map[sid].trust_score),
                latency_score=compute_latency_score(row.get("avg_latency_ms")),
                cost_score=compute_cost_score(row.get("pricing_model")),
                reliability_score=compute_reliability_score(row.get("success_rate_30d")),
                context_fit=1.0,
            )

    ranked = sorted(service_map.values(), key=lambda item: item.rank_score, reverse=True)
    sliced = ranked[request.offset : request.offset + request.limit]
    response = ServiceSearchResponse(
        total=len(ranked),
        limit=request.limit,
        offset=request.offset,
        results=sliced,
    )

    # Write to cache
    if redis is not None:
        await _cache_set(redis, cache_key, response.model_dump_json())

    return response


async def get_service_detail(db: AsyncSession, service_id: UUID) -> ServiceDetail:
    """Fetch the full service record with all related blocks."""
    service_result = await db.execute(
        text(
            """
            SELECT
                id AS service_id,
                name,
                domain,
                legal_entity,
                manifest_url,
                public_key,
                trust_tier,
                trust_score,
                is_active,
                is_banned,
                ban_reason,
                first_seen_at,
                last_crawled_at,
                last_verified_at
            FROM services
            WHERE id = :service_id
            """
        ),
        {"service_id": service_id},
    )
    service_row = service_result.mappings().first()
    if not service_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="service not found")

    manifest_result = await db.execute(
        text(
            """
            SELECT raw_json
            FROM manifests
            WHERE service_id = :service_id AND is_current = true
            ORDER BY crawled_at DESC
            LIMIT 1
            """
        ),
        {"service_id": service_id},
    )
    manifest_row = manifest_result.mappings().first()

    capabilities_result = await db.execute(
        text(
            """
            SELECT ontology_tag, description, is_verified, avg_latency_ms, success_rate_30d
            FROM service_capabilities
            WHERE service_id = :service_id
            ORDER BY ontology_tag
            """
        ),
        {"service_id": service_id},
    )
    capabilities = [
        MatchedCapability(**row) for row in capabilities_result.mappings().all()
    ]

    pricing_result = await db.execute(
        text(
            """
            SELECT pricing_model, tiers, billing_method, currency
            FROM service_pricing
            WHERE service_id = :service_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"service_id": service_id},
    )
    pricing_row = pricing_result.mappings().first()

    context_result = await db.execute(
        text(
            """
            SELECT field_name, field_type, is_required, sensitivity
            FROM service_context_requirements
            WHERE service_id = :service_id
            ORDER BY is_required DESC, field_name ASC
            """
        ),
        {"service_id": service_id},
    )
    context_requirements = [
        ContextRequirementRecord(**row) for row in context_result.mappings().all()
    ]

    operations_result = await db.execute(
        text(
            """
            SELECT
                uptime_sla_percent,
                rate_limit_rpm,
                rate_limit_rpd,
                geo_restrictions,
                compliance_certs,
                sandbox_url,
                deprecation_notice_days
            FROM service_operations
            WHERE service_id = :service_id
            """
        ),
        {"service_id": service_id},
    )
    operations_row = operations_result.mappings().first()

    attestation_result = await db.execute(
        text(
            """
            SELECT
                ar.ontology_scope,
                ar.recorded_at,
                ar.expires_at,
                a.did AS auditor_did
            FROM attestation_records ar
            JOIN auditors a
                ON a.id = ar.auditor_id
            WHERE ar.service_id = :service_id
              AND ar.is_active = true
              AND ar.is_confirmed = true
              AND (ar.expires_at IS NULL OR ar.expires_at > NOW())
              AND a.is_active = true
            ORDER BY ar.recorded_at DESC
            """
        ),
        {"service_id": service_id},
    )
    attestations = []
    for row in attestation_result.mappings().all():
        did_value = row["auditor_did"]
        attestations.append(
            {
                "ontology_scope": row["ontology_scope"],
                "recorded_at": row["recorded_at"],
                "is_expired": False,
                "auditor_org_id": did_value.rsplit(":", 1)[-1],
            }
        )
    attestation_score = compute_attestation_score(
        has_active_service_identity=False,
        attestations=attestations,
    )

    return ServiceDetail(
        service_id=service_row["service_id"],
        name=service_row["name"],
        domain=service_row["domain"],
        legal_entity=service_row["legal_entity"],
        manifest_url=service_row["manifest_url"],
        public_key=service_row["public_key"],
        trust_tier=service_row["trust_tier"],
        trust_score=service_row["trust_score"],
        attestation_score=attestation_score,
        is_active=service_row["is_active"],
        is_banned=service_row["is_banned"],
        ban_reason=service_row["ban_reason"],
        first_seen_at=service_row["first_seen_at"],
        last_crawled_at=service_row["last_crawled_at"],
        last_verified_at=service_row["last_verified_at"],
        current_manifest=manifest_row["raw_json"] if manifest_row else {},
        capabilities=capabilities,
        pricing=PricingRecord(**pricing_row) if pricing_row else None,
        context_requirements=context_requirements,
        operations=OperationsRecord(**operations_row) if operations_row else None,
    )
