# Lesson 20 — The Final Debrief: Full Layer 2 Flow & Interview Readiness

> **Beginner frame:** Layer 2 answers "who is acting, and are they still allowed to act?" This debrief ties keys, DIDs, credentials, sessions, revocation, and human approval into one identity story.

**Layer:** 2 — Identity & Credentials  
**Source:** `spec/LAYER2_COMPLETION.md`, all Layer 2 files  
**Prerequisites:** Lessons 11–19 — this lesson synthesizes everything  
**Estimated time:** 90 minutes

---

## Welcome

A debrief after an operation reviews the full sequence: what each player did, what signals were exchanged, what the outcome was, and what would break each step. It's not about re-reading the manual — it's about understanding the system as a whole.

This lesson does exactly that for Layer 2. You've studied each component in isolation. Now you'll trace a complete end-to-end flow from agent registration through session redemption, map the test coverage, understand the hardening decisions, and prepare to answer the five canonical Layer 2 interview questions.

---

## What Layer 2 Shipped

From `spec/LAYER2_COMPLETION.md`:

| Capability | Core file | Lesson |
|---|---|---|
| Ed25519 sign/verify, canonical JSON | `api/services/crypto.py` | 11 |
| `did:key` and `did:web` derivation | `api/services/did.py` | 12 |
| JWT VC issuance and session assertions | `api/services/credentials.py` | 13 |
| Agent registration, verification, revocation | `api/services/identity.py` | 14 |
| Session request, poll, redeem | `api/services/sessions.py` | 15 |
| Service `did:web` activation | `api/services/service_identity.py` | 16 |
| HITL approval queue | `api/services/authorization.py` | 17 |
| Request/response schemas, 13 endpoints | `api/models/identity.py`, `api/routers/identity.py` | 18 |
| Celery background workers | `crawler/tasks/expire_*.py`, `revalidate_*.py` | 19 |

---

## The Full End-to-End Flow

### Phase 1 — Agent comes online

```
Agent generates Ed25519 keypair
    │
    ├── private_jwk: {"kty":"OKP","crv":"Ed25519","x":"...","d":"..."}
    └── public_jwk:  {"kty":"OKP","crv":"Ed25519","x":"..."}

Agent derives DID from public key
    │
    └── did:key:z6Mk{base58btc(0xed01 + raw_key_bytes)}

Agent builds DID document
    │
    └── {"id": did, "verificationMethod": [...], "authentication": [...], "assertionMethod": [...]}

Agent builds registration proof payload
    │
    └── {did, did_document, agent_name, capability_scope, risk_tier, nonce, created_at}

Agent signs proof payload
    │
    └── signature = Ed25519(canonical_json(payload), private_key)

Agent calls POST /v1/identity/agents/register
    │
    └── {did, did_document, agent_name, ..., proof: {nonce, created_at, signature}}
```

**Server-side (identity.py:register_agent):**

```
1. Proof freshness check (abs(age) <= proof_nonce_ttl_seconds)
2. extract_public_jwk_from_did_document(did_document, expected_did=did)
3. did_key_from_public_jwk(public_jwk) == did  ← DID cross-check
4. verify_json_signature(proof_payload, signature, public_jwk)  ← key control proof
5. Redis SET NX nonce replay guard
6. SELECT agent_identities WHERE did = :did  ← duplicate check
7. issue_agent_credential(did, agent_name, scope, risk_tier) → (jwt, expires_at)
8. credential_hash = sha256(jwt)
9. INSERT agent_identities (did, ..., credential_hash, credential_expires_at)
10. RETURN {did, credential_jwt, credential_expires_at, did_document, issuer_did}
```

The agent stores `credential_jwt`. This is its identity bearer token for the lifetime of the credential.

---

### Phase 2 — Service activates its identity

```
Service operator publishes:
    https://example.com/.well-known/did.json
    └── {"id": "did:web:example.com", "verificationMethod": [...], ...}

Service signs its current manifest with the private key matching the DID doc
    signature = Ed25519(canonical_json(manifest_without_signature), service_private_key)

Operator calls POST /v1/identity/services/example.com/activate
```

