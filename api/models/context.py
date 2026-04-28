"""Layer 4 context profile models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from api.models.sanitize import check_null_bytes_recursive, strip_strings_recursive

_CONTEXT_FIELD_RE = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
)
_DOMAIN_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*$")


class _SanitizedModel(BaseModel):
    """Base model that strips strings and rejects null bytes."""

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(
                    f"null bytes are not allowed in: {', '.join(null_fields)}"
                )
            data = strip_strings_recursive(data)
        return data


def _validate_context_fields(values: list[str]) -> list[str]:
    """Validate profile field names such as user.name or user.email."""
    invalid = [value for value in values if not _CONTEXT_FIELD_RE.fullmatch(value)]
    if invalid:
        raise ValueError(f"invalid context field names: {', '.join(sorted(invalid))}")
    return values


class ContextProfileRuleInput(_SanitizedModel):
    """One context-sharing rule in a profile write request."""

    priority: int = Field(default=100, ge=0)
    scope_type: Literal["domain", "trust_tier", "service_did", "sensitivity"]
    scope_value: str = Field(min_length=1, max_length=500)
    permitted_fields: list[str] = Field(default_factory=list)
    denied_fields: list[str] = Field(default_factory=list)
    action: Literal["permit", "deny"] = "permit"

    @field_validator("permitted_fields", "denied_fields")
    @classmethod
    def validate_field_names(cls, value: list[str]) -> list[str]:
        return _validate_context_fields(value)

    @model_validator(mode="after")
    def validate_scope_value(self) -> "ContextProfileRuleInput":
        if self.scope_type == "domain":
            self.scope_value = self.scope_value.upper()
            if not _DOMAIN_RE.fullmatch(self.scope_value):
                raise ValueError("domain scope_value must be an uppercase ontology domain")
        elif self.scope_type in {"trust_tier", "sensitivity"}:
            if not self.scope_value.isdigit() or not 1 <= int(self.scope_value) <= 4:
                raise ValueError(f"{self.scope_type} scope_value must be an integer 1-4")
        elif self.scope_type == "service_did":
            if not self.scope_value.startswith("did:web:"):
                raise ValueError("service_did scope_value must start with did:web:")
        return self


class ContextProfileCreateRequest(_SanitizedModel):
    """Request payload for creating a context profile."""

    agent_did: str = Field(min_length=8, max_length=500)
    profile_name: str = Field(default="default", min_length=1, max_length=100)
    default_policy: Literal["deny", "allow"] = "deny"
    rules: list[ContextProfileRuleInput] = Field(default_factory=list)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value


class ContextProfileUpdateRequest(_SanitizedModel):
    """Request payload for replacing an active profile's rules."""

    profile_name: str = Field(default="default", min_length=1, max_length=100)
    default_policy: Literal["deny", "allow"] = "deny"
    rules: list[ContextProfileRuleInput] = Field(default_factory=list)


class ContextProfileCreateResponse(BaseModel):
    """Response returned after creating a profile."""

    profile_id: UUID
    agent_did: str
    profile_name: str
    default_policy: Literal["deny", "allow"]
    rule_count: int
    created_at: datetime


class ContextProfileRuleRecord(BaseModel):
    """Stored profile rule returned in read responses."""

    rule_id: UUID
    priority: int
    scope_type: str
    scope_value: str
    permitted_fields: list[str] = Field(default_factory=list)
    denied_fields: list[str] = Field(default_factory=list)
    action: str
    created_at: datetime


class ContextProfileRecord(BaseModel):
    """Full stored context profile with rules sorted by priority."""

    profile_id: UUID
    agent_did: str
    profile_name: str
    is_active: bool
    default_policy: Literal["deny", "allow"]
    rules: list[ContextProfileRuleRecord] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ContextMatchRequest(_SanitizedModel):
    """Request payload for context matching."""

    agent_did: str = Field(min_length=8, max_length=500)
    service_id: UUID
    session_assertion: str = Field(min_length=16, max_length=10000)
    requested_fields: list[str] = Field(min_length=1)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value

    @field_validator("requested_fields")
    @classmethod
    def validate_requested_fields(cls, value: list[str]) -> list[str]:
        return _validate_context_fields(value)


