"""Manifest request models."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from api.models.sanitize import check_null_bytes_recursive, strip_strings_recursive

_FQDN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class CapabilityManifest(BaseModel):
    """Declared service capability."""

    id: str = Field(max_length=200)
    ontology_tag: str = Field(pattern=r"^[a-z]+\.[a-z]+\.[a-z]+$")
    description: str = Field(min_length=20, max_length=2000)
    input_schema_url: HttpUrl | None = None
    output_schema_url: HttpUrl | None = None


class PricingManifest(BaseModel):
    """Pricing block for a manifest."""

    model: Literal["per_transaction", "subscription", "freemium", "free"]
    tiers: list[dict[str, Any]] = Field(default_factory=list)
    billing_method: Literal["x402", "stripe", "api_key", "none"] | None = None


class ContextField(BaseModel):
    """Context field descriptor."""

    name: str | None = None
    field_name: str | None = None
    id: str | None = None
    type: str | None = None
    field_type: str | None = None
    sensitivity: Literal["low", "medium", "high", "critical"] = "low"
    description: str | None = None

    model_config = {"extra": "allow"}

    def resolved_name(self, index: int) -> str:
        """Resolve a stable field name for persistence."""
        return self.field_name or self.name or self.id or f"context_field_{index}"

    def resolved_type(self) -> str:
        """Resolve a stable field type for persistence."""
        return self.field_type or self.type or "string"


class ContextManifest(BaseModel):
    """Context requirements block."""

    required: list[ContextField] = Field(default_factory=list)
    optional: list[ContextField] = Field(default_factory=list)
    data_retention_days: int = Field(default=0, ge=0)
    data_sharing: Literal["none", "anonymized", "third_party"] = "none"


class RateLimitManifest(BaseModel):
    """Rate-limit block."""

    rpm: int | None = Field(default=None, ge=0)
    rpd: int | None = Field(default=None, ge=0)


class OperationsManifest(BaseModel):
    """Operational metadata block."""

    uptime_sla_percent: float | None = Field(default=None, ge=0, le=100)
    rate_limits: RateLimitManifest = Field(default_factory=RateLimitManifest)
    sandbox_url: HttpUrl | None = None


class ServiceManifest(BaseModel):
    """Top-level manifest payload."""

    manifest_version: Literal["1.0"]
    service_id: UUID
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(max_length=253)
    public_key: str | None = None
    capabilities: list[CapabilityManifest]
    pricing: PricingManifest
    context: ContextManifest
    operations: OperationsManifest
    legal_entity: str | None = Field(default=None, max_length=200)
    last_updated: datetime

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        """Strip whitespace and reject null bytes from all string fields."""
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(
                    f"null bytes are not allowed in: {', '.join(null_fields)}"
                )
            data = strip_strings_recursive(data)
        return data

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        """Require a valid hostname-like domain."""
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("domain must be a valid FQDN")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[CapabilityManifest]) -> list[CapabilityManifest]:
        """Enforce cardinality and unique ontology tags."""
        if not 1 <= len(value) <= 50:
            raise ValueError("capabilities must contain between 1 and 50 items")

        counts = Counter(capability.ontology_tag for capability in value)
        duplicates = sorted(tag for tag, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(
                f"capabilities contain duplicate ontology_tag values: {', '.join(duplicates)}"
            )

        return value
