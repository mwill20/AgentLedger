# AgentLedger - Layer 2 Implementation Spec
## Identity & Attestation

**Version:** 0.1  
**Status:** Ready for Implementation  
**Author:** Michael Williams  
**Last Updated:** April 2026

---

## Purpose of This Document

This is the implementation specification for Layer 2 of AgentLedger - Identity & Attestation. It is written for Claude Code, Codex, or any developer extending the Layer 1 registry into a cryptographic trust boundary.

Every identity, credential, session, and approval decision described here is intentional. Do not build beyond this scope without updating this spec first.

---

## What Layer 2 Builds

Layer 1 answers: "What services exist and what can they do?"

Layer 2 answers: "Who is making this call, and are they who they claim to be?"

Layer 2 adds three capabilities:
1. **Agent Identity Credentials** - every agent receives a cryptographically verifiable identity credential it can present when requesting access to a service
2. **Service Identity Validation** - the dormant Layer 1 `public_key` field becomes active; services publish a `did:web` document and cryptographically sign manifests
3. **Mutual Authentication Protocol** - AgentLedger brokers a session handshake that verifies the agent, verifies the service, and issues a short-lived session assertion bound to a single service + capability request

Layer 2 does NOT include blockchain anchoring, third-party auditor federation, cross-registry blocklists, zero-knowledge context proofs, or OAuth2 delegation. Those remain Layer 3+ or future-version work.

---

## v0.1 Decisions Locked

These choices are final for the v0.1 build.

| Decision | Locked Choice | Reason |
|----------|---------------|--------|
| Agent DID method | `did:key` | Self-sovereign, offline-verifiable, no registry lookup required |
| Service DID method | `did:web` | Reuses Layer 1 domain verification and standard HTTPS hosting |
| Issuer DID method | `did:web:agentledger.io` | Single root of trust for credentials and session assertions |
| Credential format | JWT Verifiable Credentials | Standardized, compact, no JSON-LD framing complexity |
| Service key type | Ed25519 / `alg=EdDSA` | Fast signatures and simple Python support |
| HITL delivery | Pull first, webhook optional | Works without webhook infrastructure |
| Replay prevention | Online session redemption | One-use enforcement does not work with offline-only JWT checks |
| Trust tier semantics | `trust_tier=4` stays reserved for Layer 3 | Layer 2 raises `attestation_score`, not the meaning of "ledger attested" |

Two clarifications matter:

1. Session assertions are not pure bearer tokens. Services must redeem them against AgentLedger before acting.
2. `did:web` resolution is cached and revalidated out of band. Session issuance should not fetch remote DID documents on the hot path.

---

## Technology Stack

Layer 2 extends the existing Layer 1 stack.

| Component | Technology | Reason |
|-----------|------------|--------|
| API Framework | FastAPI (Python 3.11+) | Reuse current app and dependency model |
| Database | PostgreSQL 15+ | Durable identity registry, revocation log, session state |
| Cache | Redis 7+ | Nonce cache, revocation cache, DID document cache |
| Background Jobs | Celery + Redis | Revalidation, expiry pruning, optional webhook dispatch |
| Crypto | `cryptography` | Ed25519 key handling and detached signatures |
| JWT / JOSE | `PyJWT` | VC issuance and session assertion encoding |
| HTTP Client | `httpx` | `did:web` resolution and optional webhooks |
| Auth | API key for admin/registration, Bearer VC for agents | Keeps Layer 1 compatibility while enabling agent-native auth |
| Testing | `pytest` + `httpx` + `locust` | Unit, integration, and load testing |

Do not add a custom DID method, JSON-LD, Linked Data Proofs, or blockchain dependencies in this layer.

---

## Repository Structure

Preserve the current Layer 1 runtime shape and add Layer 2 modules in-place. `spec/LAYER2_SPEC.md` is the canonical Layer 2 spec path.

