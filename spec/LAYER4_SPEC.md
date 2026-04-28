# AgentLedger - Layer 4 Implementation Spec
## Context Matching: Privacy-Preserving Context Disclosure

**Version:** 0.1
**Status:** Ready for Implementation
**Author:** Michael Williams
**Last Updated:** April 2026
**Depends on:** Layer 1 (complete), Layer 2 (complete), Layer 3 (complete)

---

## Purpose of This Document

This is the implementation specification for Layer 4 of AgentLedger - Context
Matching. It is written for Claude Code or any developer building the system from
scratch. Every design decision is documented. Nothing should require guessing.

Do not build anything not described here without updating this spec first.

---

## What Layer 4 Builds

Layer 4 adds the privacy-preserving context routing layer that sits between an
agent carrying user context and a service that needs some of it to function.

Three capabilities:

1. **Context Profiles** - user-defined rules specifying what an agent is
   permitted to share with what class of service, enforced by protocol
2. **Context Matching Engine** - evaluates whether a service's declared context
   requirements are satisfied by what the agent's profile permits sharing
3. **Selective Disclosure** - generates cryptographically committed disclosure
   packages that prove a field is present without revealing its value until the
   service is trust-verified to receive it

Layer 4 does NOT include:
- Full zero-knowledge proof circuits (ZKP via circom/snark.js) - deferred to v0.2
- Payment-gated context tiers - Layer 5
- Insurance underwriting on context disclosures - Layer 6
- OAuth2 user authorization flows - v0.2

---

## The Core Problem Layer 4 Solves

Layer 1 introduced the Context Requirements Block in every manifest:
```json
"context": {
  "required": ["user.name", "user.email"],
  "optional": ["user.dob", "user.insurance_id"],
  "data_retention_days": 30,
  "data_sharing": "none"
}
```

This block declares what a service *claims* it needs. Layer 4 enforces three
things that Layer 1 cannot:

1. **Minimization** - the agent sends only what the service declared, even if
   the agent carries more context than that
2. **Permission gating** - the agent sends only what the user's profile permits
   sharing with that service category, even if the service declared it required
3. **Mismatch detection** - if a service requests context at runtime that
   exceeds what it declared in its manifest, that is flagged as a violation

The central invariant: **context flows only when (manifest declared it) AND
(user profile permits it) AND (service trust score clears the threshold)**.

---

## Technology Stack

All stack decisions are final for v0.1. Do not substitute without updating this
spec. Layer 4 adds to the existing stack - no replacements.

| Component | Technology | Reason |
|---|---|---|
| API Framework | FastAPI (Python 3.11+) | Existing - no change |
| Database | PostgreSQL 15+ | Existing - new tables added |
| Cache | Redis 7+ | Existing - disclosure cache + profile cache |
| Crypto (commitments) | Python `cryptography` library (HMAC-SHA256) | Already a dependency from Layer 2; commitment scheme uses HMAC with a per-disclosure nonce |
| Compliance exports | Python `reportlab` (PDF) | GDPR/CCPA audit export |
| Testing | pytest + httpx | Existing |

**ZK Commitment Scheme (v0.1):**
Full ZKP (circom circuits, snark.js) is deferred to v0.2. v0.1 uses HMAC-SHA256
commitments. For each sensitive field, the system generates:
```
commitment = HMAC-SHA256(key=nonce, msg=field_value)
```
The commitment is shared with the service. The field value is withheld until the
service is trust-verified (trust_tier >= 3). On verification, the nonce is
released. The service recomputes the commitment and confirms the value matches.
This provides tamper-evident selective disclosure without full ZKP complexity.

---

## Repository Structure - New Files Only

Add these files to the existing AgentLedger structure:

