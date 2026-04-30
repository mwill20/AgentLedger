# Lesson 18 — The Forms: Data Models & API Routes

> **Beginner frame:** Routes are the public counters; models are the forms people hand in. This lesson shows how AgentLedger keeps identity APIs thin at the edge and puts the important rules in service code.

**Layer:** 2 — Identity & Credentials  
**Files:** `api/models/identity.py` (303 lines), `api/routers/identity.py` (251 lines)  
**Prerequisites:** Lessons 14–17 — the models and routes here are thin wrappers over the services you've already studied  
**Estimated time:** 60 minutes

---

## Welcome

A government agency's forms are the interface between the public and the bureaucracy. They define what information is accepted, what format it must be in, and what comes back. The actual decision-making happens in the back office — but without the forms, no one can interact with the system at all.

`models/identity.py` defines the forms: Pydantic schemas that validate, sanitize, and type-check every request and response. `routers/identity.py` defines the windows: 13 endpoints that accept requests, inject dependencies, call the right service, and apply HTTP status code logic before responding.

By the end of this lesson you will be able to:

- Explain the three auth tiers used across the 13 Layer 2 endpoints
- Describe how `model_validator(mode="before")` enforces null-byte and whitespace hygiene before field validation runs
- Trace the `_is_valid_scope` and FQDN validators
- Explain the HTTP status code override pattern for 202/403 responses
- Map each of the 13 endpoints to its service function and auth tier
- Explain why `AgentCredentialPrincipal` is an internal type, not a public response model

---

## What This Connects To

Every previous lesson in Layer 2 covered a service function. This lesson covers the HTTP boundary that exposes those functions. Think of it as the assembly manual for lessons 11–17: the router wires the dependency injectors to the services, and the models define the wire format.

**Lesson 19** covers the background workers that keep the system running between API calls — the models here define what those workers read and write.

**Lesson 20** is the full-flow walkthrough that traces a single request from HTTP boundary through models, service, and database — requiring everything from lessons 11–18.

---

## Architecture Position

```
HTTP Request
     │
     ▼
routers/identity.py       ← thin: validate path params, inject deps, call service
     │
     ├── Depends(get_db)              → AsyncSession
     ├── Depends(get_redis)           → Redis client
     ├── Depends(require_api_key)     → validates X-API-Key header
     ├── Depends(require_admin_api_key) → validates admin key
     └── Depends(require_bearer_credential) → validates JWT, returns AgentCredentialPrincipal
     │
     ▼
models/identity.py        ← parse, sanitize, validate request body
     │
     ▼
services/identity.py      ← business logic (Lessons 14–17)
services/sessions.py
services/authorization.py
services/service_identity.py
```

---

## Core Concepts

### The Three Auth Tiers

Layer 2 endpoints fall into four authentication categories:

| Dependency | Validates | Used by |
|---|---|---|
| (none) | Nothing — open | `GET /well-known/did.json`, `GET /services/{domain}/did`, `POST /sessions/redeem` |
| `require_api_key` | `X-API-Key` header against configured keys | Registration, session request (also needs Bearer), service activation |
| `require_admin_api_key` | `X-API-Key` header against admin keys | Revocation, pending queue, approve/deny |
| `require_bearer_credential` | `Authorization: Bearer <vc_jwt>` | Session request, session status |

**The interesting case:** `POST /identity/sessions/request` uses *both* `require_api_key` (as a `dependencies=[...]` list item) and `require_bearer_credential` (as a named parameter). The API key gates access to the endpoint itself; the bearer credential provides the agent identity. This two-layer design allows platform operators to restrict who can request sessions (only known API consumers) while still requiring agent-level credential presentation.

### `model_validator(mode="before")` Pattern

Four models use the same sanitize guard:

```python
@model_validator(mode="before")
@classmethod
def sanitize_inputs(cls, data: Any) -> Any:
    if isinstance(data, dict):
        null_fields = check_null_bytes_recursive(data)
        if null_fields:
            raise ValueError(f"null bytes are not allowed in: {', '.join(null_fields)}")
        data = strip_strings_recursive(data)
    return data
```

`mode="before"` means this validator runs **before** Pydantic field type coercion. That matters: if null bytes were only checked after coercion, a malformed string might already have been partially processed. Running the check first ensures no tainted data reaches any field validator or the service layer.

`check_null_bytes_recursive` (from `api/models/sanitize.py`) traverses the entire request dict, including nested dicts and lists. `strip_strings_recursive` trims leading/trailing whitespace from all string values. These two operations handle the most common injection vectors for web APIs receiving JSON.

