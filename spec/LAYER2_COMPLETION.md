# AgentLedger — Layer 2 Completion Summary

**For:** architect sign-off and Layer 3 planning  
**Date:** April 14, 2026  
**Implementation Branch:** `layer2/identity-attestation`  
**Verification Scope:** full local test suite, Layer 2-focused suite, and identity verify load test

---

## 1. What Shipped

Layer 2 is the identity and authorization layer for AgentLedger. The current implementation includes:

| Capability | Description |
|------------|-------------|
| Agent identity | Ed25519 `did:key` registration with proof of key control and JWT VC issuance |
| Credential verification | Online verification with revocation enforcement |
| Session assertions | Short-lived JWT assertions scoped to one service DID and ontology tag |
| Service identity | `did:web` resolution, signed manifest validation, and trust-score activation |
| HITL authorization | Pending approval queue for `sensitivity_tier >= 3` actions |
| Revocation | Admin-triggered revocation with Redis-backed hot-path checks |

---

## 2. Build Phases

| Phase | Scope | Commit | Status |
|-------|-------|--------|--------|
| 1 | Identity foundation | `9b71f50` | Done |
| 2 | Session assertion engine | `3f9ad0d` | Done |
| 3 | Service identity activation | `678aaf4` | Done |
| 4 | HITL approval flow | `601629c` | Done |
| 5 | Identity hardening | `c6fce69` | Done |
| 6 | Runtime packaging | `3142147` | Done |
| 7 | Final verification and documentation | `79c009d` | Done |

---

## 3. API Surface

Layer 2 adds 13 endpoints:

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| `GET` | `/v1/identity/.well-known/did.json` | None | Issuer DID document |
| `POST` | `/v1/identity/agents/register` | API key | Register agent DID and issue VC |
| `POST` | `/v1/identity/agents/verify` | None | Verify a presented credential |
| `GET` | `/v1/identity/agents/{did}` | None | Resolve a registered agent record |
| `POST` | `/v1/identity/agents/{did}/revoke` | Admin key | Revoke an agent credential |
| `POST` | `/v1/identity/sessions/request` | Bearer VC | Request a session assertion |
| `GET` | `/v1/identity/sessions/{id}` | Bearer VC | Poll issued or pending session state |
| `POST` | `/v1/identity/sessions/redeem` | None | Redeem an assertion once |
| `GET` | `/v1/identity/services/{domain}/did` | None | Resolve a service `did:web` document |
| `POST` | `/v1/identity/services/{domain}/activate` | API key | Activate signed service identity |
| `GET` | `/v1/authorization/pending` | Admin key | List pending HITL requests |
| `POST` | `/v1/authorization/approve/{id}` | Admin key | Approve and issue linked session |
| `POST` | `/v1/authorization/deny/{id}` | Admin key | Deny a pending request |

---

## 4. Cryptographic Shape

- Agent DID method: `did:key`
- Service DID method: `did:web`
- JWT algorithm: `EdDSA` / Ed25519
- Issuer DID: configurable via `ISSUER_DID`
- Credential TTL: configurable via `CREDENTIAL_TTL_SECONDS`
- Registration proof replay guard: Redis `NX` nonce storage
- Session proof replay guard: Redis `NX` nonce storage

---

## 5. Database Additions

Layer 2 adds four tables:

| Table | Purpose |
|-------|---------|
| `agent_identities` | Agent DID registry and revocation state |
| `session_assertions` | Issued assertion tokens and one-use redemption tracking |
| `authorization_requests` | HITL approval queue and timeout state |
| `revocation_events` | Append-only revocation audit log |

Service identity state is derived from the current manifest, `services.public_key`, `services.last_verified_at`, and crawl events. There is no dedicated `service_identities` table in v0.1.

---

## 6. Performance Snapshot

### Verify Endpoint

Target endpoint: `POST /v1/identity/agents/verify`

Load test snapshot:

| Metric | Value |
|--------|-------|
| Concurrency | 100 users |
| Duration | 30 seconds |
| Total requests | 5,350 |
| Failures | 0 |
| Median | 61ms |
| p95 | 110ms |
| p99 | 160ms |