```
AgentLedger/
├── api/
│   ├── routers/
│   │   └── context.py              # All Layer 4 endpoints
│   ├── models/
│   │   └── context.py              # Pydantic models for Layer 4
│   └── services/
│       ├── context_profiles.py     # Profile CRUD + rule enforcement
│       ├── context_matcher.py      # Matching engine
│       ├── context_disclosure.py   # Selective disclosure package generation
│       ├── context_mismatch.py     # Over-request detection
│       └── context_compliance.py  # GDPR/CCPA export generation
├── db/
│   └── migrations/
│       └── versions/
│           └── 005_layer4_context.py
├── tests/
│   └── test_api/
│       ├── test_context_profiles.py
│       ├── test_context_matcher.py
│       ├── test_context_disclosure.py
│       └── test_context_mismatch.py
```

Do not add any other files.

---

## Database Schema - New Tables Only

Add to the existing schema. Do not modify any Layer 1-3 tables.

```sql
-- User-controlled context sharing profiles (one per agent DID)
CREATE TABLE context_profiles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    profile_name TEXT NOT NULL DEFAULT 'default',
    is_active BOOLEAN NOT NULL DEFAULT true,
    default_policy TEXT NOT NULL DEFAULT 'deny',
    -- deny = block all sharing unless explicitly permitted
    -- allow = permit all sharing unless explicitly restricted
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(agent_did, profile_name)
);

-- Individual rules within a context profile
-- Rules are evaluated in priority order (lower number = higher priority)
CREATE TABLE context_profile_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id UUID NOT NULL REFERENCES context_profiles(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100,
    scope_type TEXT NOT NULL,
    -- 'domain'       -> applies to all services in an ontology domain (e.g. HEALTH)
    -- 'trust_tier'   -> applies to all services at or above a trust tier
    -- 'service_did'  -> applies to a specific service by did:web
    -- 'sensitivity'  -> applies to fields at or above a sensitivity_tier
    scope_value TEXT NOT NULL,
    -- for domain: 'HEALTH', 'FINANCE', etc.
    -- for trust_tier: '3', '4'
    -- for service_did: 'did:web:pharmacy.example.com'
    -- for sensitivity: '3', '4'
    permitted_fields TEXT[] NOT NULL DEFAULT '{}',
    -- field names from the manifest context block e.g. 'user.name', 'user.email'
    -- empty array = no fields permitted under this rule
    denied_fields TEXT[] NOT NULL DEFAULT '{}',
    -- fields explicitly blocked regardless of other rules
    action TEXT NOT NULL DEFAULT 'permit',
    -- 'permit' = allow listed fields to flow
    -- 'deny'   = block listed fields from flowing
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Cryptographic commitments for selective disclosure (v0.1 HMAC scheme)
CREATE TABLE context_commitments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    service_id UUID NOT NULL REFERENCES services(id),
    session_assertion_id UUID REFERENCES session_assertions(id),
    field_name TEXT NOT NULL,
    commitment_hash TEXT NOT NULL,   -- HMAC-SHA256(nonce, field_value)
    nonce TEXT NOT NULL,             -- released to service after trust verification
    nonce_released BOOLEAN NOT NULL DEFAULT false,
    nonce_released_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL, -- commitment TTL: 15 minutes
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log of all context disclosures (append-only, never delete)
CREATE TABLE context_disclosures (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    service_id UUID NOT NULL REFERENCES services(id),
    session_assertion_id UUID REFERENCES session_assertions(id),
    ontology_tag TEXT NOT NULL,
    fields_requested TEXT[] NOT NULL DEFAULT '{}',   -- what service asked for
    fields_disclosed TEXT[] NOT NULL DEFAULT '{}',   -- what actually flowed
    fields_withheld TEXT[] NOT NULL DEFAULT '{}',    -- what was blocked by profile
    fields_committed TEXT[] NOT NULL DEFAULT '{}',   -- committed but not yet revealed
    disclosure_method TEXT NOT NULL DEFAULT 'direct',
    -- 'direct'     = field value transmitted in plaintext (low sensitivity)
    -- 'committed'  = HMAC commitment only, value withheld pending nonce release
    trust_score_at_disclosure FLOAT,
    trust_tier_at_disclosure INTEGER,
    profile_id UUID REFERENCES context_profiles(id),
    erased BOOLEAN NOT NULL DEFAULT false,           -- GDPR right to erasure
    erased_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Mismatch events: service requested context beyond manifest declaration
CREATE TABLE context_mismatch_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    agent_did TEXT NOT NULL,
    declared_fields TEXT[] NOT NULL DEFAULT '{}',    -- from manifest
    requested_fields TEXT[] NOT NULL DEFAULT '{}',   -- from runtime request
    over_requested_fields TEXT[] NOT NULL DEFAULT '{}', -- the delta
    severity TEXT NOT NULL DEFAULT 'warning',
    -- 'warning'  = over-request of low-sensitivity fields
    -- 'critical' = over-request of sensitivity_tier >= 3 fields
    resolved BOOLEAN NOT NULL DEFAULT false,
    resolution_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX context_profiles_agent ON context_profiles(agent_did) WHERE is_active = true;
CREATE INDEX context_profile_rules_profile ON context_profile_rules(profile_id, priority);
CREATE INDEX context_commitments_agent_service ON context_commitments(agent_did, service_id, expires_at);
CREATE INDEX context_disclosures_agent ON context_disclosures(agent_did, created_at DESC);
CREATE INDEX context_disclosures_service ON context_disclosures(service_id, created_at DESC);
CREATE INDEX context_mismatch_events_service ON context_mismatch_events(service_id, created_at DESC);
CREATE INDEX context_mismatch_events_severity ON context_mismatch_events(severity, resolved);
```