### `Literal` for Status Fields

Several models use `Literal` to constrain status fields:

```python
class SessionStatusResponse(BaseModel):
    status: Literal["issued", "pending_approval", "denied", "expired"]

class AuthorizationRequestRecord(BaseModel):
    status: Literal["pending", "approved", "denied", "expired"]
```

`Literal` types serve two purposes:
1. **Runtime validation** — Pydantic rejects any value not in the set; no `if status not in (...)` guard needed
2. **Schema documentation** — OpenAPI generation renders these as enums, giving API consumers a precise list of valid values

---

## Models Walkthrough

### 1. `IdentityProof` (lines 33–38)

```python
class IdentityProof(BaseModel):
    nonce: str = Field(min_length=8, max_length=512)
    created_at: datetime
    signature: str = Field(min_length=16, max_length=2048)
```

Reused in both `AgentRegistrationRequest` and `SessionRequest`. The `min_length=8` on `nonce` prevents trivially short nonces that would make the key space too small for replay protection. `min_length=16` on `signature` is a sanity check — a real Ed25519 signature is exactly 88 characters in base64url (64 bytes), so anything shorter is obviously invalid.

### 2. `AgentRegistrationRequest` (lines 41–82)

**The `did:key` restriction (lines 65–71):**

```python
@field_validator("did")
@classmethod
def validate_did(cls, value: str) -> str:
    if not value.startswith("did:key:"):
        raise ValueError("agent DID must use did:key")
    return value
```

This validator gates the `v0.1` decision: only `did:key` identifiers are accepted for agent registration. A `did:web` agent (if allowed in a future version) would require this validator to be updated — making the restriction explicit and easy to find.

**The scope validator (lines 73–82):**

```python
@field_validator("capability_scope")
@classmethod
def validate_capability_scope(cls, value: list[str]) -> list[str]:
    invalid = [scope for scope in value if not _is_valid_scope(scope)]
    if invalid:
        raise ValueError(f"invalid capability_scope values: {', '.join(sorted(invalid))}")
    return value
```

`_is_valid_scope` (lines 18–30) implements the scope grammar:
- 1–3 dot-separated parts
- Each part must be non-empty, lowercase, and alphanumeric (underscores allowed)
- `"*"` is only valid as the **last** part (`"health.*"` is valid; `"*.records"` is not)

This prevents scope strings like `"../../etc/passwd"`, `"health.*.records"`, or `"HEALTH.RECORDS"` from entering the credential.

**`risk_tier` (line 49):**

```python
risk_tier: Literal["standard", "elevated", "restricted"] = "standard"
```

Three-level risk classification with a safe default. The tier flows into the issued credential and influences which ontology tags are accessible in session requests.

### 3. `AgentCredentialPrincipal` (lines 95–103)

```python
class AgentCredentialPrincipal(BaseModel):
    did: str
    capability_scope: list[str] = Field(default_factory=list)
    risk_tier: str
    public_key_jwk: dict[str, Any]
    credential_claims: dict[str, Any]
    credential_expires_at: datetime | None = None
```

This is **not a public API model** — it never appears in a response body. It's the internal representation of an authenticated agent principal, produced by `authenticate_agent_credential()` in `identity.py` and consumed by `sessions.py` and other services.

Carrying `public_key_jwk` and `credential_claims` on the principal means service functions downstream don't need to re-parse the JWT or look up the public key — the work was done once at the dependency injection boundary.

### 4. `SessionRequest` (lines 169–197)

**FQDN validator (lines 190–197):**

```python
@field_validator("service_domain")
@classmethod
def validate_service_domain(cls, value: str) -> str:
    normalized = value.strip().lower()
    if not _FQDN_RE.fullmatch(normalized):
        raise ValueError("service_domain must be a valid FQDN")
    return normalized
```

The regex (line 13–15) enforces:
- Total length 1–253 characters
- No leading hyphen on any label
- Labels: `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?` — alphanumeric start/end, hyphens allowed in the middle
- TLD: 2–63 lowercase letters

Normalizing to lowercase means `"Example.COM"` and `"example.com"` are treated identically. The validator returns the normalized value, so downstream code always sees lowercase FQDNs.

**`ontology_tag` pattern (line 173):**

```python
ontology_tag: str = Field(pattern=r"^[a-z]+\.[a-z]+\.[a-z]+$")
```

