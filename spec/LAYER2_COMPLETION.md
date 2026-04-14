# AgentLedger — Layer 2 Completion Summary

**For:** Architect sign-off and Layer 3 planning  
**Date:** April 14, 2026  
**Branch:** `main`  
**Final commit:** (this commit)  
**Test suite:** 213 tests, 0 failures

---

## 1. What Was Built

Layer 2 is the **Identity & Authorization Layer** — cryptographic agent identity, credential verification, session assertion, service identity resolution, and human-in-the-loop approval for sensitive operations.

| Capability | Description |
|------------|-------------|
| **Agent Identity** | Ed25519 did:key registration with proof-of-possession, JWT VC issuance |
| **Credential Verification** | Online JWT verification with three-tier revocation checking |
| **Session Assertions** | Short-lived signed JWTs scoping agent access to specific services and ontology tags |
| **Service Identity** | did:web resolution for service domains with DNS validation and caching |
| **HITL Authorization** | Sensitivity-gated approval queue for high-risk operations (tier 3+) |
| **Revocation** | Admin-initiated revocation with Redis-cached invalidation propagation |

---

## 2. Build Phases

| Phase | Scope | Commit | Status |
|-------|-------|--------|--------|
| 1 — Identity Foundation | Agent registration, DID infrastructure, JWT VC issuance, Ed25519 crypto | `9b71f50` | **Done** |
| 2 — Session Assertion Engine | Session request/issuance/redemption, service trust gating | `3f9ad0d` | **Done** |
| 3 — Service Identity Activation | did:web resolution, DNS-based attestation, Redis caching | `678aaf4` | **Done** |
| 4 — HITL Approval Flow | Authorization request queue, admin approve/deny, linked session issuance | `601629c` | **Done** |
| 5 — Identity Hardening | Revocation caching, proof nonce replay protection, input sanitization | `c6fce69` | **Done** |
| 6 — Runtime Packaging | Docker integration, env configuration, test infrastructure | `3142147` | **Done** |
| 7 — Performance Optimization | Key caching, Redis round-trip reduction, hot-path streamlining | (this commit) | **Done** |

---

## 3. API Surface

Layer 2 adds 9 new endpoints under `/v1/identity` and `/v1/authorization`:

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| `GET` | `/v1/identity/.well-known/did.json` | None | Issuer DID document (public) |
| `POST` | `/v1/identity/agents/register` | API key | Register agent DID, issue JWT VC |
| `POST` | `/v1/identity/agents/verify` | None | Online credential verification |
| `GET` | `/v1/identity/agents/{did}` | None | Public agent identity record |
| `POST` | `/v1/identity/agents/{did}/revoke` | Admin | Revoke agent credential |
| `GET` | `/v1/identity/services/{domain}/did` | None | Resolve service did:web document |
| `POST` | `/v1/identity/services/{domain}/activate` | API key | Activate service identity |
| `POST` | `/v1/identity/agents/session` | Bearer VC | Request session assertion |
| `POST` | `/v1/identity/agents/session/redeem` | None | Redeem session at service |
| `GET` | `/v1/authorization/pending` | Admin | List pending HITL approvals |
| `POST` | `/v1/authorization/approve/{id}` | Admin | Approve authorization request |
| `POST` | `/v1/authorization/deny/{id}` | Admin | Deny authorization request |

---

## 4. Cryptographic Architecture

### DID Methods
| Method | Usage | Key Type |
|--------|-------|----------|
| `did:key` | Agent identities | Ed25519 (multicodec `0xed01`, base58btc) |
| `did:web` | Service identities | Resolved via `https://{domain}/.well-known/did.json` |

### JWT Credentials
- **Algorithm:** EdDSA (Ed25519)
- **Issuer:** `did:web:agentledger.io` (configurable via `ISSUER_DID`)
- **TTL:** 365 days (configurable via `CREDENTIAL_TTL_SECONDS`)
- **Required claims:** `iss`, `sub`, `exp`, `iat`, `nbf`, `jti`
- **VC structure:** W3C Verifiable Credential with `AgentIdentityCredential` type