---

## The Context Matching Flow

This is the complete sequence for every agent-to-service context transaction.

```
AGENT                    LAYER 4                    SERVICE
  │                          │                          │
  │── POST /context/match ──>│                          │
  │   { agent_did,           │                          │
  │     service_id,          │                          │
  │     session_assertion,   │ 1. Verify session        │
  │     requested_fields }   │    assertion (L2)        │
  │                          │ 2. Load service manifest │
  │                          │    context block (L1)    │
  │                          │ 3. Load agent profile    │
  │                          │ 4. Check trust score     │
  │                          │    meets threshold (L3)  │
  │                          │ 5. Detect mismatches     │
  │                          │ 6. Evaluate profile      │
  │                          │    rules against fields  │
  │                          │ 7. Generate disclosure   │
  │                          │    package               │
  │<── MatchResult ──────────│                          │
  │   { permitted,           │                          │
  │     withheld,            │                          │
  │     committed,           │                          │
  │     commitment_ids }     │                          │
  │                          │                          │
  │── POST /context/disclose>│                          │
  │   { commitment_ids,      │                          │
  │     service_id }         │ 8. Confirm service       │
  │                          │    trust_tier >= 3       │
  │                          │ 9. Release nonces        │
  │                          │ 10. Log disclosure       │
  │<── DisclosurePackage ────│                          │
  │   { fields (plaintext),  │                          │
  │     nonces for           │                          │
  │     committed fields }   │                          │
  │                          │                          │
  │──────────────────────────│──── context payload ────>│
  │                          │                          │
```

**Trust threshold for context disclosure:**
- sensitivity_tier 1-2 fields: trust_tier >= 2 (domain verified)
- sensitivity_tier 3 fields: trust_tier >= 3 (capability probed) + HITL approval
- sensitivity_tier 4 fields: trust_tier >= 4 (ledger attested) + HITL approval

---

## API Specification - New Endpoints Only

Base URL: `https://api.agentledger.io/v1` (local: `http://localhost:8000/v1`)
All endpoints require `X-API-Key` or Bearer VC token. Same auth as Layers 1-3.

---

### POST /context/profiles
Create a context profile for an agent.

**Request body:**
```json
{
  "agent_did": "did:key:z6Mk...",
  "profile_name": "default",
  "default_policy": "deny",
  "rules": [
    {
      "priority": 10,
      "scope_type": "domain",
      "scope_value": "HEALTH",
      "permitted_fields": ["user.name", "user.insurance_id"],
      "denied_fields": ["user.ssn", "user.full_medical_history"],
      "action": "permit"
    },
    {
      "priority": 20,
      "scope_type": "trust_tier",
      "scope_value": "4",
      "permitted_fields": ["user.name", "user.email", "user.dob"],
      "denied_fields": [],
      "action": "permit"
    }
  ]
}
```

