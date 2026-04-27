"""Service and ontology response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class OntologyTagRecord(BaseModel):
    """One ontology tag entry."""

    tag: str
    domain: str
    function: str
    label: str
    description: str
    sensitivity_tier: int


class OntologyResponse(BaseModel):
    """GET /ontology response."""

    ontology_version: str
    total_tags: int
    domains: list[str]
    tags: list[OntologyTagRecord]
    by_domain: dict[str, list[OntologyTagRecord]]


class MatchedCapability(BaseModel):
    """Capability returned in search results."""

    ontology_tag: str
    description: str
    is_verified: bool = False
    avg_latency_ms: int | None = None
    success_rate_30d: float | None = None
    match_score: float | None = None


class ServiceSummary(BaseModel):
    """Summary row for search results."""

    service_id: UUID
    name: str
    domain: str
    trust_tier: int
    trust_score: float
    rank_score: float
    pricing_model: str | None = None
    is_active: bool
    matched_capabilities: list[MatchedCapability] = Field(default_factory=list)


class ServiceSearchResponse(BaseModel):
    """Collection response for list and search endpoints."""

    total: int
    limit: int
    offset: int
    results: list[ServiceSummary]


class PricingRecord(BaseModel):
    """Service pricing block."""

    pricing_model: str
    tiers: list[dict[str, Any]] = Field(default_factory=list)
    billing_method: str | None = None
    currency: str = "USD"


class ContextRequirementRecord(BaseModel):
    """Context requirement row."""

    field_name: str
    field_type: str
    is_required: bool
    sensitivity: str


class OperationsRecord(BaseModel):
    """Operational metadata row."""

    uptime_sla_percent: float | None = None
    rate_limit_rpm: int | None = None
    rate_limit_rpd: int | None = None
    geo_restrictions: list[str] = Field(default_factory=list)
    compliance_certs: list[str] = Field(default_factory=list)
    sandbox_url: str | None = None
    deprecation_notice_days: int | None = None


class ServiceDetail(BaseModel):
    """Detailed service record for GET /services/{service_id}."""

    service_id: UUID
    name: str
    domain: str
    legal_entity: str | None = None
    manifest_url: str
    public_key: str | None = None
    trust_tier: int
    trust_score: float
    attestation_score: float | None = None
    is_active: bool
    is_banned: bool
    ban_reason: str | None = None
    first_seen_at: datetime | None = None
    last_crawled_at: datetime | None = None
    last_verified_at: datetime | None = None
    current_manifest: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[MatchedCapability] = Field(default_factory=list)
    pricing: PricingRecord | None = None
    context_requirements: list[ContextRequirementRecord] = Field(default_factory=list)
    operations: OperationsRecord | None = None