```text
AgentLedger/
|-- api/
|   |-- config.py                      # Add issuer, TTL, and role-based API key settings
|   |-- dependencies.py                # API key + VC auth dependencies
|   |-- routers/
|   |   |-- identity.py                # Agent register/verify/revoke, service DID, sessions
|   |   `-- authorization.py           # HITL approval queue endpoints
|   |-- models/
|   |   |-- identity.py                # DID docs, VC, session request/response models
|   |   `-- authorization.py           # Approval request/decision models
|   `-- services/
|       |-- crypto.py                  # Ed25519 helpers and canonical JSON signing
|       |-- did.py                     # did:key generation and did:web resolution/cache
|       |-- credentials.py             # JWT VC issuance and validation
|       |-- sessions.py                # Session request/redeem logic
|       |-- service_identity.py        # Signed manifest and did:web validation
|       |-- revocations.py             # Revocation writes and Redis cache
|       |-- authorization.py           # HITL queue and timeout logic
|       `-- webhooks.py                # Optional approval webhook client
|-- crawler/
|   `-- tasks/
|       |-- expire_identity_records.py # Prune expired sessions / auth requests
|       `-- revalidate_service_identity.py
|-- db/
|   `-- migrations/versions/002_layer2_identity.py
|-- spec/
|   |-- LAYER1_SPEC.md
|   `-- LAYER2_SPEC.md
`-- tests/
    |-- test_api/test_identity.py
    |-- test_api/test_authorization.py
    |-- test_api/test_service_identity.py
    `-- test_integration/test_identity_flow.py
```

No new top-level services are required. Reuse the current app, Redis, Postgres, Celery worker, and Celery beat containers.

---

## Database Schema

Layer 2 adds new tables. It does **not** alter existing Layer 1 tables in the schema itself.

Layer 2 may still update existing Layer 1 row values at runtime:
- `services.trust_score`
- `services.last_verified_at`
- `services.public_key`
- `manifests.raw_json`
- `crawl_events.details`

```sql
CREATE TABLE agent_identities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    did TEXT UNIQUE NOT NULL,
    agent_name TEXT NOT NULL,
    issuing_platform TEXT,
    public_key_jwk JSONB NOT NULL,
    capability_scope TEXT[] NOT NULL DEFAULT '{}',
    risk_tier TEXT NOT NULL DEFAULT 'standard',
    credential_hash TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    revoked_at TIMESTAMPTZ,
    revocation_reason TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    credential_expires_at TIMESTAMPTZ
);

CREATE TABLE authorization_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did TEXT NOT NULL,
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    sensitivity_tier INTEGER NOT NULL,
    request_context JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    approver_id TEXT,
    decided_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE session_assertions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assertion_jti TEXT UNIQUE NOT NULL,
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    assertion_token TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    authorization_ref UUID REFERENCES authorization_requests(id),
    was_used BOOLEAN NOT NULL DEFAULT false,
    used_at TIMESTAMPTZ
);

CREATE TABLE revocation_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    revoked_by TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_identities_platform ON agent_identities(issuing_platform);
CREATE INDEX agent_identities_risk_tier ON agent_identities(risk_tier);
CREATE INDEX session_assertions_agent ON session_assertions(agent_did, expires_at);
CREATE INDEX session_assertions_service ON session_assertions(service_id, expires_at);
CREATE INDEX session_assertions_expires ON session_assertions(expires_at);
CREATE INDEX authorization_requests_status ON authorization_requests(status, expires_at);
CREATE INDEX revocation_events_target ON revocation_events(target_type, target_id, created_at DESC);
```

Implementation notes:

1. `session_assertions.assertion_jti` is required even though the original design draft only stored the token. Atomic replay prevention is easier and faster by `jti` than by the full JWT string.
2. Service identity activation state is derived from the current manifest, `services.public_key`, and the latest successful validation event in `crawl_events`. A dedicated `service_identities` table is deliberately deferred in v0.1.
3. Expired rows in `session_assertions` and `authorization_requests` should be pruned every minute by Celery.

---

## DID Documents and Credential Formats

### Agent DID Method: `did:key`

Agents generate their own Ed25519 keypair locally. AgentLedger does **not** generate private keys for agents.

Rules:
- Only `did:key` with Ed25519 is supported in v0.1
- The request DID document must match the submitted public JWK
- The registry recomputes the DID from the JWK and rejects any mismatch

### Service DID Method: `did:web`

Services publish a DID document at:

```text
https://<service-domain>/.well-known/did.json
```