**Server-side (service_identity.py:activate_service_identity):**

```
1. SELECT services WHERE domain = 'example.com'
2. trust_tier >= 2?  ← must be domain-verified first
3. SELECT manifests WHERE is_current=true
4. validate_signed_manifest(manifest):
   a. manifest.identity.did == did:web:example.com
   b. resolve_service_did_document('example.com')  ← HTTPS fetch + Redis cache
   c. extract verification method from DID doc
   d. verification method in authentication AND assertionMethod?
   e. manifest.public_key == DID doc JWK?
   f. verify_json_signature(manifest_without_signature, signature, did_doc_jwk)
5. UPDATE services SET public_key=..., last_verified_at=NOW()
6. recompute_service_trust()
7. INSERT crawl_events ('service_identity_activated')
```

After this, `last_verified_at` is non-null — session requests to this service are now permitted.

---

### Phase 3 — Agent requests a session (low-sensitivity path)

```
Agent calls POST /v1/identity/sessions/request
    Authorization: Bearer <credential_jwt>
    Body: {service_domain, ontology_tag, request_context, proof}
```

**Server-side:**

```
1. require_bearer_credential:
   a. Parse Authorization header
   b. verify_agent_credential_async(jwt)  ← EdDSA verify + claim checks
   c. Check Redis revocation SET (SISMEMBER)
   d. SELECT agent_identities WHERE did=:did
   e. UPDATE last_seen_at
   f. Return AgentCredentialPrincipal

2. sessions.request_session():
   a. Proof freshness check
   b. verify_json_signature(session_proof_payload, signature, principal.public_key_jwk)
   c. Redis SET NX nonce replay guard
   d. _scope_allows(principal.capability_scope, ontology_tag)?
   e. SELECT services JOIN service_capabilities JOIN ontology_tags
      WHERE domain=:service_domain AND ontology_tag=:tag AND is_active=true
   f. sensitivity_tier >= 3?

   LOW RISK PATH (tier < 3):
   g. issue_session_assertion(agent_did, service_did, service_id, ontology_tag)
      → (assertion_jwt, jti, expires_at)
   h. INSERT session_assertions (assertion_jti, agent_did, service_id, ..., was_used=false)
   i. RETURN {status:"issued", assertion_jwt, service_did, expires_at}
```

The agent delivers `assertion_jwt` to the service.

---

### Phase 3 (alternate) — High-sensitivity path

```
   HIGH RISK PATH (tier >= 3):
   g. INSERT authorization_requests (status='pending', expires_at=NOW()+ttl)
   h. INSERT crawl_events ('authorization_requested')
   i. dispatch_authorization_webhook('authorization.pending', {...})
   j. RETURN {status:"pending_approval", authorization_request_id, expires_at}

Operator reviews queue → GET /v1/authorization/pending
Operator approves → POST /v1/authorization/approve/{id}

   Server-side:
   k. SELECT authorization_requests FOR UPDATE
   l. Re-validate agent status + service status + expiry
   m. issue_session_assertion(..., authorization_ref=req_id, ttl=approved_session_ttl)
   n. INSERT session_assertions (authorization_ref=req_id)
   o. UPDATE authorization_requests SET status='approved', approver_id=..., decided_at=NOW()
   p. INSERT crawl_events ('authorization_approved')
   q. dispatch_authorization_webhook('authorization.approved', {...})
   r. RETURN {assertion_jwt, ...}
```

---

### Phase 4 — Service redeems the assertion

```
Service calls POST /v1/identity/sessions/redeem
    {assertion_jwt, service_domain: "example.com"}
```

**Server-side (sessions.py:redeem_session):**