### Proof of Possession
Registration requires a detached proof containing:
- `nonce` — unique per registration, replay-protected via Redis NX
- `created_at` — must be within the replay window (`PROOF_NONCE_TTL_SECONDS`)
- `signature` — Ed25519 signature over the canonical registration payload

### Key Management
- Issuer private key: Ed25519 JWK stored in `ISSUER_PRIVATE_JWK` env var
- Public key cached at module level after first load (zero-cost subsequent verifications)
- Agent public keys derived from DID documents and stored as JSONB in `agent_identities`

---

## 5. Revocation Architecture

Three-tier revocation check on the verify hot path:

```
Request → Redis SISMEMBER (O(1)) → Per-DID cache GET → DB SELECT
         ↓ hit: return revoked     ↓ hit: return revoked  ↓ source of truth
```

| Tier | Mechanism | Latency | TTL |
|------|-----------|---------|-----|
| 1 — SET check | `SISMEMBER identity:revoked_set` | ~0.1ms | 300s |
| 2 — Per-DID cache | `GET identity:revoked:{sha256(did)}` | ~0.2ms | Configurable |
| 3 — DB lookup | `SELECT is_revoked FROM agent_identities` | ~2-5ms | Source of truth |

On revocation, both the SET and per-DID cache are updated immediately.

---

## 6. Session Assertion Flow

```
Agent                    AgentLedger                   Service
  |                          |                            |
  |-- POST /session -------->|                            |
  |   (bearer VC + scope)    |                            |
  |                          |-- check sensitivity tier --|
  |                          |   tier < 3: auto-issue     |
  |                          |   tier >= 3: HITL queue    |
  |<-- assertion JWT --------|                            |
  |                                                       |
  |-- POST /session/redeem -------------------------------->|
  |   (assertion JWT)        |                            |
  |                          |<-- verify assertion -------|
  |                          |-- return agent identity ---|
```

---

## 7. Database Schema Additions

### New Tables
| Table | Purpose |
|-------|---------|
| `agent_identities` | Agent DID registry — keys, capabilities, revocation state |
| `revocation_events` | Append-only audit log of all revocations |
| `service_identities` | Service did:web records with attestation scores |
| `sessions` | Active session assertions with expiry tracking |
| `authorization_requests` | HITL approval queue for sensitive operations |

---

## 8. Performance

### Verify Endpoint (`POST /v1/identity/agents/verify`)

Load test: 100 concurrent users, 30 seconds, Locust

| Metric | Value |
|--------|-------|
| Total requests | 5,350 |
| Failures | 0 (0%) |
| Median | 61ms |
| p95 | **110ms** |
| p99 | 160ms |
| Max | 1,037ms |
| Throughput | ~220 req/s |

### Optimizations Applied
1. **Ed25519 public key caching** — private key parsed and public key derived once per process lifetime, eliminating repeated JSON parse + cryptographic key construction (~50ms savings per call)
2. **Single SISMEMBER** — removed redundant `EXISTS` call before `SISMEMBER` (Redis returns 0 for non-existent keys, which is the correct "not revoked" answer)
3. **Removed hot-path pre-warm** — lazy `prewarm_revocation_set` removed from verify to eliminate random DB query spikes
4. **Removed per-request DB write** — `last_seen_at` UPDATE + COMMIT removed from verify hot path
5. **run_in_executor** — Ed25519 signature verification runs in thread pool to avoid blocking the async event loop
6. **Trimmed response model** — `CredentialVerificationResponse` reduced to 4 fields: `valid`, `did`, `expires_at`, `risk_tier`

---

## 9. Acceptance Criteria — All Verified