Rules:
- Only root-domain `did:web:<domain>` form is supported in v0.1
- Path-based `did:web` identifiers are out of scope
- The verification method used for manifest signing must appear in both `authentication` and `assertionMethod`

### AgentLedger Issuer DID

AgentLedger must expose its issuer DID document publicly at:

```text
/v1/identity/.well-known/did.json
```

This is the root of trust for agent credentials and session assertions.

### JWT VC Format

All agent credentials are JWT VCs signed by AgentLedger with `alg=EdDSA`.

```json
{
  "iss": "did:web:agentledger.io",
  "sub": "did:key:z6Mk...",
  "jti": "uuid",
  "iat": 1714000000,
  "nbf": 1714000000,
  "exp": 1745536000,
  "vc": {
    "type": ["VerifiableCredential", "AgentIdentityCredential"],
    "credentialSubject": {
      "id": "did:key:z6Mk...",
      "agent_name": "TripPlanner",
      "issuing_platform": "gpt",
      "capability_scope": ["travel.*", "commerce.*"],
      "risk_tier": "standard"
    }
  }
}
```

Rules:
- VC lifetime is 365 days by default
- `sub` and `vc.credentialSubject.id` must match exactly
- Services must be able to validate signature + expiry without calling AgentLedger
- Revocation checks remain online because revocation state is intentionally not embedded in the JWT

### Session Assertion Format

Session assertions are short-lived JWTs signed by AgentLedger with `alg=EdDSA`.

```json
{
  "iss": "did:web:agentledger.io",
  "sub": "did:key:z6Mk...",
  "aud": "did:web:payservice.com",
  "jti": "uuid",
  "iat": 1714000300,
  "nbf": 1714000300,
  "exp": 1714000600,
  "service_id": "uuid",
  "ontology_tag": "commerce.payments.send",
  "authorization_ref": null
}
```

Rules:
- default TTL is 5 minutes
- approved HITL assertions may extend to 15 minutes
- `aud` must equal the target service DID
- session assertions are single-use and must be redeemed before action

### Canonicalization

All detached signatures in Layer 2 use JSON Canonicalization Scheme (JCS, RFC 8785) over the request or manifest payload with the signature value omitted.

Do not sign Python dict string representations. Do not sign pretty-printed JSON.

---

## Manifest Schema Extensions

Layer 2 extends the Layer 1 manifest schema with two optional top-level blocks:

```json
{
  "identity": {
    "did": "did:web:flightbookerpro.com",
    "verification_method": "did:web:flightbookerpro.com#agentledger-key-1"
  },
  "signature": {
    "alg": "EdDSA",
    "value": "base64url-detached-signature"
  }
}
```

Rules:

1. `public_key` remains the canonical manifest public key field from Layer 1. In Layer 2 it must contain the same public key represented by `identity.verification_method`.
2. `identity.did` must equal `did:web:<manifest.domain>`.
3. `signature.value` signs the canonical manifest payload with the entire `signature` block omitted.
4. Existing Layer 1 services keep their current records. Enforcement begins on the next manifest submission or on explicit service activation.

---

## API Specification

Base URL: `https://api.agentledger.io/v1`  
Local base URL: `http://localhost:8000/v1`

### Authentication Model

| Endpoint Class | Auth |
|----------------|------|
| Public read endpoints | No auth |
| Agent registration | `X-API-Key` |
| Agent session requests | `Authorization: Bearer <agent-vc-jwt>` |
| Service session redemption | Service proof in request body |
| Admin endpoints | `X-API-Key` present in `ADMIN_API_KEYS` |
| Approver endpoints | `X-API-Key` present in `APPROVER_API_KEYS` |

New settings required in `api/config.py`:
- `issuer_did`
- `issuer_private_jwk`
- `admin_api_keys`
- `approver_api_keys`
- `session_assertion_ttl_seconds` (default `300`)
- `approved_session_ttl_seconds` (default `900`)
- `authorization_request_ttl_seconds` (default `300`)
- `did_web_cache_ttl_seconds` (default `600`)
- `revocation_cache_ttl_seconds` (default `300`)
- `approval_webhook_url` (optional)
- `approval_webhook_secret` (optional)

### Public Endpoints

