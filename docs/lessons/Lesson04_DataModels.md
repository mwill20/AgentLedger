# Lesson 04: The Blueprints -- Pydantic Data Models and Input Sanitization

## Welcome Back, Systems Engineer!

What happens when an AI agent sends a malformed manifest to AgentLedger? Does the database crash? Does a SQL injection slip through? Today we're exploring the **Pydantic data models** that form an impenetrable validation layer between the outside world and your business logic.

**Goal:** Understand how every request is validated, sanitized, and typed before it touches a database query.
**Time:** 60 minutes
**Prerequisites:** Lessons 01-03
**Why this matters:** Input validation bugs cause security vulnerabilities. Pydantic catches them at the boundary, before they propagate.

---

## Learning Objectives

- Explain the role of each model file in `api/models/` and what data flows through it
- Trace how `ServiceManifest` validates a full manifest registration payload
- Understand recursive null-byte detection and whitespace stripping
- Describe the difference between request models and response models
- Explain why `ContextField` uses `extra = "allow"`
- Identify the FQDN regex and why domain validation matters

---

## File Map

```
api/models/
|-- __init__.py
|-- manifest.py      # REQUEST models -- what comes IN from agents
|-- query.py         # REQUEST model (SearchRequest) + mutation response
|-- service.py       # RESPONSE models -- what goes OUT to agents
|-- sanitize.py      # Shared sanitization utilities (used by manifest + query)
```

