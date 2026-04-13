"""Layer 2 identity request and response models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

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


class IdentityProof(BaseModel):
    """Detached proof of possession for registration or challenge signing."""

    nonce: str = Field(min_length=8, max_length=512)
    created_at: datetime
    signature: str = Field(min_length=16, max_length=2048)


class AgentRegistrationRequest(BaseModel):
    """Request payload for agent identity registration."""

    did: str = Field(min_length=10, max_length=500)
    did_document: dict[str, Any]
    agent_name: str = Field(min_length=1, max_length=200)
    issuing_platform: str | None = Field(default=None, max_length=100)
    capability_scope: list[str] = Field(default_factory=list)
    risk_tier: Literal["standard", "elevated", "restricted"] = "standard"
    proof: IdentityProof

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        """Strip whitespace and reject null bytes."""
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(
                    f"null bytes are not allowed in: {', '.join(null_fields)}"
                )
            data = strip_strings_recursive(data)
        return data

    @field_validator("did")
    @classmethod
    def validate_did(cls, value: str) -> str:
        """Restrict agent registration to did:key for v0.1."""
        if not value.startswith("did:key:"):
            raise ValueError("agent DID must use did:key")
        return value

    @field_validator("capability_scope")
    @classmethod
    def validate_capability_scope(cls, value: list[str]) -> list[str]:
        """Validate ontology scopes and prefixes."""
        invalid = [scope for scope in value if not _is_valid_scope(scope)]
        if invalid:
            raise ValueError(
                f"invalid capability_scope values: {', '.join(sorted(invalid))}"
            )
        return value


class AgentRegistrationResponse(BaseModel):
    """Response payload for successful agent registration."""

    did: str
    credential_jwt: str
    credential_expires_at: datetime
    did_document: dict[str, Any]
    issuer_did: str


class AgentCredentialPrincipal(BaseModel):
    """Authenticated agent principal derived from a bearer VC."""

    did: str
    capability_scope: list[str] = Field(default_factory=list)
    risk_tier: str
    public_key_jwk: dict[str, Any]
    credential_claims: dict[str, Any]
    credential_expires_at: datetime | None = None


class CredentialVerificationRequest(BaseModel):
    """Request payload for online credential verification."""

    credential_jwt: str = Field(min_length=32)


class CredentialVerificationResponse(BaseModel):
    """Result of an online credential verification."""

    valid: bool
    did: str | None = None
    expires_at: datetime | None = None
    is_revoked: bool = False
    capability_scope: list[str] = Field(default_factory=list)
    risk_tier: str | None = None


class AgentIdentityResponse(BaseModel):
    """Public agent identity record."""

    did: str
    did_document: dict[str, Any]
    agent_name: str
    issuing_platform: str | None = None
    capability_scope: list[str] = Field(default_factory=list)
    risk_tier: str
    is_active: bool
    is_revoked: bool
    registered_at: datetime
    last_seen_at: datetime | None = None
    credential_expires_at: datetime | None = None


class AgentRevokeRequest(BaseModel):
    """Admin request to revoke an agent identity."""

    reason_code: str = Field(min_length=3, max_length=100)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        """Strip whitespace and reject null bytes."""
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(
                    f"null bytes are not allowed in: {', '.join(null_fields)}"
                )
            data = strip_strings_recursive(data)
        return data


class AgentRevokeResponse(BaseModel):
    """Response payload for a revocation action."""

    did: str
    revoked_at: datetime
    reason_code: str


class SessionRequest(BaseModel):
    """Request payload for a session assertion."""

    service_domain: str = Field(max_length=253)
    ontology_tag: str = Field(pattern=r"^[a-z]+\.[a-z]+\.[a-z]+$")
    request_context: dict[str, Any] = Field(default_factory=dict)
    proof: IdentityProof

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        """Strip whitespace and reject null bytes."""
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(
                    f"null bytes are not allowed in: {', '.join(null_fields)}"
                )
            data = strip_strings_recursive(data)
        return data

    @field_validator("service_domain")
    @classmethod
    def validate_service_domain(cls, value: str) -> str:
        """Require a valid FQDN-like service domain."""
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("service_domain must be a valid FQDN")
        return normalized


class SessionStatusResponse(BaseModel):
    """Status response for session issuance or polling."""

    status: Literal["issued", "pending_approval", "denied", "expired"]
    session_id: str | None = None
    assertion_jwt: str | None = None
    service_did: str | None = None
    authorization_request_id: str | None = None
    expires_at: datetime


class SessionRedeemRequest(BaseModel):
    """Request payload for redeeming a session assertion."""

    assertion_jwt: str = Field(min_length=32)
    service_domain: str = Field(max_length=253)

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        """Strip whitespace and reject null bytes."""
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(
                    f"null bytes are not allowed in: {', '.join(null_fields)}"
                )
            data = strip_strings_recursive(data)
        return data

    @field_validator("service_domain")
    @classmethod
    def validate_service_domain(cls, value: str) -> str:
        """Require a valid FQDN-like service domain."""
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("service_domain must be a valid FQDN")
        return normalized


class SessionRedeemResponse(BaseModel):
    """Response payload for successful session redemption."""

    status: Literal["accepted"]
    agent_did: str
    ontology_tag: str
    authorization_ref: str | None = None