```
1. verify_session_assertion(assertion_jwt)
   └── EdDSA verify + exp + iss + aud + jti checks

2. aud == did:web:example.com?  ← audience check

3. SELECT services WHERE domain='example.com' AND is_active=true
4. claims["service_id"] == services.id?  ← service binding check

5. UPDATE session_assertions
   SET was_used=true, used_at=NOW()
   WHERE assertion_jti=:jti AND service_id=:id
     AND was_used=false AND expires_at>NOW()
   RETURNING agent_did, ontology_tag, authorization_ref

6. INSERT crawl_events ('session_redeemed')
7. RETURN {status:"accepted", agent_did, ontology_tag}
```

The service now knows: which agent is calling, what capability is being invoked, and (if applicable) which human approved the access.

---

## Chain of Custody Summary

```
Agent key           → signs registration proof
                       └── proves key control
                       └── server verifies → issues JWT VC

JWT VC              → bearer token for all authenticated calls
                       └── server verifies (async, thread pool)
                       └── checks Redis revocation SET
                       └── checks DB for is_revoked, is_active

Session proof       → agent signs each session request
                       └── proves this agent made this request
                       └── nonce prevents replay

Session assertion   → server issues for specific service+capability
                       └── audience binding (aud=service_did)
                       └── service_id binding (UUID in claims)
                       └── one-use (was_used atomic UPDATE)

Service DID doc     → published at HTTPS endpoint
                       └── proves domain control
                       └── manifest signature proves key control
                       └── server cross-checks both

HITL approval       → human decision recorded immutably
                       └── FOR UPDATE prevents duplicate approval
                       └── authorization_ref links session to approval
```

---

## Acceptance Criteria (all 10 passed)

From `spec/LAYER2_COMPLETION.md`:

| # | Criterion |
|---|-----------|
| 1 | `POST /identity/agents/register` issues a signed VC for a valid `did:key` registration |
| 2 | `POST /identity/agents/verify` performs online verification with revocation checks |
| 3 | Bearer VC authentication gates `/identity/sessions/request` |
| 4 | Revoked credentials are rejected on verify and bearer-auth paths |
| 5 | Service `did:web` resolution fetches and caches `/.well-known/did.json` |
| 6 | Session assertions are scoped to one service DID and ontology tag |
| 7 | `sensitivity_tier >= 3` requests enter the HITL queue |
| 8 | Approval issues a linked session and denial blocks issuance |
| 9 | Proof nonce replay protection prevents detached proof reuse |
| 10 | Verify endpoint stays under 200ms p95 at 100 concurrent users |

---

## Performance Snapshot

From `spec/LAYER2_COMPLETION.md §6`:

| Metric | Value |
|---|---|
| Endpoint | `POST /v1/identity/agents/verify` |
| Concurrency | 100 users |
| Total requests | 5,350 / 30s |
| Failures | 0 |
| Median | 61ms |
| p95 | 110ms |
| p99 | 160ms |

**Five hardening changes that made the final run valid:**

1. **Cached issuer key material at module scope** — `_cached_public_key` in `credentials.py`; eliminates key-parse overhead on every verify call
2. **Reduced revocation lookup to `SISMEMBER` + per-DID fallback** — single round-trip for the common (not revoked) case
3. **Removed lazy revocation prewarm from verify path** — prewarm now happens at startup only, not inline
4. **Removed per-request `last_seen_at` write from verify** — `authenticate_agent_credential` updates `last_seen_at`, but the public `verify` endpoint skips it (read-only hot path)
5. **Offloaded signature verification from the async event loop** — `verify_agent_credential_async` uses `run_in_executor` to keep Ed25519 math off the event loop

---

## Test Coverage Map

Layer 2 test files with their focus:

