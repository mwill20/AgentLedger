"""Query and mutation response models."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from api.models.sanitize import check_null_bytes_recursive, strip_strings_recursive


class ManifestRegistrationResponse(BaseModel):
    """POST /manifests response."""

    service_id: UUID
    trust_tier: int
    trust_score: float
    status: Literal["registered", "updated", "pending_review"]
    capabilities_indexed: int
    typosquat_warnings: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """POST /search request."""

    query: str = Field(max_length=500)
    trust_min: float = Field(default=0, ge=0, le=100)
    geo: str | None = Field(default=None, max_length=10)
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

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
