"""Layer 5 workflow registry models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from api.models.sanitize import check_null_bytes_recursive, strip_strings_recursive

_CONTEXT_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_DOMAIN_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*$")
_ONTOLOGY_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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
    """Validate context field names such as user.name or user.email."""
    invalid = [value for value in values if not _CONTEXT_FIELD_RE.fullmatch(value)]
    if invalid:
        raise ValueError(f"invalid context field names: {', '.join(sorted(invalid))}")
    return values


class WorkflowStepInput(_SanitizedModel):
    """One submitted workflow step."""

    step_number: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    ontology_tag: str = Field(min_length=3, max_length=200)
    service_id: UUID | None = None
    is_required: bool = True
    fallback_step_number: int | None = Field(default=None, ge=1)
    context_fields_required: list[str] = Field(default_factory=list)
    context_fields_optional: list[str] = Field(default_factory=list)
    min_trust_tier: int = Field(default=2, ge=1, le=4)
    min_trust_score: float = Field(default=50.0, ge=0.0, le=100.0)
    timeout_seconds: int = Field(default=30, ge=1, le=3600)

    @field_validator("ontology_tag")
    @classmethod
    def validate_ontology_tag(cls, value: str) -> str:
        if not _ONTOLOGY_TAG_RE.fullmatch(value):
            raise ValueError("ontology_tag must be dot-separated lowercase text")
        return value

    @field_validator("context_fields_required", "context_fields_optional")
    @classmethod
    def validate_context_fields(cls, value: list[str]) -> list[str]:
        return _validate_context_fields(value)


class WorkflowContextBundleSpec(_SanitizedModel):
    """Context aggregation metadata included in submitted workflow specs."""

    all_required_fields: list[str] = Field(default_factory=list)
    all_optional_fields: list[str] = Field(default_factory=list)
    single_approval: bool = True

    @field_validator("all_required_fields", "all_optional_fields")
    @classmethod
    def validate_context_fields(cls, value: list[str]) -> list[str]:
        return _validate_context_fields(value)


class WorkflowQualitySpec(_SanitizedModel):
    """Optional quality metadata in submitted workflow specs."""

    quality_score: float = Field(default=0.0, ge=0.0, le=100.0)
    execution_count: int = Field(default=0, ge=0)
    success_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    validation_status: str = "draft"
    validated_by_domain: str | None = None


class WorkflowAccountabilitySpec(_SanitizedModel):
    """Accountability metadata in submitted workflow specs."""

    author_did: str = Field(min_length=8, max_length=500)
    published_at: datetime | None = None
    spec_hash: str | None = None

    @field_validator("author_did")
    @classmethod
    def validate_author_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("author_did must start with did:")
        return value


class WorkflowCreateRequest(_SanitizedModel):
    """Full submitted workflow spec."""

    spec_version: Literal["1.0"]
    workflow_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    ontology_domain: str = Field(min_length=2, max_length=100)
    tags: list[str] = Field(min_length=1)
    steps: list[WorkflowStepInput] = Field(min_length=1, max_length=20)
    context_bundle: WorkflowContextBundleSpec | None = None
    quality: WorkflowQualitySpec | None = None
    accountability: WorkflowAccountabilitySpec

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not _SLUG_RE.fullmatch(value):
            raise ValueError("slug must use lowercase letters, numbers, and hyphens")
        return value

    @field_validator("ontology_domain")
    @classmethod
    def validate_ontology_domain(cls, value: str) -> str:
        value = value.upper()
        if not _DOMAIN_RE.fullmatch(value):
            raise ValueError("ontology_domain must be an uppercase ontology domain")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        invalid = [tag for tag in value if not _ONTOLOGY_TAG_RE.fullmatch(tag)]
        if invalid:
            raise ValueError(f"invalid ontology tags: {', '.join(sorted(invalid))}")
        return value

    @model_validator(mode="after")
    def validate_step_graph(self) -> "WorkflowCreateRequest":
        expected = list(range(1, len(self.steps) + 1))
        actual = [step.step_number for step in self.steps]
        if actual != expected:
            raise ValueError("step_number values must be sequential starting at 1")

        max_step = len(self.steps)
        for step in self.steps:
            if step.fallback_step_number is None:
                continue
            if step.fallback_step_number <= step.step_number:
                raise ValueError("fallback_step_number must reference a later step")
            if step.fallback_step_number > max_step:
                raise ValueError("fallback_step_number must reference an existing step")

        seen: set[tuple[str, str | None]] = set()
        for step in self.steps:
            key = (
                step.ontology_tag,
                str(step.service_id) if step.service_id is not None else None,
            )
            if key in seen:
                raise ValueError(
                    "duplicate ontology_tag entries require different service_id values"
                )
            seen.add(key)
        return self


class WorkflowCreateResponse(BaseModel):
    """Response returned after a workflow is submitted."""

    workflow_id: UUID
    slug: str
    status: Literal["draft"]
    validation_id: UUID
    estimated_review_hours: int = 48


class WorkflowStepRecord(BaseModel):
    """Stored workflow step returned by read endpoints."""

    step_id: UUID
    step_number: int
    name: str
    ontology_tag: str
    service_id: UUID | None = None
    is_required: bool
    fallback_step_number: int | None = None
    context_fields_required: list[str] = Field(default_factory=list)
    context_fields_optional: list[str] = Field(default_factory=list)
    min_trust_tier: int
    min_trust_score: float
    timeout_seconds: int
    created_at: datetime


class WorkflowRecord(BaseModel):
    """Full workflow detail returned by read endpoints."""

    workflow_id: UUID
    name: str
    slug: str
    description: str
    ontology_domain: str
    tags: list[str] = Field(default_factory=list)
    spec: dict[str, Any]
    spec_version: str
    spec_hash: str | None = None
    author_did: str
    status: str
    quality_score: float
    execution_count: int
    success_count: int
    failure_count: int
    parent_workflow_id: UUID | None = None
    published_at: datetime | None = None
    deprecated_at: datetime | None = None
    steps: list[WorkflowStepRecord] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class WorkflowSummary(BaseModel):
    """Workflow list item."""

    workflow_id: UUID
    name: str
    slug: str
    description: str
    ontology_domain: str
    tags: list[str] = Field(default_factory=list)
    status: str
    quality_score: float
    execution_count: int
    step_count: int
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkflowListResponse(BaseModel):
    """Paginated workflow list response."""

    total: int
    limit: int
    offset: int
    workflows: list[WorkflowSummary] = Field(default_factory=list)


class ValidationAssignRequest(_SanitizedModel):
    """Request payload for assigning a workflow validator."""

    validator_did: str = Field(min_length=8, max_length=500)
    validator_domain: str = Field(min_length=2, max_length=100)

    @field_validator("validator_did")
    @classmethod
    def validate_validator_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("validator_did must start with did:")
        return value

    @field_validator("validator_domain")
    @classmethod
    def validate_validator_domain(cls, value: str) -> str:
        value = value.upper()
        if not _DOMAIN_RE.fullmatch(value):
            raise ValueError("validator_domain must be an uppercase ontology domain")
        return value


class ValidatorDecisionRequest(_SanitizedModel):
    """Request payload for a validator decision."""

    validator_did: str = Field(min_length=8, max_length=500)
    decision: Literal["approved", "rejected", "revision_requested"]
    checklist: dict[str, bool] = Field(default_factory=dict)
    rejection_reason: str | None = Field(default=None, max_length=2000)
    revision_notes: str | None = Field(default=None, max_length=4000)

    @field_validator("validator_did")
    @classmethod
    def validate_validator_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("validator_did must start with did:")
        return value

    @model_validator(mode="after")
    def validate_approval_checklist(self) -> "ValidatorDecisionRequest":
        if self.decision != "approved":
            return self
        required = {
            "steps_achievable",
            "context_minimal",
            "trust_thresholds_appropriate",
            "no_sensitive_tag_without_domain_review",
            "fallback_logic_sound",
        }
        missing = sorted(required - set(self.checklist))
        if missing:
            raise ValueError(
                f"approved decisions require checklist keys: {', '.join(missing)}"
            )
        failed = sorted(key for key in required if self.checklist.get(key) is not True)
        if failed:
            raise ValueError(
                f"approved decisions require passing checklist keys: {', '.join(failed)}"
            )
        return self


class ValidationResponse(BaseModel):
    """Workflow validation assignment or decision record."""

    validation_id: UUID
    workflow_id: UUID
    validator_did: str
    validator_domain: str
    assigned_at: datetime
    decision: str | None = None
    decision_at: datetime | None = None
    rejection_reason: str | None = None
    revision_notes: str | None = None
    checklist: dict[str, Any] = Field(default_factory=dict)


class ServiceCandidate(BaseModel):
    """Ranked service candidate for one workflow step."""

    service_id: UUID
    name: str
    trust_score: float
    trust_tier: int
    rank_score: float
    can_disclose: bool = True


class RankedStep(BaseModel):
    """Ranked candidate list for one workflow step."""

    step_number: int
    ontology_tag: str
    is_required: bool
    min_trust_tier: int
    min_trust_score: float
    candidates: list[ServiceCandidate] = Field(default_factory=list)


class WorkflowRankResponse(BaseModel):
    """Response returned by GET /workflows/{id}/rank."""

    workflow_id: UUID
    ranked_steps: list[RankedStep] = Field(default_factory=list)


class BundleFieldBreakdown(BaseModel):
    """Per-step workflow context field classification."""

    permitted: list[str] = Field(default_factory=list)
    withheld: list[str] = Field(default_factory=list)
    committed: list[str] = Field(default_factory=list)


class BundleCreateRequest(_SanitizedModel):
    """Request payload for creating a workflow-level context bundle."""

    workflow_id: UUID
    agent_did: str = Field(min_length=8, max_length=500)
    scoped_profile_overrides: dict[
        str,
        Literal["permit", "withhold", "deny"],
    ] = Field(default_factory=dict)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value

    @field_validator("scoped_profile_overrides")
    @classmethod
    def validate_scoped_profile_overrides(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        _validate_context_fields(list(value.keys()))
        return value


class BundleResponse(BaseModel):
    """Response returned by POST /workflows/context/bundle."""

    bundle_id: UUID
    workflow_id: UUID
    status: Literal["pending"]
    by_step: dict[str, BundleFieldBreakdown] = Field(default_factory=dict)
    all_permitted: list[str] = Field(default_factory=list)
    all_committed: list[str] = Field(default_factory=list)
    all_withheld: list[str] = Field(default_factory=list)
    expires_at: datetime


class BundleApproveRequest(_SanitizedModel):
    """Request payload for approving a context bundle."""

    agent_did: str = Field(min_length=8, max_length=500)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value


class BundleApproveResponse(BaseModel):
    """Response returned after a context bundle is approved."""

    bundle_id: UUID
    status: Literal["approved"]
    approved_at: datetime


class ExecutionReportRequest(_SanitizedModel):
    """Request payload for reporting a workflow execution outcome."""

    agent_did: str = Field(min_length=8, max_length=500)
    context_bundle_id: UUID | None = None
    outcome: Literal["success", "failure"]
    steps_completed: int = Field(ge=0)
    steps_total: int = Field(ge=1)
    failure_step_number: int | None = Field(default=None, ge=1)
    failure_reason: str | None = Field(default=None, max_length=2000)
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("agent_did")
    @classmethod
    def validate_agent_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("agent_did must start with did:")
        return value


class ExecutionReportResponse(BaseModel):
    """Response returned after reporting a workflow execution."""

    execution_id: UUID
    verified: bool
    quality_score: float