Key hot-path improvements:

1. Cached issuer key material at module scope
2. Reduced revocation lookup to a single `SISMEMBER` plus per-DID fallback
3. Removed lazy revocation prewarm from the verify path
4. Removed per-request `last_seen_at` write from verify
5. Offloaded signature verification from the async event loop

---

## 7. Acceptance Criteria

| # | Criterion | Status |
|---|-----------|--------|
| 1 | `POST /identity/agents/register` issues a signed VC for a valid `did:key` registration | Passed |
| 2 | `POST /identity/agents/verify` performs online verification with revocation checks | Passed |
| 3 | Bearer VC authentication gates `/identity/sessions/request` | Passed |
| 4 | Revoked credentials are rejected on verify and bearer-auth paths | Passed |
| 5 | Service `did:web` resolution fetches and caches `/.well-known/did.json` | Passed |
| 6 | Session assertions are scoped to one service DID and ontology tag | Passed |
| 7 | `sensitivity_tier >= 3` requests enter the HITL queue | Passed |
| 8 | Approval issues a linked session and denial blocks issuance | Passed |
| 9 | Proof nonce replay protection prevents detached proof reuse | Passed |
| 10 | Verify endpoint stays under 200ms p95 at 100 concurrent users | Passed |

---

## 8. Test Verification

Local verification completed with:

- `pytest -q` -> `213 passed`
- Layer 2-focused suite -> `34 passed`

Primary Layer 2 coverage files:

| File | Focus |
|------|-------|
| `tests/test_api/test_identity.py` | Agent, service DID, and authorization routes |
| `tests/test_api/test_identity_service.py` | Revocation cache and proof nonce behavior |
| `tests/test_api/test_sessions.py` | Session request, poll, and redeem routes |
| `tests/test_api/test_sessions_service.py` | Session service replay protections |
| `tests/test_api/test_authorization.py` | HITL queue, approve, and deny flows |
| `tests/test_api/test_service_identity.py` | `did:web` resolution and activation |
| `tests/test_api/test_crypto.py` | Ed25519, DID, and JWT round-trips |
| `tests/test_crawler/test_expire_identity_records.py` | Session and authorization expiry |
| `tests/test_crawler/test_revalidate_service_identity.py` | Service identity revalidation |

---

## 9. Configuration Surface

| Variable | Default | Purpose |
|----------|---------|---------|
| `ISSUER_DID` | `did:web:agentledger.io` | VC issuer DID |
| `ISSUER_PRIVATE_JWK` | empty | Ed25519 private JWK for signing |
| `CREDENTIAL_TTL_SECONDS` | `31536000` | Credential expiration |
| `PROOF_NONCE_TTL_SECONDS` | `60` | Proof replay window |
| `SESSION_ASSERTION_TTL_SECONDS` | `300` | Standard session lifetime |
| `APPROVED_SESSION_TTL_SECONDS` | `900` | Approved HITL session lifetime |
| `AUTHORIZATION_REQUEST_TTL_SECONDS` | `300` | Pending request timeout |
| `REVOCATION_CACHE_TTL_SECONDS` | `300` | Revocation cache TTL |
| `DID_WEB_CACHE_TTL_SECONDS` | `600` | `did:web` cache TTL |
| `AUTHORIZATION_WEBHOOK_URL` | empty | Optional approval webhook |
| `AUTHORIZATION_WEBHOOK_SECRET` | empty | Webhook signing secret |
| `AUTHORIZATION_WEBHOOK_TIMEOUT_SECONDS` | `3.0` | Webhook timeout |

---

## 10. Layer 3 Handoff

Layer 3 can extend the current implementation without breaking Layer 2 contracts:

- replace or augment service `attestation_score` evidence with third-party or on-chain attestations
- extend `reputation_score` beyond local session outcomes into cross-agent or cross-registry trust signals
- publish revocation state into external status lists or federated trust infrastructure

---

Canonical behavior remains defined by [LAYER2_SPEC.md](LAYER2_SPEC.md).