Exactly three dot-separated lowercase word components. This enforces the AgentLedger ontology tag format (`domain.category.action`) without a custom validator. Note this is more restrictive than the scope validator — it requires exactly 3 parts with no `*` wildcards, because a session request is for a specific tag, not a prefix.

### 5. `SessionStatusResponse` (lines 200–208)

```python
class SessionStatusResponse(BaseModel):
    status: Literal["issued", "pending_approval", "denied", "expired"]
    session_id: str | None = None
    assertion_jwt: str | None = None
    service_did: str | None = None
    authorization_request_id: str | None = None
    expires_at: datetime
```

Most fields are optional because not all statuses carry all data:
- `"issued"` → `session_id`, `assertion_jwt`, `service_did`, `expires_at`
- `"pending_approval"` → `authorization_request_id`, `expires_at`
- `"denied"` / `"expired"` → `authorization_request_id` or `session_id`, `expires_at`

`expires_at` is always present — the client always needs to know when to give up polling.

---

## Router Walkthrough

### Auth Tier Summary (all 13 endpoints)

```
Public (no auth):
  GET  /v1/identity/.well-known/did.json
  POST /v1/identity/agents/verify
  GET  /v1/identity/agents/{did_value}
  GET  /v1/identity/sessions/{session_id}  ← Bearer required
  POST /v1/identity/sessions/redeem
  GET  /v1/identity/services/{domain}/did

API key (require_api_key):
  POST /v1/identity/agents/register
  POST /v1/identity/services/{domain}/activate

Bearer credential (require_bearer_credential):
  POST /v1/identity/sessions/request       ← also require_api_key
  GET  /v1/identity/sessions/{session_id}

Admin API key (require_admin_api_key):
  POST /v1/identity/agents/{did_value}/revoke
  GET  /v1/authorization/pending
  POST /v1/authorization/approve/{id}
  POST /v1/authorization/deny/{id}
```

### The 202/403 Status Override Pattern (lines 122–153)

FastAPI routes default to `200 OK`. But the session endpoints need to communicate state through HTTP status codes:

```python
# POST /identity/sessions/request
async def request_session_assertion(
    ...,
    response: Response,       # ← FastAPI injects the response object
    ...
) -> SessionStatusResponse:
    result = await sessions.request_session(...)
    if result.status == "pending_approval":
        response.status_code = status.HTTP_202_ACCEPTED   # ← override
    return result
```

`Response` is a special FastAPI dependency — injecting it gives the route handler access to the outgoing response before it's sent. Mutating `response.status_code` changes the HTTP status without changing the response body. The client receives `202` (not yet complete) vs `200` (complete), which is the semantically correct distinction.

The GET session status endpoint uses the same pattern:

```python
if result.status == "pending_approval":
    response.status_code = status.HTTP_202_ACCEPTED
elif result.status in {"denied", "expired"}:
    response.status_code = status.HTTP_403_FORBIDDEN
```

`403` for `denied` and `expired` — the client doesn't need to retry; the answer is final.

### `del admin_api_key` (line 215)

```python
async def get_pending_authorizations(
    admin_api_key: str = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_db),
) -> AuthorizationPendingListResponse:
    del admin_api_key   # ← required for auth side-effect only
    return await authorization.list_pending_authorizations(db=db)
```

`require_admin_api_key` raises `401` if the key is invalid. But the route handler doesn't need the key value for anything — it only needs the side effect. The `del` statement is a Python convention signaling "this parameter was injected for its side effects only, not for its value." Some linters warn on unused parameters; the `del` silences the warning while making the intent explicit.

### The `require_bearer_credential` Dependency

```python
@router.get("/identity/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session_assertion_status(
    session_id: UUID,
    response: Response,
    principal: AgentCredentialPrincipal = Depends(require_bearer_credential),
    db: AsyncSession = Depends(get_db),
) -> SessionStatusResponse:
    result = await sessions.get_session_status(
        db=db, principal=principal, session_id=session_id
    )
```

`require_bearer_credential` (defined in `api/dependencies.py`) parses `Authorization: Bearer <jwt>`, calls `authenticate_agent_credential()`, and returns an `AgentCredentialPrincipal`. The route handler gets a fully-populated principal with no credential parsing work to do. This dependency is reused across every Bearer-authenticated endpoint, keeping all JWT verification in one place.

### Path Parameter Types

```python
@router.get("/identity/sessions/{session_id}", ...)
async def get_session_assertion_status(session_id: UUID, ...):
```