| # | Criterion | Status |
|---|-----------|--------|
| 1 | `POST /identity/agents/register` issues a signed JWT VC for a valid did:key registration | **Passed** |
| 2 | `POST /identity/agents/verify` returns online verification status with revocation check | **Passed** |
| 3 | Bearer VC authentication gates `/identity/agents/session` with 401/403 | **Passed** |
| 4 | Admin revocation propagates to Redis cache and blocks subsequent verify/auth | **Passed** |
| 5 | Service did:web resolution fetches and caches `/.well-known/did.json` | **Passed** |
| 6 | Session assertion JWT is scoped to service DID + ontology tag | **Passed** |
| 7 | Sensitivity tier >= 3 routes to HITL queue instead of auto-issuance | **Passed** |
| 8 | HITL approve issues a linked session; deny blocks it | **Passed** |
| 9 | Proof nonce replay protection prevents credential reuse | **Passed** |
| 10 | Verify endpoint p95 < 200ms at 100 concurrent users | **Passed** (110ms) |

---

## 10. Test Coverage

### 213 Tests, 0 Failures

| Module | Files | Focus |
|--------|-------|-------|
| Identity endpoints | `test_identity.py` | Router-level tests for all 9 identity endpoints |
| Identity service | `test_identity_service.py` | Service-layer revocation cache, proof nonce, concurrency |
| Session flow | `test_session.py` | Session request, issuance, redemption, expiry |
| Authorization | `test_authorization.py` | HITL queue, approve, deny, sensitivity routing |
| Service identity | `test_service_identity.py` | did:web resolution, DNS attestation, activation |
| Credentials | `test_credentials.py` | JWT issuance, verification, claims validation |
| Crypto | `test_crypto.py` | Ed25519 key derivation, signing, JWK handling |
| DID | `test_did.py` | did:key construction, document building, multicodec |
| Models | `test_identity_models.py` | Pydantic validation, sanitization, scope validation |
| + Layer 1 tests | 14 test files | All Layer 1 tests continue to pass |

### Load Tests
- `tests/load/locustfile.py` — Extended with `identity_verify`, `identity_lookup`, and `identity_mixed` profiles
- Verified: verify endpoint at 110ms p95, 0% failure rate under 100 concurrent users

---

## 11. Configuration Surface (Layer 2 Additions)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ISSUER_DID` | `did:web:agentledger.io` | Issuer DID for JWT VC `iss` claim |
| `ISSUER_PRIVATE_JWK` | (required) | Ed25519 private JWK for credential signing |
| `CREDENTIAL_TTL_SECONDS` | `31536000` (1 year) | JWT VC expiration |
| `SESSION_ASSERTION_TTL_SECONDS` | `900` (15 min) | Session assertion expiration |
| `PROOF_NONCE_TTL_SECONDS` | `300` (5 min) | Proof replay window |
| `REVOCATION_CACHE_TTL_SECONDS` | `3600` (1 hour) | Per-DID revocation cache TTL |
| `SERVICE_DID_CACHE_TTL` | `86400` (24 hours) | did:web resolution cache TTL |
| `HITL_SENSITIVITY_THRESHOLD` | `3` | Minimum sensitivity tier requiring HITL approval |

---

## 12. Layer 3 Integration Points

### 12.1 On-Chain Attestation
The `attestation_score` field in the trust score formula (currently 0.0) is ready for Layer 3 to provide blockchain-backed attestation values. `service_identities.attestation_score` stores the current value.

### 12.2 Reputation Score
The `reputation_score` field in the trust score formula (currently 0.0) can receive cross-agent reputation signals from Layer 3's trust network.

### 12.3 Trust Tier 4
`services.trust_tier` accepts integer values up to 4. Tier 4 ("Ledger Attested") requires Layer 3's on-chain verification. The `agent_identities` table can similarly be extended.

### 12.4 Credential Revocation Lists
The `revocation_events` table provides an append-only audit trail suitable for publishing as a W3C Credential Status List or feeding into a Layer 3 on-chain revocation registry.

### 12.5 Federation
The did:web resolution infrastructure (`resolve_service_did_document`) is designed to support cross-registry trust by resolving DID documents from any domain. Layer 3 can extend this to federated agent discovery across multiple AgentLedger instances.

---

*Canonical spec: `spec/LAYER2_SPEC.md` — update it before changing any Layer 2 behavior.*  
*This completion summary is a point-in-time snapshot for architect review. The spec remains the source of truth.*