#### GET /identity/.well-known/did.json
Returns AgentLedger's issuer DID document.

#### GET /identity/agents/{did}
Returns an agent DID document plus public registry metadata.

#### GET /identity/services/{domain}/did
Returns the service DID document currently cached or fetched from `https://<domain>/.well-known/did.json`.

### Agent Endpoints

#### POST /identity/agents/register
Issue an agent identity VC after proof of key control.

Request:

```json
{
  "did": "did:key:z6Mk...",
  "did_document": {},
  "agent_name": "TripPlanner",
  "issuing_platform": "gpt",
  "capability_scope": ["travel.*"],
  "risk_tier": "standard",
  "proof": {
    "nonce": "base64url",
    "created_at": "2026-04-13T10:15:00Z",
    "signature": "base64url"
  }
}
```

Validation:
- DID recomputes from the supplied Ed25519 public JWK
- proof signature verifies against the supplied key
- nonce has not been seen before
- `capability_scope` values are ontology prefixes or exact tags

Response `201`: `did`, `credential_jwt`, `credential_expires_at`, `did_document`, `issuer_did`

#### POST /identity/agents/verify
Verify a presented VC for services that want an online status check in addition to local JWT validation.

Request: `{ "credential_jwt": "..." }`

Response `200`: `valid`, `did`, `expires_at`, `is_revoked`, `capability_scope`, `risk_tier`

#### POST /identity/agents/{did}/revoke
Revoke an agent credential.

Request: `{ "reason_code": "key_compromised", "evidence": {} }`

Response `200`: `did`, `revoked_at`, `reason_code`

### Session Endpoints

#### POST /identity/sessions/request
Request a session assertion for one agent -> one service -> one ontology tag.

Request:

```json
{
  "service_domain": "payservice.com",
  "ontology_tag": "commerce.payments.send",
  "request_context": {
    "amount_bucket": "100-500",
    "currency": "USD"
  },
  "proof": {
    "nonce": "base64url",
    "created_at": "2026-04-13T10:16:00Z",
    "signature": "base64url"
  }
}
```

Validation:
- VC signature valid
- VC not expired
- agent not revoked
- proof signature verifies against the agent's `did:key`
- `ontology_tag` exists and is in the agent's scope
- target service exists and has active service identity
- service manifest declares the requested ontology tag

Behavior:
- if `sensitivity_tier < 3`, issue a session assertion immediately
- if `sensitivity_tier >= 3`, create `authorization_requests` and return pending state

Responses:
- `200`: `status=issued`, `session_id`, `assertion_jwt`, `expires_at`, `service_did`
- `202`: `status=pending_approval`, `authorization_request_id`, `expires_at`, `poll_url`
- `403`: revoked agent, out-of-scope capability, or stale/invalid service identity

#### GET /identity/sessions/{id}
Poll a pending HITL flow.

Responses:
- pending: `status=pending_approval`, `expires_at`
- approved: `status=issued`, `assertion_jwt`, `expires_at`, `authorization_request_id`
- denied: `status=denied`
- expired: `status=expired`

#### POST /identity/sessions/redeem
Redeem a session assertion exactly once. This is the enforcement point for replay prevention.

Request:

```json
{
  "assertion_jwt": "...",
  "service_did": "did:web:payservice.com",
  "proof": {
    "nonce": "base64url",
    "created_at": "2026-04-13T10:16:20Z",
    "signature": "base64url"
  }
}
```

Validation:
- assertion JWT signature valid
- assertion not expired
- assertion `aud` equals `service_did`
- service proof verifies against the active `did:web` verification method
- assertion row exists and `was_used=false`

Responses:
- `200`: `status=accepted`, `agent_did`, `ontology_tag`, `authorization_ref`
- `403`: invalid or expired assertion
- `409`: assertion already redeemed

Implementation rule: session redemption must execute as a single atomic database update.

### Service Identity Endpoints

#### POST /identity/services/{domain}/activate
Validate a signed manifest against the service's `did:web` document and activate service identity.

Rules:
- endpoint reads the current manifest from the database; it does not accept an arbitrary out-of-band blob
- service must exist and be at least Layer 1 domain-verified (`trust_tier >= 2`)
- current manifest must contain `public_key`, `identity`, and `signature`
- `did:web` document resolves over HTTPS
- DID document key must match manifest `public_key`
- detached manifest signature must verify

