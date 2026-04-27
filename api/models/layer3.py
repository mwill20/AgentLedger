"""Layer 3 trust, audit, federation, and chain models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from api.models.sanitize import check_null_bytes_recursive, strip_strings_recursive

_FQDN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def _is_valid_scope(value: str) -> bool:
    """Return whether a scope string is an exact tag or a supported prefix."""
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        return False
    for index, part in enumerate(parts):
        if not part:
            return False
        if part == "*":
            return index == len(parts) - 1
        if not part.replace("_", "").isalnum() or not part.islower():
            return False
    return True


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


class AuditorRegistrationRequest(_SanitizedModel):
    """Request payload for registering a Layer 3 auditor."""

    did: str = Field(min_length=10, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    ontology_scope: list[str] = Field(min_length=1)
    accreditation_refs: list[dict[str, Any]] = Field(default_factory=list)
    chain_address: str | None = Field(default=None, max_length=128)

    @field_validator("did")
    @classmethod
    def validate_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("auditor DID must start with did:")
        return value

    @field_validator("ontology_scope")
    @classmethod
    def validate_ontology_scope(cls, value: list[str]) -> list[str]:
        invalid = [scope for scope in value if not _is_valid_scope(scope)]
        if invalid:
            raise ValueError(
                f"invalid ontology_scope values: {', '.join(sorted(invalid))}"
            )
        return value

    @field_validator("chain_address")
    @classmethod
    def validate_chain_address(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.lower()
        if not re.fullmatch(r"0x[a-f0-9]{40}", normalized):
            raise ValueError("chain_address must be a 0x-prefixed 40-byte hex address")
        return normalized


class AuditorRegistrationResponse(BaseModel):
    """Registration result for one auditor."""

    application_id: UUID
    status: Literal["active"]


class AuditorRecord(BaseModel):
    """Public auditor record."""

    did: str
    name: str
    ontology_scope: list[str] = Field(default_factory=list)
    accreditation_refs: list[dict[str, Any]] = Field(default_factory=list)
    chain_address: str | None = None
    is_active: bool
    approved_at: datetime | None = None
    credential_expires_at: datetime | None = None


class AttestationCreateRequest(_SanitizedModel):
    """Request payload for recording a service attestation."""

    auditor_did: str = Field(min_length=10, max_length=500)
    service_domain: str = Field(max_length=253)
    ontology_scope: str = Field(min_length=1, max_length=120)
    certification_ref: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    evidence_package: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service_domain")
    @classmethod
    def validate_service_domain(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("service_domain must be a valid FQDN")
        return normalized

    @field_validator("auditor_did")
    @classmethod
    def validate_auditor_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("auditor_did must start with did:")
        return value

    @field_validator("ontology_scope")
    @classmethod
    def validate_ontology_scope(cls, value: str) -> str:
        if not _is_valid_scope(value):
            raise ValueError("ontology_scope must be a valid tag or supported prefix")
        return value


class AttestationCreateResponse(BaseModel):
    """Result of creating one attestation."""

    attestation_id: UUID
    tx_hash: str
    block_number: int


class RevocationCreateRequest(_SanitizedModel):
    """Request payload for recording a service revocation."""

    auditor_did: str = Field(min_length=10, max_length=500)
    service_domain: str = Field(max_length=253)
    reason_code: str = Field(min_length=3, max_length=100)
    evidence_package: dict[str, Any] = Field(default_factory=dict)

    @field_validator("service_domain")
    @classmethod
    def validate_service_domain(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("service_domain must be a valid FQDN")
        return normalized

    @field_validator("auditor_did")
    @classmethod
    def validate_auditor_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("auditor_did must start with did:")
        return value


class RevocationCreateResponse(BaseModel):
    """Result of recording one service revocation."""

    revocation_id: UUID
    tx_hash: str
    block_number: int


class AttestationRecord(BaseModel):
    """One attestation listed for a service."""

    attestation_id: UUID
    auditor: AuditorRecord
    scope: str
    certification_ref: str | None = None
    expires_at: datetime | None = None
    tx_hash: str
    is_confirmed: bool
    recorded_at: datetime


class AttestationVerifyResponse(BaseModel):
    """Verification result for a service's attestation state."""

    on_chain_matches_db: bool
    attestation_count: int
    trust_tier_eligible: bool