`UUID` as a path parameter type means FastAPI automatically validates the path segment is a valid UUID and parses it to `uuid.UUID`. Any non-UUID path value returns `422 Unprocessable Entity` before the route handler is invoked. This catches malformed IDs at the HTTP boundary without any explicit validation in the service layer.

---

## Exercises

### Exercise 1 — Inspect the OpenAPI schema

```bash
# With the API running
curl -s http://localhost:8000/openapi.json | python -c "
import json, sys
spec = json.load(sys.stdin)
# Print all Layer 2 endpoints
for path, methods in spec['paths'].items():
    if 'identity' in path or 'authorization' in path:
        for method, detail in methods.items():
            print(f'{method.upper():6} {path}')
            if 'security' in detail:
                print(f'       auth: {detail[\"security\"]}')
" | sort
```

Expected output (partial):
```
DELETE /v1/identity/agents/{did_value}/revoke
GET    /v1/identity/.well-known/did.json
GET    /v1/identity/agents/{did_value}
GET    /v1/identity/services/{domain}/did
GET    /v1/identity/sessions/{session_id}
GET    /v1/authorization/pending
POST   /v1/identity/agents/register
POST   /v1/identity/agents/verify
POST   /v1/identity/sessions/redeem
POST   /v1/identity/sessions/request
POST   /v1/identity/services/{domain}/activate
POST   /v1/authorization/approve/{authorization_request_id}
POST   /v1/authorization/deny/{authorization_request_id}
```

### Exercise 2 — Test scope validation

```python
from api.models.identity import AgentRegistrationRequest
from pydantic import ValidationError

# Valid scopes
try:
    req = AgentRegistrationRequest(
        did="did:key:z6Mk...",
        did_document={},
        agent_name="test",
        capability_scope=["health.*", "finance.reports", "search"],
        risk_tier="standard",
        proof={"nonce": "12345678", "created_at": "2026-04-27T12:00:00Z", "signature": "a" * 88},
    )
    print("Valid scopes accepted:", req.capability_scope)
except ValidationError as e:
    print("Error:", e)

# Invalid scopes
try:
    req2 = AgentRegistrationRequest(
        did="did:key:z6Mk...",
        did_document={},
        agent_name="test",
        capability_scope=["HEALTH.*", "*.records", "too.many.parts.here"],
        risk_tier="standard",
        proof={"nonce": "12345678", "created_at": "2026-04-27T12:00:00Z", "signature": "a" * 88},
    )
except ValidationError as e:
    print("Rejected invalid scopes:")
    for err in e.errors():
        print(" -", err["msg"])
```

Expected output:
```
Valid scopes accepted: ['health.*', 'finance.reports', 'search']
Rejected invalid scopes:
 - Value error, invalid capability_scope values: *.records, HEALTH.*, too.many.parts.here
```

### Exercise 3 — Test FQDN validation

```python
from api.models.identity import SessionRequest
from pydantic import ValidationError

cases = [
    ("example.com", True),
    ("sub.example.com", True),
    ("localhost", False),           # no TLD
    ("EXAMPLE.COM", False),         # uppercase (but validator normalizes)
    ("-invalid.com", False),        # leading hyphen
    ("a" * 64 + ".com", False),     # label too long
]

for domain, should_pass in cases:
    try:
        req = SessionRequest(
            service_domain=domain,
            ontology_tag="search.manifest.lookup",
            proof={"nonce": "12345678", "created_at": "2026-04-27T12:00:00Z", "signature": "a" * 88},
        )
        print(f"{'PASS' if should_pass else 'UNEXPECTED PASS':20} {domain!r} → {req.service_domain!r}")
    except ValidationError:
        print(f"{'FAIL (expected)' if not should_pass else 'UNEXPECTED FAIL':20} {domain!r}")
```

### Exercise 4 — Verify null-byte rejection

```python
from api.models.identity import AgentRegistrationRequest
from pydantic import ValidationError

try:
    req = AgentRegistrationRequest(
        did="did:key:z6Mk\x00injected",
        did_document={},
        agent_name="test",
        capability_scope=[],
        risk_tier="standard",
        proof={"nonce": "12345678", "created_at": "2026-04-27T12:00:00Z", "signature": "a" * 88},
    )
except ValidationError as e:
    for err in e.errors():
        print(err["msg"])
```

Expected output:
```
Value error, null bytes are not allowed in: did
```

---

## Best Practices

### What AgentLedger does

