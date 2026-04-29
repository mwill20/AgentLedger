"""Layer 6 liability models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from api.models.sanitize import check_null_bytes_recursive, strip_strings_recursive


ClaimType = Literal[
    "service_failure",
    "data_misuse",
    "wrong_outcome",
    "unauthorized_action",
    "workflow_design_flaw",
]

ComplianceExportType = Literal["eu_ai_act", "hipaa", "sec", "full"]


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
            return strip_strings_recursive(data)
        return data


def _validate_did(value: str) -> str:
    """Require DID-shaped identifiers in public liability requests."""
    if not value.startswith("did:"):
        raise ValueError("DID values must start with did:")
    return value


class SnapshotStepTrustState(BaseModel):
    """Point-in-time trust state for one workflow step."""

    step_number: int
    ontology_tag: str
    service_id: UUID | None = None
    service_name: str | None = None
    min_trust_tier: int
    min_trust_score: float
    trust_score: float | None = None
    trust_tier: int | None = None
    trust_score_source: str


class SnapshotContextSummary(BaseModel):
    """Context disclosure summary captured with a liability snapshot."""

    fields_disclosed: list[str] = Field(default_factory=list)
    fields_withheld: list[str] = Field(default_factory=list)
    fields_committed: list[str] = Field(default_factory=list)
    mismatch_count: int = 0


class LiabilitySnapshotRecord(BaseModel):
    """Full liability snapshot returned by the API."""

    snapshot_id: UUID
    execution_id: UUID
    workflow_id: UUID
    agent_did: str
    captured_at: datetime
    workflow_quality_score: float
    workflow_author_did: str
    workflow_validator_did: str | None = None
    workflow_validation_checklist: dict[str, Any] | None = None
    step_trust_states: list[SnapshotStepTrustState] = Field(default_factory=list)
    context_summary: SnapshotContextSummary
    critical_mismatch_count: int
    agent_profile_default_policy: str | None = None
    created_at: datetime


class LiabilitySnapshotSummary(BaseModel):
    """List-row snapshot summary."""

    snapshot_id: UUID
    execution_id: UUID
    workflow_id: UUID
    agent_did: str
    workflow_quality_score: float
    critical_mismatch_count: int
    captured_at: datetime
    created_at: datetime


class LiabilitySnapshotListResponse(BaseModel):
    """Paginated snapshot list response."""

    total: int
    limit: int
    offset: int
    snapshots: list[LiabilitySnapshotSummary] = Field(default_factory=list)


class ClaimCreateRequest(_SanitizedModel):
    """Request payload for filing a liability claim."""

    execution_id: UUID
    claimant_did: str = Field(min_length=8, max_length=500)
    claim_type: ClaimType
    description: str = Field(min_length=1, max_length=4000)
    harm_value_usd: float | None = Field(default=None, ge=0.0)

    @field_validator("claimant_did")
    @classmethod
    def validate_claimant_did(cls, value: str) -> str:
        return _validate_did(value)


class EvidenceRecord(BaseModel):
    """Evidence item attached to a liability claim."""

    evidence_id: UUID
    claim_id: UUID
    evidence_type: str
    source_table: str
    source_id: UUID
    source_layer: int
    summary: str
    raw_data: dict[str, Any] = Field(default_factory=dict)
    gathered_at: datetime
    created_at: datetime


class LiabilityDeterminationRecord(BaseModel):
    """Latest attribution determination for a claim."""

    determination_id: UUID
    claim_id: UUID
    determination_version: int
    agent_weight: float
    service_weight: float
    workflow_author_weight: float
    validator_weight: float
    agent_did: str
    service_id: UUID | None = None
    workflow_author_did: str | None = None
    validator_did: str | None = None
    attribution_factors: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float
    determined_by: str
    determined_at: datetime
    created_at: datetime


class ClaimResponse(BaseModel):
    """Claim summary returned after status transitions."""

    claim_id: UUID
    execution_id: UUID
    snapshot_id: UUID
    claimant_did: str
    claim_type: str
    description: str
    harm_value_usd: float | None = None
    status: str
    reviewer_did: str | None = None
    resolution_note: str | None = None
    filed_at: datetime
    evidence_gathered_at: datetime | None = None
    determined_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ClaimDetailResponse(ClaimResponse):
    """Full claim detail with attached evidence and latest determination."""

    evidence_count: int
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    determination: LiabilityDeterminationRecord | None = None


class EvidenceGatherResponse(BaseModel):
    """Response returned after gathering claim evidence."""

    claim_id: UUID
    evidence_count: int
    status: str


class DetermineRequest(_SanitizedModel):
    """Request payload for computing liability attribution."""

    reviewer_did: str | None = Field(default=None, min_length=8, max_length=500)

    @field_validator("reviewer_did")
    @classmethod
    def validate_reviewer_did(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_did(value)


class AttributionFactor(BaseModel):
    """One attribution factor applied by the engine."""

    factor: str
    actor: Literal["agent", "service", "workflow_author", "validator"]
    weight_contribution: float
    evidence_ids: list[UUID] = Field(default_factory=list)


class DeterminationResponse(BaseModel):
    """Response returned by POST /liability/claims/{id}/determine."""

    determination_id: UUID
    claim_id: UUID
    determination_version: int
    attribution: dict[str, float]
    applied_factors: list[AttributionFactor] = Field(default_factory=list)
    confidence: float
    determined_by: Literal["system", "reviewer"]
    determined_at: datetime


class ComplianceExportParams(_SanitizedModel):
    """Resolved query parameters for a liability compliance export."""

    export_type: ComplianceExportType
    agent_did: str | None = Field(default=None, min_length=8, max_length=500)
    execution_id: UUID | None = None
    claim_id: UUID | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_did(value)


class ResolveRequest(_SanitizedModel):
    """Request payload for resolving a determined claim."""

    resolution_note: str = Field(min_length=1, max_length=4000)
    reviewer_did: str = Field(min_length=8, max_length=500)

    @field_validator("reviewer_did")
    @classmethod
    def validate_reviewer_did(cls, value: str) -> str:
        return _validate_did(value)


class AppealRequest(_SanitizedModel):
    """Request payload for appealing a determined claim."""

    appeal_reason: str = Field(min_length=1, max_length=4000)
    claimant_did: str = Field(min_length=8, max_length=500)

    @field_validator("claimant_did")
    @classmethod
    def validate_claimant_did(cls, value: str) -> str:
        return _validate_did(value)