Response `200`: `domain`, `did`, `identity_status=active`, `attestation_score`, `trust_score`, `verified_at`

### Authorization Endpoints

#### GET /authorization/pending
List pending HITL requests ordered by `created_at ASC`.

#### POST /authorization/approve/{id}
Approve a pending HITL request and mint the session assertion.

Request: `{ "approver_id": "ops@agentledger.io" }`

#### POST /authorization/deny/{id}
Deny a pending HITL request.

Request: `{ "approver_id": "ops@agentledger.io", "reason": "manual_review_failed" }`

Webhook behavior:
- if `approval_webhook_url` is configured, AgentLedger sends a best-effort POST when a new request enters `pending`
- polling remains the source of truth
- webhook delivery failures must not fail request creation

---

## Mutual Authentication Flow

### 1. Agent Registration
1. Agent generates an Ed25519 keypair locally
2. Agent derives its `did:key`
3. Agent calls `POST /identity/agents/register` with DID + proof of key control
4. AgentLedger verifies the proof and issues a JWT VC
5. Agent stores the VC locally

### 2. Service Identity Activation
1. Service publishes `https://<domain>/.well-known/did.json`
2. Service re-submits its manifest through Layer 1 with `identity` + `signature`
3. Service calls `POST /identity/services/{domain}/activate`
4. AgentLedger resolves the DID document, verifies the manifest signature, and caches the validation result
5. The service becomes eligible for `attestation_score=1.0`

### 3. Normal Session Flow
1. Agent requests a session assertion with its VC and a signed proof
2. AgentLedger validates the agent and target service
3. AgentLedger returns a 5-minute session assertion JWT
4. Agent presents that assertion to the service
5. Service verifies the JWT locally, then calls `POST /identity/sessions/redeem`
6. AgentLedger verifies the service proof and atomically marks the assertion used
7. Only then does the service proceed

### 4. HITL Session Flow
1. Agent requests a session assertion
2. AgentLedger detects `sensitivity_tier >= 3`
3. AgentLedger creates `authorization_requests` with a 5-minute expiry
4. Approver polls `/authorization/pending` or receives an optional webhook
5. On approval, AgentLedger issues a session assertion with a 15-minute TTL
6. Agent polls `GET /identity/sessions/{id}` until the assertion is issued or denied
7. Service still must redeem the final assertion before acting

---

## Layer 1 Integration Notes

### 1. `api/dependencies.py`
Replace the single `require_api_key()` gate with:
- `require_api_key()` for admin and service write endpoints
- `require_admin_api_key()` for revocation
- `require_approver_api_key()` for HITL actions
- `require_bearer_credential()` for agent session requests and polling

### 2. Manifest Ingest
`POST /manifests` continues to accept unsigned manifests, but signed manifests must be validated when `identity` + `signature` are present.

Rules:
- invalid signatures return `422`
- missing signature does not block Layer 1 registration
- missing valid identity data blocks future advancement beyond `trust_tier=1`

### 3. Trust Score
Layer 2 supplies:

```python
def compute_attestation_score(has_active_service_identity: bool) -> float:
    return 1.0 if has_active_service_identity else 0.0


def compute_reputation_score(successful_redemptions_30d: int, failed_redemptions_30d: int) -> float:
    total = successful_redemptions_30d + failed_redemptions_30d
    if total == 0:
        return 0.0
    return round(successful_redemptions_30d / total, 4)
```

Notes:
- `failed_redemptions_30d` includes expired, denied, and replay-rejected session attempts tied to a service
- reputation remains service-level in Layer 2 because that is what Layer 1 ranking consumes
- agent reputation is out of scope for v0.1

### 4. Trust Tier
Do **not** auto-promote services to `trust_tier=4` in Layer 2. Layer 2 improves trust ranking through `attestation_score`, not by redefining tiers.

### 5. Background Jobs
New recurring jobs:
- `revalidate_service_identity.py` - re-fetch `did:web` for activated services every 24 hours
- `expire_identity_records.py` - mark HITL requests expired and prune expired assertions every minute

---