- **`model_validator(mode="before")` for security sanitization** — runs before field coercion; catches injection attempts at the earliest possible point
- **`Literal` types for status fields** — eliminates custom validation for enumerated values; improves OpenAPI documentation automatically
- **UUID path params** — delegates format validation to FastAPI; no service-layer UUID parsing needed
- **`del admin_api_key` for side-effect-only dependencies** — explicit signal that the dependency was injected only for auth enforcement
- **Response object mutation for 202/403** — returns semantically correct HTTP status codes without duplicating route definitions

### Recommended (not implemented here)

- **`response_model_exclude_none=True`** — omitting `None` fields from responses reduces payload size. Currently optional fields that are `None` are included in the serialized JSON. Setting `response_model_exclude_none=True` on response-heavy endpoints like `SessionStatusResponse` would reduce bandwidth.
- **Input length limits on `did_document` and `request_context`** — these accept arbitrary `dict[str, Any]`, which means a large nested object is accepted without size bounds. A custom validator that checks `len(json.dumps(value)) <= MAX_BYTES` would prevent abuse.
- **`AgentCredentialPrincipal` cache** — currently, every request with a Bearer token calls `authenticate_agent_credential()` which includes a database round trip. A short-lived Redis cache keyed on `sha256(credential_jwt)` would reduce database load for burst patterns.

---

## Interview Q&A

**Q: Why use `model_validator(mode="before")` instead of `field_validator` for null-byte checking?**

A: `field_validator` runs per-field after Pydantic coerces the raw input to the expected type. `mode="before"` runs the validator on the raw dict before any type coercion. This matters for nested structures: a null byte in a nested dict value would survive field-level validation on the top-level field (which only sees a `dict`). The `mode="before"` validator recursively traverses the entire input before any field sees it.

**Q: How does `require_bearer_credential` interact with `require_api_key` on the same endpoint?**

A: Both appear as dependencies, so FastAPI resolves them independently. `require_api_key` is in `dependencies=[]` (side-effect only — raises 401 if invalid), and `require_bearer_credential` is a named parameter (provides the `AgentCredentialPrincipal` value). Both must succeed for the request to reach the handler. An API key without a bearer token fails the second dependency; a bearer token without an API key fails the first.

**Q: Why does `SessionStatusResponse.expires_at` have no default value?**

A: `expires_at` is always meaningful — even `"denied"` and `"expired"` results have an expiry timestamp indicating when the original request was valid until. Making it non-optional forces the service layer to always supply it. If it were optional with a `None` default, a future refactor might accidentally omit it, and clients polling for status would lose the ability to know when to stop.

**Q: What is `AgentCredentialPrincipal` and why isn't it a response model?**

A: `AgentCredentialPrincipal` is an internal data transfer object produced by `authenticate_agent_credential()` and consumed by service functions downstream in the same request. It carries the bearer credential's claims plus database-derived state (capability scope, risk tier, public key). It's never serialized to an HTTP response body — the client already has the JWT it submitted. Making it a response model would either leak credential internals or require a separate public-facing representation.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 18 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ Auth tiers (4 levels)                                           │
│   public:          no header required                          │
│   require_api_key: X-API-Key header                            │
│   require_admin_api_key: admin X-API-Key                       │
│   require_bearer_credential: Authorization: Bearer <vc_jwt>    │
│                                                                 │
│ Validation order in models                                      │
│   1. model_validator(mode="before"): null-byte + strip        │
│   2. field_validator: did:key check, scope grammar, FQDN RE   │
│   3. Pydantic type coercion: Literal, UUID, datetime           │
│                                                                 │
│ HTTP status overrides                                           │
│   202 Accepted: status == "pending_approval"                   │
│   403 Forbidden: status in {"denied", "expired"}               │
│   Default: 200 OK (and 201 Created for register)               │
│                                                                 │
│ Scope grammar (_is_valid_scope)                                │
│   1–3 dot-separated parts, lowercase, alphanumeric+underscore  │
│   "*" only as final part (health.*)                            │
│   Exact: "health.records" or prefix: "health.*" or "health"   │
│                                                                 │
│ ontology_tag format (SessionRequest)                           │
│   Exactly 3 lowercase parts: /^[a-z]+\.[a-z]+\.[a-z]+$/      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 19 — The Night Shift** covers `crawler/tasks/expire_identity_records.py` and `crawler/tasks/revalidate_service_identity.py`: the two Layer 2 background workers that keep identity state consistent without operator intervention, including their Celery schedules, Redis patterns, and failure behavior.