**Response 201:**
```json
{
  "profile_id": "uuid",
  "agent_did": "did:key:z6Mk...",
  "profile_name": "default",
  "default_policy": "deny",
  "rule_count": 2,
  "created_at": "ISO 8601"
}
```

**Response 422:** Validation error - invalid field names, unknown domain, etc.

---

### GET /context/profiles/{agent_did}
Retrieve the active context profile for an agent including all rules.

**Response 200:** Full profile object with rules array sorted by priority.
**Response 404:** No active profile for this agent_did.

---

### PUT /context/profiles/{agent_did}
Replace the active profile's rules. Does not create a new profile - updates in place.

**Request body:** Same as POST minus `agent_did` (taken from path).
**Response 200:** Updated profile object.

---

### POST /context/match
Core matching endpoint. Evaluates whether a service's context requirements can
be satisfied given the agent's profile and current trust state.

**Request body:**
```json
{
  "agent_did": "did:key:z6Mk...",
  "service_id": "uuid",
  "session_assertion": "JWT string (from Layer 2)",
  "requested_fields": ["user.name", "user.email", "user.insurance_id"]
}
```

**Processing (in order):**
1. Verify session_assertion signature and expiry (Layer 2 dependency)
2. Load service manifest context block from Layer 1 DB
3. Detect mismatch: `requested_fields` must be subset of manifest declared fields
   - If not: log `context_mismatch_events`, return 400 with mismatch detail
4. Check trust score threshold per field sensitivity tier
   - If service trust score insufficient for any required field: return 403
5. Load agent context profile, evaluate rules in priority order
6. Classify each field: permitted / withheld / committed
   - withheld: profile rule denies this field to this service
   - committed: profile permits but sensitivity_tier >= 3 (commitment scheme applied)
7. Generate HMAC commitments for committed fields
8. Return MatchResult

**Response 200:**
```json
{
  "match_id": "uuid",
  "session_assertion_id": "uuid",
  "permitted_fields": ["user.name", "user.email"],
  "withheld_fields": ["user.ssn"],
  "committed_fields": ["user.insurance_id"],
  "commitment_ids": ["uuid", "uuid"],
  "mismatch_detected": false,
  "trust_tier_at_match": 3,
  "trust_score_at_match": 82.4,
  "can_disclose": true
}
```

**Response 400:** Mismatch detected - service requested undeclared fields.
**Response 403:** Service trust score insufficient for requested field sensitivity.

---

### POST /context/disclose
Execute disclosure. Releases committed field nonces to the agent. Agent then
transmits the context payload (with nonces) directly to the service.
AgentLedger does NOT proxy the context payload to the service.

**Request body:**
```json
{
  "match_id": "uuid",
  "agent_did": "did:key:z6Mk...",
  "service_id": "uuid",
  "commitment_ids": ["uuid", "uuid"]
}
```

**Processing:**
1. Re-verify service trust_tier meets threshold (trust state could change between
   match and disclose)
2. Release nonces for committed fields
3. Write append-only record to `context_disclosures`
4. Return disclosure package

**Response 200:**
```json
{
  "disclosure_id": "uuid",
  "permitted_fields": {
    "user.name": "Michael Williams",
    "user.email": "michael@example.com"
  },
  "committed_field_nonces": {
    "user.insurance_id": "nonce_string_for_service_to_verify"
  },
  "disclosed_at": "ISO 8601",
  "expires_at": "ISO 8601"
}
```

**Response 403:** Trust tier dropped between match and disclose - disclose blocked.
**Response 410:** match_id expired (match TTL is 5 minutes).

---

### GET /context/disclosures/{agent_did}
Full audit trail of all context disclosures for an agent.

**Query params:** `service_id`, `from_date`, `to_date`, `limit`, `offset`
**Response 200:** Paginated disclosure records (field values redacted in response -
audit log records field *names* only, not values).