| File | What it tests |
|---|---|
| `tests/test_api/test_identity.py` | Agent register, verify, lookup, revoke; service DID routes; authorization queue and approve/deny |
| `tests/test_api/test_identity_service.py` | Revocation cache (SET + per-DID key), proof nonce replay rejection, prewarm behavior |
| `tests/test_api/test_sessions.py` | Session request (low + high sensitivity), poll status, redeem once, replay rejection |
| `tests/test_api/test_sessions_service.py` | Session service unit tests: scope matching, proof verification, audience binding |
| `tests/test_api/test_authorization.py` | HITL queue listing, approve idempotency, deny idempotency, expired request rejection |
| `tests/test_api/test_service_identity.py` | `did:web` resolution + caching, `validate_signed_manifest` (5 steps), activation flow |
| `tests/test_api/test_crypto.py` | Ed25519 round-trips, canonical JSON, DID derivation, JWK encode/decode |
| `tests/test_crawler/test_expire_identity_records.py` | Authorization expiry, session assertion deletion, batch counts |
| `tests/test_crawler/test_revalidate_service_identity.py` | Per-service pass/fail isolation, `force_refresh` behavior, crawl_events logging |

Total Layer 2-focused tests: **34 passed** (within the full suite of 213 passing).

---

## Configuration Reference

From `spec/LAYER2_COMPLETION.md §9`:

| Variable | Default | Notes |
|---|---|---|
| `ISSUER_DID` | `did:web:agentledger.io` | The `iss` claim in all issued JWTs |
| `ISSUER_PRIVATE_JWK` | (empty — required) | Ed25519 JWK; must be set for any Layer 2 operation |
| `CREDENTIAL_TTL_SECONDS` | `31536000` (365 days) | Agent VC expiry |
| `PROOF_NONCE_TTL_SECONDS` | `60` | Registration and session proof freshness window |
| `SESSION_ASSERTION_TTL_SECONDS` | `300` (5 min) | Standard session expiry |
| `APPROVED_SESSION_TTL_SECONDS` | `900` (15 min) | HITL-approved session expiry |
| `AUTHORIZATION_REQUEST_TTL_SECONDS` | `300` | Pending HITL request timeout |
| `REVOCATION_CACHE_TTL_SECONDS` | `300` | Redis revocation cache TTL |
| `DID_WEB_CACHE_TTL_SECONDS` | `600` (10 min) | Redis `did:web` document cache TTL |
| `AUTHORIZATION_WEBHOOK_URL` | (empty) | Outbound HITL webhook; optional |
| `AUTHORIZATION_WEBHOOK_SECRET` | (empty) | HMAC-SHA256 signing key for webhooks |
| `AUTHORIZATION_WEBHOOK_TIMEOUT_SECONDS` | `3.0` | Webhook delivery timeout |

---

## Threat Model: Four Layer 2 Attack Surfaces

### 1. Spoofed Registration
**Attack:** An adversary submits another agent's DID and claims ownership.  
**Mitigation:** Five-step registration proof in `register_agent()`:
- Proof freshness prevents old captured proofs
- DID cross-check (re-derive from key) prevents DID/key mismatch
- Ed25519 signature proves key control
- Nonce Redis SET NX prevents replay

**Code:** `identity.py:register_agent`, lines 202–334

### 2. Revoked Credential Used as Bearer Token
**Attack:** An agent whose credential has been revoked continues using the issued JWT (which is still cryptographically valid).  
**Mitigation:** Online revocation check in `authenticate_agent_credential()`:
- Tier 1: Redis SET `SISMEMBER` (sub-millisecond)
- Tier 2: Per-DID Redis key (metadata)
- Tier 3: Database `is_revoked` column

**Code:** `identity.py:authenticate_agent_credential`, lines 387–463

### 3. Session Assertion Replay
**Attack:** A session assertion token intercepted in transit is submitted a second time.  
**Mitigation:** Atomic one-use UPDATE in `redeem_session()`:
```sql
WHERE assertion_jti=:jti AND was_used=false AND expires_at>NOW()
```
The `was_used=false` condition in the WHERE clause means the second submission matches zero rows.

**Code:** `sessions.py:redeem_session`, lines 498–515

### 4. Unauthorized Service Identity
**Attack:** A service submits a manifest with a falsified `did:web` identity claiming control of a domain it doesn't own.  
**Mitigation:** Chain of authority in `validate_signed_manifest()`:
- Server independently fetches the DID document from the HTTPS endpoint
- Manifest public key must exactly match the DID document JWK
- Manifest signature is verified against the DID document's key (not the manifest's own claim)

