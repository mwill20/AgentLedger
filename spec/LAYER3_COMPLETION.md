# AgentLedger - Layer 3 Completion Summary

**For:** architect sign-off and Layer 4 planning  
**Date:** April 27, 2026  
**Implementation Branch:** `layer3/trust-verification`  
**Verification Network:** Polygon Amoy (`chain_id=80002`)  
**Core implementation commit:** `5ef619d` - "Build Layer 3 trust verification and Amoy runtime"

---

## 1. What Shipped

Layer 3 is the trust, verification, audit, and federation layer for AgentLedger. The current implementation includes:

| Capability | Description |
|------------|-------------|
| Auditor network | Auditor registration, scope control, and active auditor listing |
| Live attestations | Service attestations written to `AttestationLedger` on Polygon Amoy and indexed back into PostgreSQL |
| Live revocations | Service revocations written on-chain, confirmed through the 20-block window, and exposed via the shared blocklist |
| Trust tier 4 | `attestation_score` population plus live tier-4 activation after confirmed multi-auditor attestation |
| Audit chain | Off-chain audit records, Merkle batch construction, on-chain batch anchoring, and verification endpoints |
| Federation | Public blocklist endpoint, SSE stream, subscriber registration, and revocation push fan-out |
| Chain operations | Amoy contract deployment, role grants, chain event indexing, and confirmation tracking via `/v1/chain/status` |

---

## 2. Build Phases

This branch kept Layer 3 delivery in one consolidated implementation commit plus the completion summary, rather than one commit per phase.

| Phase | Scope | Commit | Status |
|-------|-------|--------|--------|
| Spec baseline | Layer 3 spec and branch kickoff | `0c7d5a1` | Done |
| 1 | Smart contract foundation | `5ef619d` | Done |
| 2 | Chain event listener and confirmation flow | `5ef619d` | Done |
| 3 | Attestation API and auditor network | `5ef619d` | Done |
| 4 | Audit chain and Merkle batching | `5ef619d` | Done |
| 5 | Cross-registry federation | `5ef619d` | Done |
| 6 | Hardening, load path tuning, and live Amoy verification | `5ef619d` | Done |

---

## 3. API Surface

Layer 3 adds 18 endpoints:

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| `POST` | `/v1/auditors/register` | API key | Register or refresh one active auditor |
| `GET` | `/v1/auditors` | API key | List active auditors |
| `GET` | `/v1/auditors/{did}` | API key | Resolve one auditor |
| `POST` | `/v1/attestations` | API key | Submit one live service attestation |
| `POST` | `/v1/attestations/revoke` | API key | Submit one live service revocation |
| `GET` | `/v1/attestations/{service_id}` | API key | List active attestations for one service |
| `GET` | `/v1/attestations/{service_id}/verify` | API key | Compare DB attestation state to chain events |
| `POST` | `/v1/audit/records` | API key | Create one audit record pending anchor |
| `GET` | `/v1/audit/records` | API key | Query audit history |
| `GET` | `/v1/audit/records/{record_id}` | API key | Fetch one audit record |
| `GET` | `/v1/audit/records/{record_id}/verify` | API key | Verify one stored record and Merkle proof |
| `GET` | `/v1/chain/status` | Public | Report chain mode, latest block, and optional tx confirmation status |
| `GET` | `/v1/chain/events` | API key | Query indexed Layer 3 chain events |
| `GET` | `/.well-known/agentledger-blocklist.json` | Public | Publish the current revocation blocklist |
| `GET` | `/v1/federation/blocklist` | Public | Return the confirmed shared blocklist |
| `GET` | `/v1/federation/blocklist/stream` | Public | SSE snapshot feed for blocklist consumers |
| `POST` | `/v1/federation/registries/subscribe` | API key | Register a downstream federation subscriber |
| `POST` | `/v1/federation/revocations/submit` | API key | Accept one incoming federated revocation |

---

## 4. Live Chain Deployment

### Contract addresses

| Contract | Address |
|----------|---------|
| `AttestationLedger` proxy | `0x7961BC0F69Dac95309F197E176ea8CD1D3EbF23D` |
| `AuditChain` proxy | `0x55366DA11A48e2dCFE3F67f9802aF3e032dC2244` |

### Deployment and role-grant transactions

| Action | Tx hash | Block |
|--------|---------|-------|
| `AttestationLedger` deployment | `0x8cb72138a80eecdd9405566996fb32a1e3658f1649bed859acb101cbe7eb46bd` | `37400939` |
| `AuditChain` deployment | `0xf3cbb54162e163db136a933e6b290a48c736d86025534dfe2c1f333aac187b95` | `37400945` |
| `AUDITOR_ROLE` grant | `0x37191c2479999b28b6b7a111d1d3f1b2d16d0f5a25ad994b1fad5be81c0c05dd` | `37400994` |
| `ANCHOR_ROLE` grant | `0x8503208a9dc89e1fabc4e1686cf5ebdd770e793a742963e62cb8eba02b919d14` | `37400996` |

`ADMIN_ROLE` was granted during proxy initialization to the deployer/app signer, so there is no separate `ADMIN_ROLE` tx hash.

---

## 5. Acceptance Criteria