---

### POST /context/revoke/{disclosure_id}
GDPR right to erasure. Marks a disclosure as erased. Does not delete the record
(audit trail must remain) but nulls the field metadata and flags `erased=true`.

**Response 200:** `{ "disclosure_id": "uuid", "erased_at": "ISO 8601" }`
**Response 404:** Disclosure not found for this agent_did.

---

### GET /context/mismatches
List context mismatch events. Admin endpoint.

**Query params:** `service_id`, `severity`, `resolved`, `limit`, `offset`
**Response 200:** Paginated mismatch events.

---

### POST /context/mismatches/{id}/resolve
Mark a mismatch as reviewed and resolved (or escalated to trust revocation).

**Request body:** `{ "resolution_note": "string", "escalate_to_trust": boolean }`
If `escalate_to_trust: true`, the endpoint calls the Layer 3 revocation API
automatically and records the revocation reference.

---

### GET /context/compliance/export/{agent_did}
GDPR/CCPA compliance export. Returns a PDF audit package for a specific agent
covering all disclosures, profile rules, and erasure records.

**Response 200:** `application/pdf`
**Use case:** User requests "what did AgentLedger share about me and with whom."

---

## Profile Rule Evaluation Algorithm

Rules are evaluated in ascending priority order (lowest number = first evaluated).
First matching rule wins. Default policy applies if no rule matches.

```python
def evaluate_profile(
    rules: list[ProfileRule],
    field: str,
    service: Service,
    default_policy: str  # 'deny' | 'allow'
) -> str:  # 'permit' | 'withhold' | 'commit'

    for rule in sorted(rules, key=lambda r: r.priority):
        if not rule_matches_service(rule, service):
            continue

        # Explicit deny always wins regardless of action
        if field in rule.denied_fields:
            return 'withhold'

        if field in rule.permitted_fields:
            sensitivity = get_sensitivity_tier(field)
            if sensitivity >= 3:
                return 'commit'   # permit but via commitment scheme
            return 'permit'

    # No rule matched
    if default_policy == 'allow':
        sensitivity = get_sensitivity_tier(field)
        if sensitivity >= 3:
            return 'commit'
        return 'permit'

    return 'withhold'  # default_policy == 'deny'


def rule_matches_service(rule: ProfileRule, service: Service) -> bool:
    if rule.scope_type == 'domain':
        return service.ontology_domain == rule.scope_value
    if rule.scope_type == 'trust_tier':
        return service.trust_tier >= int(rule.scope_value)
    if rule.scope_type == 'service_did':
        return service.did == rule.scope_value
    if rule.scope_type == 'sensitivity':
        return True  # sensitivity rules apply globally
    return False
```

---

## Mismatch Detection Algorithm

Run at the start of every `/context/match` call.

```python
def detect_mismatch(
    requested_fields: list[str],
    manifest_context: ManifestContextBlock
) -> MismatchResult:

    declared = set(manifest_context.required + manifest_context.optional)
    requested = set(requested_fields)
    over_requested = requested - declared

    if not over_requested:
        return MismatchResult(detected=False)

    severity = 'warning'
    for field in over_requested:
        if get_sensitivity_tier(field) >= 3:
            severity = 'critical'
            break

    return MismatchResult(
        detected=True,
        over_requested_fields=list(over_requested),
        severity=severity
    )
```

---

## HMAC Commitment Scheme (v0.1)

```python
import hmac
import hashlib
import secrets

def generate_commitment(field_value: str) -> tuple[str, str]:
    """Returns (commitment_hash, nonce)."""
    nonce = secrets.token_hex(32)
    commitment = hmac.new(
        key=nonce.encode(),
        msg=field_value.encode(),
        digestmod=hashlib.sha256
    ).hexdigest()
    return commitment, nonce

def verify_commitment(
    commitment_hash: str,
    nonce: str,
    field_value: str
) -> bool:
    """Service uses this to verify nonce + value matches commitment."""
    expected = hmac.new(
        key=nonce.encode(),
        msg=field_value.encode(),
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(commitment_hash, expected)
```