class AuditRecordCreateRequest(_SanitizedModel):
    """Request payload for creating a Layer 3 audit record."""

    session_assertion_id: UUID
    ontology_tag: str = Field(pattern=r"^[a-z]+\.[a-z]+\.[a-z]+$")
    action_context: dict[str, Any] = Field(default_factory=dict)
    outcome: Literal["success", "failure", "timeout", "rejected"]
    outcome_details: dict[str, Any] = Field(default_factory=dict)


class AuditRecordCreateResponse(BaseModel):
    """Result of creating one audit record."""

    record_id: UUID
    record_hash: str
    status: Literal["pending_anchor"]


class AuditRecordView(BaseModel):
    """Public view of a stored audit record."""

    id: UUID
    agent_did: str
    service_id: UUID
    ontology_tag: str
    session_assertion_id: UUID | None = None
    action_context: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    outcome_details: dict[str, Any] = Field(default_factory=dict)
    record_hash: str
    batch_id: UUID | None = None
    merkle_proof: list[dict[str, str]] = Field(default_factory=list)
    tx_hash: str | None = None
    block_number: int | None = None
    is_anchored: bool
    anchored_at: datetime | None = None
    created_at: datetime


class AuditRecordDetailResponse(BaseModel):
    """Detailed response for one audit record."""

    record: AuditRecordView
    record_hash: str
    batch_id: UUID | None = None
    tx_hash: str | None = None
    is_anchored: bool


class AuditRecordVerifyResponse(BaseModel):
    """Verification result for a stored audit record."""

    integrity_valid: bool
    merkle_proof_valid: bool
    tx_hash: str | None = None
    block_number: int | None = None


class AuditRecordListResponse(BaseModel):
    """Paginated audit record query results."""

    total: int
    limit: int
    offset: int
    records: list[AuditRecordView] = Field(default_factory=list)


class FederationRegistrySubscribeRequest(_SanitizedModel):
    """Request payload for registering a federated registry subscriber."""

    name: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=8, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=500)
    public_key_pem: str = Field(min_length=32, max_length=10000)


class FederationRegistrySubscribeResponse(BaseModel):
    """Result of subscribing a federated registry."""

    subscriber_id: UUID
    status: Literal["active"]


class FederationRevocationSubmitRequest(_SanitizedModel):
    """Incoming federated revocation submission."""

    domain: str = Field(max_length=253)
    reason_code: str = Field(min_length=3, max_length=100)
    evidence_url: str = Field(min_length=8, max_length=500)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("domain must be a valid FQDN")
        return normalized


class FederationRevocationSubmitResponse(BaseModel):
    """Accepted federated revocation result."""

    submission_id: UUID
    status: Literal["pending_review"]


class FederationBlocklistEntry(BaseModel):
    """One confirmed revocation entry in the shared blocklist."""

    domain: str
    reason: str
    revoked_at: datetime
    tx_hash: str


class FederationBlocklistResponse(BaseModel):
    """Blocklist query result."""

    revocations: list[FederationBlocklistEntry] = Field(default_factory=list)
    total: int
    next_page: int | None = None


class ChainStatusResponse(BaseModel):
    """Current chain connectivity and contract metadata."""

    chain_id: int
    network: str
    latest_block: int
    contracts: dict[str, str]
    tracked_tx_hash: str | None = None
    tracked_block_number: int | None = None
    confirmation_depth: int | None = None
    is_confirmed: bool | None = None


class ChainEventRecord(BaseModel):
    """Indexed Layer 3 chain event."""

    id: UUID
    event_type: str
    service_id: UUID | None = None
    tx_hash: str
    block_number: int
    chain_id: int
    is_confirmed: bool
    event_data: dict[str, Any] = Field(default_factory=dict)
    indexed_at: datetime
    confirmed_at: datetime | None = None


class ChainEventsResponse(BaseModel):
    """Paginated chain events result."""

    events: list[ChainEventRecord] = Field(default_factory=list)
    total: int