class ContextMismatchRecord(BaseModel):
    """Stored mismatch event."""

    id: UUID
    service_id: UUID
    agent_did: str
    declared_fields: list[str] = Field(default_factory=list)
    requested_fields: list[str] = Field(default_factory=list)
    over_requested_fields: list[str] = Field(default_factory=list)
    severity: Literal["warning", "critical"]
    resolved: bool
    resolution_note: str | None = None
    created_at: datetime


class ContextMismatchListResponse(BaseModel):
    """Paginated mismatch event list."""

    total: int
    limit: int
    offset: int
    events: list[ContextMismatchRecord] = Field(default_factory=list)


class ContextMismatchResolveRequest(_SanitizedModel):
    """Request payload for resolving a mismatch event."""

    resolution_note: str = Field(min_length=1, max_length=2000)
    escalate_to_trust: bool = False


class ContextMismatchResolveResponse(BaseModel):
    """Response returned after resolving a mismatch event."""

    mismatch_id: UUID
    resolved: bool
    resolution_note: str
    escalated_to_trust: bool
    revocation_id: UUID | None = None
    tx_hash: str | None = None


class ContextMatchResponse(BaseModel):
    """Result of a successful context match."""

    match_id: UUID
    session_assertion_id: UUID | None = None
    permitted_fields: list[str] = Field(default_factory=list)
    withheld_fields: list[str] = Field(default_factory=list)
    committed_fields: list[str] = Field(default_factory=list)
    commitment_ids: list[UUID] = Field(default_factory=list)
    mismatch_detected: bool = False
    trust_tier_at_match: int
    trust_score_at_match: float
    can_disclose: bool


class DisclosureRequest(_SanitizedModel):
    """Request payload for releasing committed-field nonces."""

    match_id: UUID
    agent_did: str = Field(min_length=8, max_length=500)
    service_id: UUID
    commitment_ids: list[UUID] = Field(default_factory=list)
    field_values: dict[str, str] = Field(default_factory=dict)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value

    @field_validator("field_values")
    @classmethod
    def validate_field_value_keys(cls, value: dict[str, str]) -> dict[str, str]:
        _validate_context_fields(list(value.keys()))
        return value


class DisclosurePackage(BaseModel):
    """Nonce release package returned to the agent."""

    disclosure_id: UUID
    permitted_fields: dict[str, str] = Field(default_factory=dict)
    committed_field_nonces: dict[str, str] = Field(default_factory=dict)
    disclosed_at: datetime
    expires_at: datetime


class DisclosureRecord(BaseModel):
    """Field-name-only disclosure audit record."""

    disclosure_id: UUID
    agent_did: str
    service_id: UUID
    ontology_tag: str
    fields_requested: list[str] | None = None
    fields_disclosed: list[str] | None = None
    fields_withheld: list[str] | None = None
    fields_committed: list[str] | None = None
    disclosure_method: str
    trust_score_at_disclosure: float | None = None
    trust_tier_at_disclosure: int | None = None
    erased: bool
    erased_at: datetime | None = None
    created_at: datetime


class DisclosureListResponse(BaseModel):
    """Paginated disclosure audit response."""

    total: int
    limit: int
    offset: int
    disclosures: list[DisclosureRecord] = Field(default_factory=list)


class DisclosureRevokeRequest(_SanitizedModel):
    """Request payload for GDPR-style disclosure erasure."""

    agent_did: str = Field(min_length=8, max_length=500)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value


class DisclosureRevokeResponse(BaseModel):
    """Response returned after a disclosure is marked erased."""

    disclosure_id: UUID
    erased_at: datetime