Nonces are stored server-side in `context_commitments`. They are released via
`POST /context/disclose` only after trust re-verification passes. Nonces expire
with the commitment (15-minute TTL). Expired commitments require a new match.

---

## Layer 3 Integration Points Activated by Layer 4

| # | Integration Point | Layer 3 State | Layer 4 Change |
|---|---|---|---|
| 1 | Trust score threshold gate | Computed but not enforcement point | Layer 4 blocks disclosure for fields where service trust score < required threshold per sensitivity_tier |
| 2 | Trust tier check | Stored in `services.trust_tier` | Layer 4 re-checks trust_tier at disclose time (not just match time) - trust state can change |
| 3 | Revocation escalation | Manual auditor action | `POST /context/mismatches/{id}/resolve?escalate_to_trust=true` calls Layer 3 revocation API automatically |
| 4 | Audit chain | Stores agent action records | Context disclosures write into the audit chain with field names (not values) for compliance |
| 5 | Attestation score | Drives trust score | High attestation_score unlocks commitment nonce release for sensitivity_tier 3 fields |

---

## Threat Model - Layer 4 Additions

Layers 1-3 defined 10 threats. Layer 4 adds 4 more:

| # | Threat | Attack | Severity | Mitigation |
|---|--------|--------|----------|------------|
| 11 | Context Hoarding | Service stores user context beyond declared `data_retention_days` | Critical | Compliance exports provide evidence of violation; mismatch events feed into trust revocation pipeline |
| 12 | Context Laundering | Service passes disclosed context to an undeclared third party despite `data_sharing: none` | Critical | Commitment scheme limits field value exposure until trust verification; audit trail enables post-hoc liability attribution via Layer 6 |
| 13 | Profile Inference | Service infers undisclosed fields from patterns in permitted fields (e.g. inferring diagnosis from pharmacy refill requests) | High | Sensitivity-tier gating on field combinations; future v0.2 ZKP circuits prevent inference from commitment metadata |
| 14 | Commitment Grinding | Attacker tries to reverse HMAC commitment by brute-forcing field values | Medium | Nonces are 256-bit random; HMAC-SHA256 is preimage resistant; commitment TTL is 15 minutes limiting attack window |

---

## GDPR / CCPA Compliance Architecture

Layer 4 enforces data minimization by protocol - not by policy. The compliance
posture baked into v0.1:

| Requirement | Mechanism |
|---|---|
| Data minimization | Matching engine strips fields not in `permitted_fields` before disclosure package is generated - no voluntary restriction required |
| Purpose limitation | `ontology_tag` in session assertion binds context to a specific declared purpose; disclosed fields are logged with that tag |
| Right of access | `GET /context/disclosures/{agent_did}` returns full audit trail |
| Right to erasure | `POST /context/revoke/{disclosure_id}` marks disclosure erased; field names retained for audit integrity, values nulled |
| Data portability | `GET /context/compliance/export/{agent_did}` generates PDF audit package |
| Breach notification | `context_mismatch_events` with `severity=critical` trigger admin alerts |
| Consent records | `context_disclosures` records which profile rule permitted each disclosure |

---

## Build Order

### Phase 1 - Schema + Profile CRUD
- Migration 005 with all four new tables
- Pydantic models in `api/models/context.py`
- `api/services/context_profiles.py` - create, read, update profile + rules
- `POST /context/profiles`, `GET /context/profiles/{agent_did}`, `PUT /context/profiles/{agent_did}`
- Seed two default profiles in test fixtures

**Done when:** POST /context/profiles with a two-rule HEALTH domain profile
returns 201, and GET retrieves it with rules sorted by priority.

---

### Phase 2 - Mismatch Detection
- `api/services/context_mismatch.py` - detect_mismatch algorithm
- `context_mismatch_events` write path
- `GET /context/mismatches`, `POST /context/mismatches/{id}/resolve`
- Layer 3 revocation call wired into resolve when `escalate_to_trust=true`