The split is deliberate: **request models** have validators and sanitizers (they don't trust input). **Response models** are plain data containers (the data is already clean because it came from our database).

---

## Code Walkthrough: `api/models/sanitize.py`

This is the smallest file (49 lines) but arguably the most security-critical.

```python
# api/models/sanitize.py -- The entire file

def contains_null_bytes(value: str) -> bool:
    """Check if a string contains null bytes."""
    return "\x00" in value


def strip_strings_recursive(data: Any) -> Any:
    """Recursively strip whitespace from all string values."""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        return {k: strip_strings_recursive(v) for k, v in data.items()}
    if isinstance(data, list):
        return [strip_strings_recursive(item) for item in data]
    return data


def check_null_bytes_recursive(data: Any, path: str = "") -> list[str]:
    """Find all string fields containing null bytes."""
    violations: list[str] = []
    if isinstance(data, str):
        if contains_null_bytes(data):
            violations.append(path or "value")
    elif isinstance(data, dict):
        for key, val in data.items():
            child_path = f"{path}.{key}" if path else key
            violations.extend(check_null_bytes_recursive(val, child_path))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            child_path = f"{path}[{i}]"
            violations.extend(check_null_bytes_recursive(item, child_path))
    return violations
```

Line-by-Line:

1. **`contains_null_bytes`** -- Null bytes (`\x00`) are used in SQL injection, log injection, and path traversal attacks. PostgreSQL TEXT columns reject null bytes anyway, but catching them at the model layer produces a clear error instead of a cryptic database exception.

2. **`strip_strings_recursive`** -- Recursively traverses dicts and lists, stripping leading/trailing whitespace from every string. This prevents subtle bugs where `"travel.air.book "` (trailing space) fails to match `"travel.air.book"` in the ontology.

3. **`check_null_bytes_recursive`** -- Returns dotted paths (e.g., `capabilities[0].description`) for every field containing null bytes. This gives the caller a precise error message instead of just "bad input."

**Insight:**
The recursive approach handles arbitrarily nested JSON. A manifest's `capabilities[2].description` is 3 levels deep, and `context.required[0].name` is 4 levels deep. A flat check would miss these.

---

## Code Walkthrough: `api/models/manifest.py`

This is the most complex model file (136 lines, 7 model classes). It validates the entire `POST /manifests` payload.

### The FQDN Regex

```python
_FQDN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
```

This regex validates fully qualified domain names per RFC 1035:
- `(?=.{1,253}$)` -- Total length 1-253 characters
- `(?!-)` -- Cannot start with a hyphen
- `[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?` -- Each label: starts/ends with alphanumeric, up to 63 chars
- `[a-z]{2,63}$` -- TLD must be alphabetic, 2-63 chars

Why this matters: The `domain` field is used for DNS verification (Vector B). If we accepted `"not-a-domain!"` here, the DNS lookup in the crawler would fail with a confusing error much later. Catching it at registration time gives instant feedback.

### The Model Hierarchy

```
ServiceManifest (top-level)
  |-- capabilities: list[CapabilityManifest]    # 1-50 items
  |-- pricing: PricingManifest                  # required
  |-- context: ContextManifest                  # required
  |     |-- required: list[ContextField]
  |     `-- optional: list[ContextField]
  |-- operations: OperationsManifest
  |     `-- rate_limits: RateLimitManifest
  |-- domain: str                               # FQDN-validated
  |-- service_id: UUID                          # pre-assigned by agent
  `-- manifest_version: Literal["1.0"]          # must be exactly "1.0"
```

### CapabilityManifest -- Declared Service Capabilities

```python
class CapabilityManifest(BaseModel):
    id: str = Field(max_length=200)
    ontology_tag: str = Field(pattern=r"^[a-z]+\.[a-z]+\.[a-z]+$")
    description: str = Field(min_length=20, max_length=2000)
    input_schema_url: HttpUrl | None = None
    output_schema_url: HttpUrl | None = None
```

Key constraints:
- `ontology_tag` must match the three-part pattern `domain.function.action` (e.g., `travel.air.book`). The regex `^[a-z]+\.[a-z]+\.[a-z]+$` enforces lowercase, no spaces, exactly three segments.
- `description` requires 20-2000 characters. The minimum prevents empty or useless descriptions that would produce poor embeddings. The maximum prevents abuse.
- `input_schema_url` and `output_schema_url` are optional -- not all services publish JSON Schemas for their APIs.

### ContextField -- The `extra = "allow"` Pattern

```python
class ContextField(BaseModel):
    name: str | None = None
    field_name: str | None = None
    id: str | None = None
    type: str | None = None
    field_type: str | None = None
    sensitivity: Literal["low", "medium", "high", "critical"] = "low"
    description: str | None = None

    model_config = {"extra": "allow"}

    def resolved_name(self, index: int) -> str:
        return self.field_name or self.name or self.id or f"context_field_{index}"

    def resolved_type(self) -> str:
        return self.field_type or self.type or "string"
```

**Why `extra = "allow"`?** Different agent manifest formats use different field names for the same concept (`name` vs `field_name`, `type` vs `field_type`). Rather than reject valid manifests, AgentLedger accepts extra fields and uses `resolved_name()` / `resolved_type()` to normalize them. This is a deliberate trade-off: strictness vs interoperability.

**Why `resolved_name()` has a fallback chain?** The method tries `field_name`, then `name`, then `id`, and finally generates `context_field_{index}`. This means the persistence layer always gets a stable string, even from a minimal manifest that only provides `sensitivity`.

### ServiceManifest -- The Top-Level Validator

```python
class ServiceManifest(BaseModel):
    manifest_version: Literal["1.0"]
    service_id: UUID
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(max_length=253)
    # ... other fields ...

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

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _FQDN_RE.fullmatch(normalized):
            raise ValueError("domain must be a valid FQDN")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[CapabilityManifest]) -> list[CapabilityManifest]:
        if not 1 <= len(value) <= 50:
            raise ValueError("capabilities must contain between 1 and 50 items")
        counts = Counter(capability.ontology_tag for capability in value)
        duplicates = sorted(tag for tag, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(
                f"capabilities contain duplicate ontology_tag values: {', '.join(duplicates)}"
            )
        return value
```

Three layers of validation, in order:

1. **`sanitize_inputs` (model_validator, mode="before")** -- Runs BEFORE Pydantic parses the JSON into typed fields. Strips whitespace and rejects null bytes on the raw dict. This is the first line of defense.

2. **`validate_domain` (field_validator)** -- Normalizes to lowercase and validates FQDN format. Runs after parsing but before the model is constructed.

3. **`validate_capabilities` (field_validator)** -- Enforces 1-50 capabilities and rejects duplicate ontology tags. The `Counter` usage is clean: count tags, filter where count > 1, sort for deterministic error messages.

**Insight:**
`mode="before"` vs `mode="after"`: The `sanitize_inputs` validator uses `mode="before"` because it needs to operate on raw strings before Pydantic tries to parse them into `UUID`, `HttpUrl`, etc. If a `UUID` field contained `"\x00"`, a `mode="after"` validator would never see it -- Pydantic would crash first trying to parse the UUID.

---

## Code Walkthrough: `api/models/query.py`

```python
class ManifestRegistrationResponse(BaseModel):
    service_id: UUID
    trust_tier: int
    trust_score: float
    status: Literal["registered", "updated", "pending_review"]
    capabilities_indexed: int
    typosquat_warnings: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(max_length=500)
    trust_min: float = Field(default=0, ge=0, le=100)
    geo: str | None = Field(default=None, max_length=10)
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        # Same pattern as ServiceManifest
```

Two models in one file:

1. **`ManifestRegistrationResponse`** -- The response from `POST /manifests`. Note `typosquat_warnings: list[str]` -- if the registered domain looks suspiciously similar to an existing domain, warnings are included in the response (but registration still succeeds). The `status` field uses `Literal` to constrain to exactly three values.

2. **`SearchRequest`** -- The request body for `POST /search`. Defaults are carefully chosen:
   - `trust_min=0` -- No trust filter by default (show everything)
   - `limit=10, ge=1, le=100` -- Reasonable pagination with a hard upper bound
   - `offset=0, ge=0` -- No negative offsets
   - `query` max 500 chars -- Prevents abuse of the embedding model with massive inputs

The same `sanitize_inputs` pattern is applied here. Both request models use identical sanitization logic.

---

## Code Walkthrough: `api/models/service.py`

This file contains **response models only** -- no validators, no sanitizers.

### The Response Model Hierarchy

```
OntologyResponse
  `-- tags: list[OntologyTagRecord]
  `-- by_domain: dict[str, list[OntologyTagRecord]]

ServiceSearchResponse
  `-- results: list[ServiceSummary]
        `-- matched_capabilities: list[MatchedCapability]

ServiceDetail
  `-- capabilities: list[MatchedCapability]
  `-- pricing: PricingRecord | None
  `-- context_requirements: list[ContextRequirementRecord]
  `-- operations: OperationsRecord | None
```

Key design patterns:

1. **Separation of summary vs detail**: `ServiceSummary` (search results) contains just enough for an agent to decide whether to look closer. `ServiceDetail` (single service lookup) contains everything -- pricing tiers, context requirements, operational metadata, the full manifest.

2. **`MatchedCapability.match_score`**: Only populated during semantic search. For structured queries (`GET /services?tag=travel.air.book`), this is `None`. This lets the response model serve both query types without a separate type.

3. **`OntologyResponse.by_domain`**: Pre-grouped for convenience. An agent exploring capabilities can iterate by domain (`TRAVEL`, `FINANCE`, etc.) without client-side grouping.

4. **`ServiceDetail.current_manifest: dict[str, Any]`**: Stores the raw manifest JSON as-is. This is an escape hatch -- if a client needs a field that AgentLedger doesn't index, they can find it in the raw manifest.

---

## Data Flow: From HTTP Request to Database

```
Agent sends POST /manifests
        |
        v
FastAPI parses JSON body
        |
        v
ServiceManifest.sanitize_inputs()     # mode="before" -- raw dict
  |-- check_null_bytes_recursive()     # reject \x00
  `-- strip_strings_recursive()        # trim whitespace
        |
        v
Pydantic field parsing                 # UUID, HttpUrl, Literal checks
        |
        v
ServiceManifest.validate_domain()     # FQDN regex
ServiceManifest.validate_capabilities() # 1-50 items, no duplicates
        |
        v
Router handler receives typed model   # All fields guaranteed valid
        |
        v
registry.register_manifest()          # Business logic
        |
        v
ManifestRegistrationResponse          # Clean response model
```

If any step fails, the request gets a 422 Unprocessable Entity with a detailed error. The business logic in `registry.py` never sees invalid data.

---

## Hands-On Exercises

### Exercise 1: Test Null Byte Rejection

```python
from api.models.sanitize import check_null_bytes_recursive

# Should find violations
data = {"name": "Good Service", "capabilities": [{"description": "Has \x00 null byte"}]}
violations = check_null_bytes_recursive(data)
print(violations)
# Expected: ['capabilities[0].description']
```

### Exercise 2: Test FQDN Validation

```python
from api.models.manifest import _FQDN_RE

# Valid domains
assert _FQDN_RE.fullmatch("example.com")
assert _FQDN_RE.fullmatch("api.agent-service.io")

# Invalid domains
assert not _FQDN_RE.fullmatch("not a domain")
assert not _FQDN_RE.fullmatch("-starts-with-hyphen.com")
assert not _FQDN_RE.fullmatch("localhost")       # no TLD
assert not _FQDN_RE.fullmatch("a" * 254 + ".com")  # too long
```

### Exercise 3: Test Duplicate Capability Rejection

```python
from api.models.manifest import ServiceManifest
from uuid import uuid4
from datetime import datetime

try:
    ServiceManifest(
        manifest_version="1.0",
        service_id=uuid4(),
        name="Test",
        domain="example.com",
        capabilities=[
            {"id": "cap1", "ontology_tag": "travel.air.book", "description": "A" * 20},
            {"id": "cap2", "ontology_tag": "travel.air.book", "description": "B" * 20},  # duplicate!
        ],
        pricing={"model": "free"},
        context={"required": [], "optional": []},
        operations={},
        last_updated=datetime.now(),
    )
except Exception as e:
    print(e)
    # Expected: capabilities contain duplicate ontology_tag values: travel.air.book
```

---

## Interview Prep

**Q: Why does AgentLedger validate at the Pydantic model layer instead of at the database layer?**

**A:** Three reasons: (1) **Better error messages** -- Pydantic returns structured validation errors with field paths, while database constraint violations produce cryptic messages. (2) **Fail fast** -- Invalid data is rejected before any database connection is used, saving resources. (3) **Defense in depth** -- The database still has constraints (NOT NULL, CHECK, UNIQUE), but Pydantic catches most issues first. This layered approach means a bug in one layer doesn't compromise security.

---

**Q: What is the `mode="before"` model validator and why is it used for sanitization?**

**A:** A `mode="before"` model validator runs on the raw input dict before Pydantic parses fields into their declared types. This is necessary for sanitization because: (1) We need to strip whitespace from strings that will become UUIDs, URLs, or enums -- after parsing, they're no longer strings. (2) We need to check for null bytes in the raw data before a type parser crashes on them. A `mode="after"` validator would be too late.

---

## Key Takeaways

- Request models (`manifest.py`, `query.py`) have validators; response models (`service.py`) don't
- `sanitize.py` provides recursive null-byte detection and whitespace stripping
- The FQDN regex prevents invalid domains from reaching the DNS crawler
- `ContextField` uses `extra = "allow"` for manifest format interoperability
- Capabilities are capped at 50 with no duplicate ontology tags
- `mode="before"` validators run on raw dicts before type parsing
- Response models separate summary (search) from detail (single lookup)

---

## Summary Reference Card

| Model | File | Type | Key Constraint |
|-------|------|------|----------------|
| `ServiceManifest` | manifest.py | Request | FQDN domain, 1-50 unique capabilities |
| `CapabilityManifest` | manifest.py | Nested | `ontology_tag` pattern, description 20-2000 chars |
| `ContextField` | manifest.py | Nested | `extra="allow"`, fallback name resolution |
| `SearchRequest` | query.py | Request | query max 500, limit 1-100 |
| `ManifestRegistrationResponse` | query.py | Response | typosquat_warnings list |
| `ServiceSummary` | service.py | Response | Compact for search results |
| `ServiceDetail` | service.py | Response | Full service record with raw manifest |

---

## Ready for Lesson 05?

Next up, we'll explore **The Filing Cabinet** -- the manifest registration pipeline that takes a validated `ServiceManifest` and writes it across 6 database tables in a single transaction. Get ready to see how AgentLedger handles upserts, embedding generation, and typosquat detection!

*Remember: Every SQL injection, every data corruption bug, every "but the client sent garbage" issue -- they all die here at the model layer. This is your first and best line of defense!*