## Threat Model Additions

| Threat | Attack | Severity | Mitigation |
|--------|--------|----------|------------|
| Credential theft | Agent private key is exfiltrated and reused | Critical | Short-lived assertions, scope-limited VCs, revocation endpoint |
| DID spoofing | Attacker attempts to register a different DID for the same key | Critical | Recompute `did:key` from submitted JWK; reject mismatch |
| Session replay | Captured assertion is replayed against the service | High | Mandatory `POST /identity/sessions/redeem` and atomic `was_used` update |
| HITL bypass | Agent retries through a lower-risk path to avoid review | High | Sensitivity tier is resolved from ontology tag, never caller-declared |

MITRE ATLAS mapping additions:
- Credential theft -> `AML.T0012`
- Session replay -> `AML.T0020`

---

## Build Order

### Phase 1 - Cryptographic Foundation
- `api/services/crypto.py`
- `api/services/did.py`
- `api/services/credentials.py`
- unit tests for Ed25519 sign/verify, `did:key`, `did:web`, and JWT issuance/verification
- **Done when:** sign -> verify roundtrip passes for both agent credentials and session assertions

### Phase 2 - Agent Identity API
- add Layer 2 models and router
- implement register / verify / revoke / issuer DID document
- **Done when:** an agent can register, receive a VC, and have the VC verified independently

### Phase 3 - Session Assertion Engine
- implement request + redeem flow
- add nonce replay cache in Redis
- add pruning job
- upgrade dependencies for Bearer VC auth
- **Done when:** a valid agent can obtain and redeem a session assertion in under 200ms p95 locally

### Phase 4 - Service Identity Activation
- extend manifest schema with `identity` + `signature`
- implement service DID activation and nightly revalidation
- wire `attestation_score` and `reputation_score` into `api/services/ranker.py`
- **Done when:** a domain-verified service activates its DID and its trust score increases

### Phase 5 - Human in the Loop
- add approval queue, approve/deny endpoints, polling, and optional webhook dispatch
- enforce `sensitivity_tier >= 3` interruption in session issuance
- **Done when:** a request for `health.records.retrieve` blocks until approval or timeout

### Phase 6 - Hardening
- cache revocation state in Redis
- load test identity endpoints at 100 concurrent users
- verify p95 < 200ms for request-heavy paths
- add coverage to 80%+ for new modules

---

## Running Tests

```bash
pytest -q

pytest tests/test_api/test_identity.py tests/test_api/test_authorization.py tests/test_api/test_service_identity.py tests/test_integration/test_identity_flow.py -q

pytest tests --cov=api --cov=crawler --cov-report=term -q
```

**Windows note:** If coverage cannot write `.coverage`, redirect it to a temp path:

```powershell
$env:COVERAGE_FILE = Join-Path $env:TEMP 'agentledger.coverage'
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

### Load Testing

Extend `tests/load/locustfile.py` with an `identity` profile that covers:
- agent session request
- HITL poll loop
- session redemption
- service DID activation cache-hit path

---

## Acceptance Criteria

- [ ] Agent registers and receives a valid JWT VC signed by AgentLedger
- [ ] VC verification passes independently without calling AgentLedger
- [ ] Session assertion issued for valid agent + valid service + valid `ontology_tag`
- [ ] Session assertion rejected for revoked agent credential
- [ ] Session assertion rejected on second redemption attempt
- [ ] Service with valid `public_key` + signed manifest activates DID and receives `attestation_score > 0`
- [ ] `sensitivity_tier >= 3` request creates a pending `authorization_requests` record
- [ ] Approval unblocks session assertion issuance
- [ ] HITL timeout after 5 minutes returns `403` to the agent
- [ ] Identity endpoints sustain `< 200ms` p95 under 100 concurrent requests locally

---

## What Layer 2 Does NOT Include

- Blockchain / on-chain storage
- Third-party auditor attestation network
- Cross-registry blocklist federation
- Zero-knowledge proofs for selective context disclosure
- OAuth2 agent delegation flows
- Payment-gated capability scopes
- Public agent reputation marketplace
- Multi-tenant webhook registration

---

*This spec is the source of truth for Layer 2. Update it before changing any Layer 2 behavior.*