**Done when:** A match request with a field not in the service manifest returns
400 with mismatch detail, and the event appears in GET /context/mismatches.

---

### Phase 3 - Matching Engine
- `api/services/context_matcher.py` - full evaluate_profile algorithm
- `api/services/context_disclosure.py` - HMAC commitment generation
- `POST /context/match` - full flow including trust threshold checks
- Redis cache for match results (TTL: 5 min, matching commitment TTL)

**Done when:** A match request returns a MatchResult with correctly classified
permitted / withheld / committed fields for a HEALTH domain service with a
sensitivity_tier 3 field.

---

### Phase 4 - Selective Disclosure
- `POST /context/disclose` - nonce release + audit log write
- Trust tier re-verification at disclose time
- `context_disclosures` append-only write
- `GET /context/disclosures/{agent_did}` - paginated audit trail
- `POST /context/revoke/{disclosure_id}` - GDPR erasure

**Done when:** Full match -> disclose flow completes for a committed field,
nonce is released, and disclosure appears in the audit trail.

---

### Phase 5 - Compliance Export
- `api/services/context_compliance.py` - PDF generation via reportlab
- `GET /context/compliance/export/{agent_did}` -> PDF response
- PDF includes: profile rules in effect, all disclosures (field names only),
  any mismatches, any erasure records

**Done when:** Export endpoint returns a valid PDF covering test disclosures.

---

### Phase 6 - Hardening
- Redis profile cache (TTL: 60s, invalidated on PUT)
- Trust score re-check at disclose time uses Redis cache from Layer 3
- Rate limiting: 100 match requests per agent_did per minute
- 80%+ test coverage for all new modules
- Load test: `POST /context/match` at 100 concurrent, p95 < 300ms

---

## Acceptance Criteria (10 gates)

```
[ ] POST /context/profiles creates profile with rules for a registered agent DID
[ ] Profile rules evaluate correctly: permitted field flows, denied field withheld
[ ] Mismatch detected when service requests field not in manifest context block
[ ] Critical mismatch (sensitivity_tier >= 3 over-request) logged with severity=critical
[ ] POST /context/match returns correct permit/withhold/committed classification
[ ] sensitivity_tier 3 field returns commitment_id, not plaintext value
[ ] POST /context/disclose releases nonce only when service trust_tier >= 3
[ ] POST /context/disclose returns 403 if service trust_tier dropped since match
[ ] GET /context/disclosures returns full audit trail (field names, not values)
[ ] GET /context/compliance/export returns valid PDF for a given agent_did
[ ] POST /context/match p95 < 300ms @ 100 concurrent requests
```

---

## What Layer 4 Does NOT Include

- Full ZKP circuits (circom, snark.js) - deferred to v0.2
- OAuth2 user authorization for profile creation - v0.2
- Payment-gated context tiers - Layer 5
- Workflow-level context bundling - Layer 5
- Insurance liability for context disclosure failures - Layer 6
- Cross-registry context profile federation - Layer 5+

---

## Layer 5 Integration Points (for the next session)

| # | Integration Point | Where in Layer 4 | What Layer 5 Adds |
|---|---|---|---|
| 1 | Workflow context bundles | `context_disclosures` | Layer 5 groups multiple single-service disclosures into a workflow-level context bundle with a single user approval |
| 2 | Context fit ranking signal | `context_matcher.py` - returns `can_disclose: bool` | Layer 5 ranker uses context fit as a quality signal for workflow step selection |
| 3 | Mismatch -> workflow abort | `context_mismatch_events` | Layer 5 orchestrator checks for critical mismatches before advancing workflow steps |
| 4 | Profile inheritance | `context_profiles` | Layer 5 introduces workflow-scoped profiles that override agent defaults for specific workflow execution |
| 5 | Compliance bundle | `GET /context/compliance/export` | Layer 6 combines context compliance export with audit chain records into a single regulatory package |

---

*This spec is the source of truth for Layer 4. Update it before changing any
behavior described here.*