**Code:** `service_identity.py:validate_signed_manifest`, lines 209–260

---

## Interview Prep: Five Canonical Layer 2 Questions

### Q1: Walk me through what happens when an agent calls `POST /v1/identity/sessions/request`.

**Answer (key points, 2-3 minutes):**

1. The `require_bearer_credential` dependency fires first: it parses the `Authorization: Bearer` header, calls `verify_agent_credential_async` (Ed25519 verify + claim checks, off the event loop via `run_in_executor`), checks the Redis revocation SET, and then queries the database for active status. This produces an `AgentCredentialPrincipal` with the agent's scope and public key.

2. `request_session()` verifies the session proof: the agent signs the request payload with its own private key, and the server verifies using the public key from the principal (not the issuer's key). This ensures the agent who holds the credential is the same one making the request.

3. The server looks up the service by domain, joining to `service_capabilities` and `ontology_tags` to get the capability's sensitivity tier.

4. If `sensitivity_tier < 3`: issue a JWT session assertion immediately (5-minute TTL), store it in `session_assertions`, return `status="issued"` with the JWT.

5. If `sensitivity_tier >= 3`: create an `authorization_requests` row (`status="pending"`), fire the HITL webhook, return `status="pending_approval"` with `202 Accepted`.

### Q2: How does AgentLedger prevent the same session assertion from being redeemed twice, even under concurrent requests?

**Answer:**

The `redeem_session` function uses an atomic SQL UPDATE with a compound WHERE clause:

```sql
UPDATE session_assertions
SET was_used = true, used_at = NOW()
WHERE assertion_jti = :jti
  AND service_id = :service_id
  AND was_used = false
  AND expires_at > NOW()
RETURNING agent_did, ontology_tag
```

PostgreSQL guarantees that only one concurrent UPDATE can win the row lock when two requests target the same `assertion_jti` simultaneously. The winning UPDATE sets `was_used=true`; the losing UPDATE's WHERE clause (`was_used = false`) no longer matches and returns zero rows. There is no SELECT-then-UPDATE race condition because the validity check and the state change happen atomically in one statement.

### Q3: What is the purpose of the `authorization_ref` field on a session assertion?

**Answer:**

`authorization_ref` links a session assertion back to the `authorization_requests` row that authorized it. It's only populated for sessions that went through the HITL approval flow (sensitivity tier ≥ 3). When a service redeems the assertion, it receives `authorization_ref` in the response — allowing it to:

1. Look up the human decision record (who approved it, when, with what context)
2. Implement capability-level audit trails that include the human approval
3. Revoke the approval retroactively if needed (a downstream service can check whether the authorization record was later denied or flagged)

For immediate (low-risk) sessions, `authorization_ref` is null — no human decision is in the chain.

### Q4: Why does the service `did:web` validation use the key from the DID document rather than the key from the manifest's own `public_key` field, even though the validator checks they match?

**Answer:**

The manifest's `public_key` field is a claim by the submitting party. The DID document at `https://{domain}/.well-known/did.json` is fetched independently by the server from the service's HTTPS endpoint.

Using the DID document's JWK as the verification authority means:
- The verification is grounded in independent evidence (the HTTPS response), not the claim being verified
- An attacker who can only modify the manifest body can't substitute their own key — they'd also need to control the HTTPS endpoint at the service domain
- The check `manifest.public_key == did_doc_jwk` is an additional consistency assertion, but the *authority* is always the DID document

If the server verified the manifest signature against the manifest's own `public_key`, any party could sign a manifest with any key, include the matching public key in `public_key`, and forge a valid service identity. The double-grounding (DID doc + key match + signature) is what makes the proof meaningful.

### Q5: Why are there two Redis structures for revocation (`identity:revoked_set` and `identity:revoked:{sha}`) instead of just one?

**Answer:**

The two structures serve different query patterns at different cost points:

- **`identity:revoked_set` (Redis SET):** `SISMEMBER` is O(1) in the set size, returns `0` (not revoked) or `1` (revoked) in a single round trip. This is the hot path — checked on every authenticated request. The SET also handles the "not revoked" case gracefully: `SISMEMBER` on a non-existent key returns `0`, so no separate `EXISTS` check is needed.

- **`identity:revoked:{sha}` (Redis string):** A JSON string with `{did, revoked_at, reason_code}`. Only fetched when the SET indicates "revoked" or when the SET is cold (TTL expired). It carries the metadata needed for audit responses and logging without loading the entire SET.

Combining them gives O(1) membership detection on the hot path plus per-DID metadata detail without overfitting either data structure to two very different use cases.

---

## Layer 2 → Layer 3 Bridge

From `spec/LAYER2_COMPLETION.md §10`:

Layer 3 extends Layer 2 without breaking any existing contracts:

1. **Attestation score** — Layer 3 auditors write on-chain attestations. The `attestation_score` that Layer 2's `compute_trust_score()` computes from `has_active_service_identity` becomes the Layer 3 component that weighs multi-auditor blockchain attestations.

2. **Reputation score** — Layer 2's local reputation is 70% session outcomes + 30% federated signals. Layer 3 adds the federated component: cross-registry revocation signals that flow through the federation blocklist.

3. **Revocation publication** — Layer 2 revocation lives in the local database. Layer 3 publishes revocations to the Polygon Amoy blockchain (`AttestationLedger.isGloballyRevoked`) and the federation blocklist endpoint.

The key invariant preserved: every Layer 3 operation that touches a service must first confirm `services.last_verified_at IS NOT NULL` — the same condition Layer 2 set up.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Layer 2 Architecture Summary                                            │
├─────────────────────────────────────────────────────────────────────────┤
│ Cryptographic primitives                                                │
│   Ed25519 (EdDSA) for all signing        PyJWT for JWT encode/decode   │
│   did:key for agents                     did:web for services           │
│   canonical JSON (sort_keys=True)        base58btc for DID encoding     │
│                                                                         │
│ Proof chain (registration)                                              │
│   1. Timestamp freshness   2. DID doc extraction                        │
│   3. DID cross-check       4. Signature verify                          │
│   5. Nonce replay (Redis SET NX)                                        │
│                                                                         │
│ Session lifecycle                                                       │
│   VC (365d) → session proof → assertion JWT (5min) → one-use redeem    │
│   tier < 3: immediate    tier >= 3: HITL queue → human approve/deny    │
│                                                                         │
│ Revocation hot path                                                     │
│   Redis SET (SISMEMBER) → per-DID cache → DB                          │
│   Write order: db.commit() → then Redis update                         │
│                                                                         │
│ Service identity chain of authority                                     │
│   HTTPS DID doc → key match → manifest signature → activation          │
│   Revalidated nightly by background worker (force_refresh=True)        │
│                                                                         │
│ Performance targets (all met)                                           │
│   verify endpoint: p95=110ms @ 100 concurrent, 0 failures              │
│   5 hardening changes: key cache, SISMEMBER, prewarm, skip last_seen,  │
│                        run_in_executor                                  │
│                                                                         │
│ Test suite: 34 Layer 2 tests + 213 total passing                       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## What's Next

You have completed the full Layer 2 curriculum. Layer 3 (Lessons 21–30) builds directly on this foundation:

- **Lesson 21** explains why blockchain is added on top of what you just built
- **Lesson 22** covers how all Layer 3 chain writes go through a single abstraction layer
- **Lesson 25** shows how Layer 3 attestations replace the placeholder `attestation_score` with real multi-auditor evidence
- **Lesson 27** connects the federation blocklist to the Layer 2 revocation system you just studied

Jump to [Lesson 21: The Notary's Seal](Lesson21_TrustArchitecture.md) to continue.