All 10 Layer 3 gates have now been verified on Polygon Amoy:

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Attestation Events written and indexed on Amoy chain | [x] | 2 confirmed `attestation` events; txs `0xff6f0a456c36452a85a437b68240678b27a8b042185c9c403429d2e9825a7b55` and `0x867cee7807f664396d1d215fb1c61f14b52539e381ec674ff3dd034675e08e8b` |
| 2 | Revocation Events propagate to federation blocklist | [x] | Confirmed revocation tx `0xfcd55ffde0653ca09a267f049b82402f5fc06a2de812995b59beb713c8f20f3f`; service appeared in `/v1/federation/blocklist` |
| 3 | Version Events track manifest changes immutably | [x] | `42` confirmed post-deployment `version` events indexed on Amoy; latest sample tx `0x9682937e9d287476d64437da49a6f3f3160df326926e75b98351dfdc97c570be` |
| 4 | Trust score recomputes with `attestation_score` populated | [x] | `GET /v1/services/1b1f88dc-5dab-41db-9f00-957bc053c6ac` returned `trust_score=49.9` and `attestation_score=1.0` |
| 5 | Trust tier 4 gate activates after live attestation | [x] | Same service returned `trust_tier=4` after two confirmed live auditor attestations |
| 6 | Audit batch anchoring confirmed on-chain | [x] | Batch `268fb73d-f5d7-4787-a1b0-f40a6f064fa8` anchored with tx `0xe066d788317d44e0241e2a71b21f5cb76462ce1e0302f23f84e93fc765be1b9b`; `confirmation_depth=20`; `audit_batches.status=confirmed` |
| 7 | Federation blocklist endpoint returns revoked services | [x] | `/v1/federation/blocklist` returned `test-1b1f88dc.example.com` with its revocation tx |
| 8 | Live chain deployment verified (real Amoy tx hashes) | [x] | Both proxy deployments and role grants completed on Amoy with recorded tx hashes |
| 9 | 20-block confirmation behavior verified on real chain | [x] | Verified for attestation tx `0x867cee...`, revocation tx `0xfcd55f...`, and audit batch tx `0xe066d7...` |
| 10 | Layer 3 load targets: p95 < 500ms @ 100 concurrent | [x] | Final Layer 3 Locust run completed with `0` failures and aggregated `p95 = 92ms` |

### Final indexed event snapshot

| Event type | Count |
|------------|-------|
| `attestation` | `2` |
| `audit_batch` | `1` |
| `revocation` | `1` |
| `version` | `42` |

---

## 6. Acceptance Run Transactions

### Live acceptance transactions

| Flow | Tx hash | Block |
|------|---------|-------|
| Attestation 1 | `0xff6f0a456c36452a85a437b68240678b27a8b042185c9c403429d2e9825a7b55` | `37402202` |
| Attestation 2 | `0x867cee7807f664396d1d215fb1c61f14b52539e381ec674ff3dd034675e08e8b` | `37402203` |
| Revocation | `0xfcd55ffde0653ca09a267f049b82402f5fc06a2de812995b59beb713c8f20f3f` | `37402243` |
| Audit batch anchor | `0xe066d788317d44e0241e2a71b21f5cb76462ce1e0302f23f84e93fc765be1b9b` | `37403577` |
| Sample live version event | `0x9682937e9d287476d64437da49a6f3f3160df326926e75b98351dfdc97c570be` | `37404295` |

### Confirmed service used for acceptance

| Field | Value |
|-------|-------|
| Service ID | `1b1f88dc-5dab-41db-9f00-957bc053c6ac` |
| Domain | `test-1b1f88dc.example.com` |

---

## 7. Test Verification

Fresh verification after the final audit-batch reconciliation fix:

- Python test suite: `232 passed, 1 warning`
- Contract test suite: `4 passing`

The earlier `228 Python + 4 contract` checkpoint was superseded by the final Layer 3 acceptance hardening and test-harness fixes on this branch. The current branch state is `232 Python + 4 contract`.

---

## 8. Load Test Snapshot

Final Layer 3 load run:

| Metric | Value |
|--------|-------|
| Profile | `layer3` |
| Concurrency | `100` users |
| Duration | `30s` |
| Total requests | `6681` |
| Failures | `0` |
| Median | `8ms` |
| Aggregated p95 | `92ms` |
| Aggregated p99 | `130ms` |
| Throughput | `235.24 req/s` |

Endpoint p95 values:

| Endpoint | p95 |
|----------|-----|
| `/v1/attestations/{service_id}` | `86ms` |
| `/v1/attestations/{service_id}/verify` | `81ms` |
| `/v1/chain/status` | `100ms` |
| `/v1/federation/blocklist` | `84ms` |

Key hardening that made the final run valid:

1. Added a dedicated Layer 3 Locust profile.
2. Stopped manifest seeding for the read-only Layer 3 profile.
3. Raised the temporary acceptance `IP_RATE_LIMIT` so the run measured Layer 3 latency instead of the limiter.
4. Added short TTL caches for the hot Layer 3 read paths.
5. Fixed audit-batch confirmation reconciliation so on-chain confirmed batches always settle into `audit_batches.status`.

---

## 9. Layer 4 Integration Points

Layer 4 should build on these stable Layer 3 surfaces:

1. Use `attestation_records`, `attestation_score`, and `trust_tier` as the gating input for sensitive context disclosure.
2. Use `audit_records` and `audit_batches` as the evidence trail for context mismatch detection and disclosure dispute review.
3. Enforce the federated blocklist before any context routing decision so revoked services receive no context.
4. Extend `AuditChain.sol` through the existing UUPS upgrade path with a Layer 4 `ContextDisclosureAnchored` event instead of redeploying the contract.
5. Reuse the existing `chain_events` indexer, 20-block confirmation model, and `/v1/chain/status` verification path for Layer 4 disclosure proofs.

---

Canonical behavior remains defined by [LAYER3_SPEC.md](LAYER3_SPEC.md).
