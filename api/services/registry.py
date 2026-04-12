"""Core registry CRUD logic."""

from __future__ import annotations

import json
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
from api.services.embedder import embed_text, semantic_similarity, serialize_embedding
from api.services.ranker import (
    compute_cost_score,
    compute_latency_score,
    compute_rank_score,
    compute_reliability_score,
    compute_trust_score,
    normalize_trust_score,
)

_ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "ontology" / "v0.1.json"


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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown ontology_tag values: {joined}",
        )

    try:
        domain_check = await db.execute(
            text("SELECT id FROM services WHERE domain = :domain"),
            {"domain": manifest.domain},
        )
        existing_domain = domain_check.mappings().first()
        if existing_domain and existing_domain["id"] != manifest.service_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="domain is already registered to a different service_id",
            )

        existing_service = await db.execute(
            text("SELECT id FROM services WHERE id = :service_id"),
            {"service_id": manifest.service_id},
        )
        is_update = existing_service.mappings().first() is not None

        trust_score = _trust_score_for_manifest(manifest)
        status_name = _status_for_manifest(manifest)
        is_active = status_name != "pending_review"

        service_params = {
            "service_id": manifest.service_id,
            "name": manifest.name,
            "domain": manifest.domain,
            "legal_entity": manifest.legal_entity,
            "manifest_url": _manifest_url(manifest.domain),
            "public_key": manifest.public_key,
            "trust_tier": 1,
            "trust_score": trust_score,
            "is_active": is_active,
        }

        if is_update:
            await db.execute(
                text(
                    """
                    UPDATE services
                    SET name = :name,
                        domain = :domain,
                        legal_entity = :legal_entity,
                        manifest_url = :manifest_url,
                        public_key = :public_key,
                        trust_tier = :trust_tier,
                        trust_score = :trust_score,
                        is_active = :is_active,
                        last_crawled_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :service_id
                    """
                ),
                service_params,
            )
        else:
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
                    """
                ),
                service_params,
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
                "raw_json": json.dumps(manifest.model_dump(mode="json")),
                "manifest_hash": _manifest_hash(manifest),
                "manifest_version": manifest.manifest_version,
            },
        )

        await db.execute(
            text("DELETE FROM service_capabilities WHERE service_id = :service_id"),
            {"service_id": manifest.service_id},
        )
        for capability in manifest.capabilities:
            embedding = serialize_embedding(embed_text(capability.description))
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
                {
                    "service_id": manifest.service_id,
                    "ontology_tag": capability.ontology_tag,
                    "description": capability.description,
                    "embedding": embedding,
                    "input_schema_url": (
                        str(capability.input_schema_url) if capability.input_schema_url else None
                    ),
                    "output_schema_url": (
                        str(capability.output_schema_url) if capability.output_schema_url else None
                    ),
                },
            )

        await db.execute(
            text("DELETE FROM service_pricing WHERE service_id = :service_id"),
            {"service_id": manifest.service_id},
        )
        await db.execute(
            text(
                """
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
        for row in _resolve_context_rows(manifest.context.required, True) + _resolve_context_rows(
            manifest.context.optional, False
        ):
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
                {"service_id": manifest.service_id, **row},
            )

        await db.execute(
            text("DELETE FROM service_operations WHERE service_id = :service_id"),
            {"service_id": manifest.service_id},
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
) -> ServiceSearchResponse:
    """Return structured query results for a single ontology tag."""
    ensure_ontology_tag_exists(ontology)

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
              AND (:pricing_model IS NULL OR p.pricing_model = :pricing_model)
              AND (:latency_max_ms IS NULL OR c.avg_latency_ms IS NULL OR c.avg_latency_ms <= :latency_max_ms)
              AND (
                    :geo IS NULL
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
    return ServiceSearchResponse(total=len(results), limit=limit, offset=offset, results=results)


async def search_services(db: AsyncSession, request: SearchRequest) -> ServiceSearchResponse:
    """Return semantic search results using local embeddings."""
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
            WHERE s.is_active = true
              AND s.is_banned = false
              AND s.trust_score >= :trust_min
              AND (
                    :geo IS NULL
                    OR o.geo_restrictions IS NULL
                    OR COALESCE(array_length(o.geo_restrictions, 1), 0) = 0
                    OR :geo = ANY(o.geo_restrictions)
              )
            """
        ),
        {"trust_min": request.trust_min, "geo": request.geo},
    )

    ranked: list[ServiceSummary] = []
    for row in (dict(item) for item in result.mappings().all()):
        candidate_text = f'{row["name"]} {row["ontology_tag"]} {row["description"]}'
        match_score = semantic_similarity(request.query, candidate_text)
        if match_score <= 0:
            continue
        ranked.append(_service_summary_from_row(row, match_score=match_score))

    ranked.sort(key=lambda item: item.rank_score, reverse=True)
    sliced = ranked[request.offset : request.offset + request.limit]
    return ServiceSearchResponse(
        total=len(ranked),
        limit=request.limit,
        offset=request.offset,
        results=sliced,
    )


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

    return ServiceDetail(
        service_id=service_row["service_id"],
        name=service_row["name"],
        domain=service_row["domain"],
        legal_entity=service_row["legal_entity"],
        manifest_url=service_row["manifest_url"],
        public_key=service_row["public_key"],
        trust_tier=service_row["trust_tier"],
        trust_score=service_row["trust_score"],
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
