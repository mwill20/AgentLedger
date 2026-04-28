# 🎓 Lesson 30: The Audit Examiner — Hardening, Load Testing & Interview Readiness

## 🔬 Welcome Back, Agent Architect!

You've built the trust layer, understood every component, and traced events from API call to on-chain confirmation. Now comes the final test: not "does it work?" but "does it hold under adversarial pressure?"

Think of a **financial auditor reviewing internal controls**: they don't just check if the books balance today. They probe for collusion, test under load, and ask hard questions about what breaks when assumptions fail. This lesson is your audit review — understanding the four threat surfaces, what mitigates each, and how to explain the full system to any technical interviewer.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Name the four Layer 3 threat surfaces and the specific code lines that mitigate each
- ✅ Explain the caching strategy and per-prefix invalidation on writes
- ✅ Interpret the load test results and explain what the five hardening changes enabled
- ✅ Describe the complete test coverage map for Layer 3
- ✅ Answer five canonical interview questions about blockchain-anchored trust

**Estimated time:** 90 minutes
**Prerequisites:** Lessons 21–29 (complete Layer 3 curriculum)

---

## 🔍 The Four Layer 3 Threat Surfaces

Layer 3 defends against four distinct attacks. Each has a primary mitigation in code and a recommended production hardening.

```
┌─────────────────────────────────────────────────────────┐
│              Layer 3 Threat Model                       │
├────────────────────┬────────────────────────────────────┤
│ Threat             │ Primary Mitigation                  │
├────────────────────┼────────────────────────────────────┤
│ Auditor collusion  │ evaluate_trust_tier_4()             │
│                    │ → ≥2 independent auditor_org_id     │
├────────────────────┼────────────────────────────────────┤
│ Chain reorg attack │ confirm_pending_events()            │
│                    │ → 20-block window                   │
├────────────────────┼────────────────────────────────────┤
│ Blocklist poisoning│ dispatch_revocation_pushes()        │
│                    │ → Ed25519 signature on each push    │
├────────────────────┼────────────────────────────────────┤
│ Audit tampering    │ verify_audit_record()               │
│                    │ → hash + Merkle proof comparison    │
└────────────────────┴────────────────────────────────────┘
```

---

## 🛡️ Threat 1: Auditor Collusion

**What it is:** A single malicious firm registers multiple auditor DIDs and issues attestations from all of them, trying to satisfy the Tier 4 quorum of "≥2 independent organizations."

**Primary mitigation:**

**File:** [`api/services/ranker.py`](../../api/services/ranker.py) lines 101–113

```python
def evaluate_trust_tier_4(
    attestations: list[dict[str, Any]],
    is_globally_revoked: bool,
) -> bool:
    if is_globally_revoked or len(attestations) < 2:
        return False
    active_orgs = {
        str(attestation["auditor_org_id"])
        for attestation in attestations
        if not attestation.get("is_expired", False)
    }
    return len(active_orgs) >= 2
```

The `auditor_org_id` is extracted from the DID's last segment (from `trust.py` line 142):
```python
auditor_org_id = did_value.rsplit(":", 1)[-1]
# "did:web:audit.firm.io" → "audit.firm.io"
```

A firm registering `did:web:dept1.audit.firm.io` and `did:web:dept2.audit.firm.io` would produce org IDs `dept1.audit.firm.io` and `dept2.audit.firm.io` — different, so quorum is satisfied. This is a **known limitation**: the check is a strong deterrent for lazy attackers, but sophisticated actors can circumvent it by registering under different domain names.

**What breaks if removed:** Without the org independence check, a single audit firm could register N auditor DIDs and supply all N attestations themselves. The Tier 4 label would become meaningless — no independent verification required.

**Recommended (not implemented here):** A production hardening would require auditor DIDs to be independently verifiable via DNS TXT records (proving the registrant controls the domain) and would check TLD+1 distinctness rather than full last-segment matching.

---

## 🛡️ Threat 2: Chain Reorg Attack

**What it is:** Blockchain reorgs happen when competing miners (in PoW) or validators produce a longer chain that replaces recent blocks. Events in the "reorganized-out" blocks are removed from the canonical chain. An attacker could submit an attestation that gets confirmed in the canonical chain, wait for AgentLedger to promote it, then orchestrate a reorg that removes the attestation event — leaving the service with Tier 4 trust from an event that no longer exists on-chain.

**Primary mitigation:**

**File:** [`api/services/chain.py`](../../api/services/chain.py) — `confirm_pending_events()`

```python
# Only confirm events that are ≥ 20 blocks deep
AND block_number <= :latest_block - :confirmation_blocks
```

Where `confirmation_blocks = 20` (configurable via `CHAIN_CONFIRMATION_BLOCKS` env var).

On Polygon PoS (the live deployment), reorgs exceeding 20 blocks are **cryptoeconomically impractical** — they would require an attacker to control more than 50% of the staked POL and undo ~40 seconds of finalized blocks. The 20-block window is the standard defense against short reorgs on PoS chains.

**What breaks if removed:** Without the confirmation window, a trust score update would fire the moment an event is indexed — potentially from a block that gets reorged out within the next few seconds. The service would show `trust_tier=4` based on an event that no longer exists.

**What happens with a very deep reorg (>20 blocks)?** This is an unlikely but possible scenario. `confirm_pending_events` would have already promoted the event. The event's `tx_hash` would no longer exist on the canonical chain. The `poll_remote_chain_events` function would not see the orphaned event in subsequent `eth_getLogs` calls (since the block is no longer canonical). Manual remediation would be required: identify the orphaned event, mark it `is_confirmed=false`, and trigger a trust recompute.

> **Recommended (not implemented here):** A production deployment should run a periodic "reorg detector" that re-checks recent confirmed events against the canonical chain — for events confirmed within the last 500 blocks, verify the tx hash still exists on-chain. If not, mark the event as `is_orphaned=true` and trigger a trust recompute.

---

## 🛡️ Threat 3: Blocklist Poisoning

**What it is:** A man-in-the-middle or compromised downstream subscriber could inject false revocations into its local blocklist — falsely marking legitimate services as revoked to denial-of-service them, or removing real revocations to whitewash malicious services.

**Primary mitigation:**

**File:** [`api/services/federation.py`](../../api/services/federation.py) lines 230–237
**File:** [`api/services/crypto.py`](../../api/services/crypto.py) lines 92–96

```python
if settings.issuer_private_jwk:
    private_jwk = json.loads(settings.issuer_private_jwk)
    headers["X-AgentLedger-Signature"] = sign_json(body, private_jwk)
```

`sign_json` produces an Ed25519 signature over canonical JSON (deterministic byte serialization). Any subscriber that receives a push can verify:

```python
verify_json_signature(payload, signature, publisher_public_jwk) → True/False
```

If the payload was tampered in transit (any field added, removed, or modified), the signature check fails.

**What breaks if removed:** Without the signature, a compromised relay or DNS hijack could serve a different blocklist to downstream consumers. The signature provides cryptographic authentication of the publisher's identity — subscribers know the blocklist came from the registered AgentLedger instance, not an impostor.

**What breaks if the issuer key is misconfigured:** The current implementation silently falls back to `X-AgentLedger-Signature: ""` and delivers the unsigned push anyway. The subscriber receives the blocklist but cannot verify its origin. This is a fail-open design — prioritizing blocklist delivery over signature verification.

**Recommended (not implemented here):** Subscribers should reject pushes with an empty or invalid signature. The issuer should log a critical alert when `sign_json` fails. A key rotation protocol should be defined and documented.

---

## 🛡️ Threat 4: Audit Record Tampering

**What it is:** A database administrator (or a SQL injection vulnerability) directly modifies audit record data after anchoring — changing `action_context`, `outcome`, or `record_hash` in the `audit_records` table to cover up evidence of a security incident.

**Primary mitigation:**

**File:** [`api/services/audit.py`](../../api/services/audit.py) — `verify_audit_record()`

```python
# Step 1: Recompute the hash from the current DB state
computed_hash = canonical_hash(_audit_payload(record))
integrity_valid = computed_hash == record["record_hash"]

# Step 2: Verify the Merkle proof against the on-chain root
merkle_proof_valid = merkle.verify_proof(
    leaf=record["record_hash"],
    proof=record["merkle_proof"],
    root=batch["merkle_root"],
)
```

If `action_context` was modified in the DB, `canonical_hash(_audit_payload(record))` would produce a different hash than `record["record_hash"]` — `integrity_valid = False`.

If `record_hash` itself was modified in the DB, the Merkle proof (which was computed from the original `record_hash`) would no longer verify against the on-chain Merkle root — `merkle_proof_valid = False`.

The on-chain Merkle root is the tamper-evident anchor: it cannot be modified without a new on-chain transaction.

**What breaks if removed:** Without the hash + proof check, an attacker who modifies the DB would leave no detectable trace. The audit trail would appear intact while actually containing altered records.

---

## ⚡ Caching Strategy

Layer 3 introduced per-prefix short-TTL caching using `runtime_cache` (an in-memory LRU/TTL store).

**Cache entries and TTLs:**

| Cache prefix | TTL | Invalidated by |
|-------------|-----|----------------|
| `blocklist:{page}:{limit}:{since}` | 2s | New confirmed revocation |
| `chain_status:{mode}` | 1s | (auto-expiry only) |
| `attestations:{service_id}` | 2s | New attestation or revocation |

**Why short TTLs instead of explicit invalidation?** Explicit invalidation requires every write path to know which cache keys to clear. The blocklist cache key includes `since` — a timestamp — making it difficult to enumerate all affected keys on write. TTL expiry is the simpler contract: stale data is at most `TTL_seconds` old, which is acceptable for these read paths.

**The 5 hardening changes that enabled the final load run** (from `spec/LAYER3_COMPLETION.md` §8):

1. **Added a dedicated Layer 3 Locust profile.** The Layer 3 read paths (attestations, chain status, federation blocklist) needed their own load profile that didn't mix in Layer 1/2 traffic patterns.

2. **Stopped manifest seeding for the Layer 3 profile.** The Layer 3 profile is read-only. Seeding manifests during setup created write contention that skewed latency measurements for reads.

3. **Raised the temporary acceptance `IP_RATE_LIMIT`.** The default rate limit was triggering on the concentrated load from 100 concurrent Locust workers sharing the same IP. The load run needed to measure Layer 3 latency, not the rate limiter's rejection time.

4. **Added short TTL caches for the hot Layer 3 read paths.** Without caching, the `GET /v1/attestations/{service_id}` endpoint was running the attestation JOIN query for every request. With 100 concurrent users, this was a hot query. The 2-second TTL eliminated redundant DB queries while keeping data fresh.

5. **Fixed audit-batch confirmation reconciliation.** On-chain confirmed audit batches weren't always being reconciled back into `audit_batches.status=confirmed`. The fix ensured that `confirm_pending_events` updates the batch status when the anchor chain event confirms.

---

## 📊 Load Test Results

**Source:** [`spec/LAYER3_COMPLETION.md`](../../spec/LAYER3_COMPLETION.md) §8

| Metric | Value |
|--------|-------|
| Profile | `layer3` |
| Concurrency | 100 users |
| Duration | 30 seconds |
| Total requests | 6,681 |
| Failures | **0** |
| Median latency | **8ms** |
| Aggregated p95 | **92ms** |
| Aggregated p99 | 130ms |
| Throughput | 235.24 req/s |

**Endpoint breakdown:**

| Endpoint | p95 |
|----------|-----|
| `GET /v1/attestations/{service_id}` | 86ms |
| `GET /v1/attestations/{service_id}/verify` | 81ms |
| `GET /v1/chain/status` | 100ms |
| `GET /v1/federation/blocklist` | 84ms |

**Why p95=92ms is significant:** The Layer 3 spec target was `p95 < 500ms @ 100 concurrent`. The actual result (92ms) is 5× below the target. The median of 8ms shows that most requests hit the cache and return before any DB query completes. The 0 failure rate is the critical production readiness signal: no timeouts, no 5xx, no dropped connections under 100 concurrent users.

**What p95=92ms with median=8ms tells you:** The 8ms median represents cached responses. The 92ms p95 represents cache misses that require a DB round-trip. The gap (8ms → 92ms) shows the caching strategy is working — the cache hit rate is high enough that most users see sub-10ms latency.

---

## 🧪 Test Coverage Map

**Python test suite:** `232 passed, 1 warning`
**Contract test suite:** `4 passing`

**Layer 3 Python tests:** [`tests/test_api/test_layer3.py`](../../tests/test_api/test_layer3.py) — covers:
- Auditor registration (valid, duplicate DID upsert, invalid scope, invalid DID format, invalid chain_address)
- Attestation submission (valid, cross-scope rejection, unknown auditor, unknown service)
- Revocation (valid, non-existent service)
- Trust tier computation (single auditor → not Tier 4, dual org → Tier 4, revoked → dropped)
- Audit record create and verify (valid, tampered hash → integrity failure)
- Chain event indexing and confirmation
- Federation blocklist (full list, incremental `since`, pagination)
- Subscriber registration and push dispatch

**Hardhat contract tests:** [`contracts/test/AttestationLedger.test.js`](../../contracts/test/AttestationLedger.test.js) — covers:
- `recordAttestation` emits `AttestationRecorded` event
- `recordRevocation` emits `RevocationRecorded` event
- Calling `recordAttestation` without `AUDITOR_ROLE` reverts
- `isGloballyRevoked` mapping updates correctly on revocation

> **Recommended (not implemented here):** The contract test suite covers 4 cases. A production-hardened test suite would add: UUPS upgrade flow test (deploy logic V2, upgrade proxy, verify state preserved), `commitBatch` with duplicate root rejection, gas usage regression tests (ensure `commitBatch` stays under block gas limit for max batch size), and fuzz testing of the event decoding path.

---

## 📚 Interview Preparation — Five Canonical Questions

### Q1: "Why blockchain for an agent registry? Why not just use a signed database?"

**A:** A signed database can prove that a record existed at signing time, but it cannot prove the record **hasn't been modified since then** unless you trust the signer. If the database operator is compromised or coerced, they can silently alter or delete records. Blockchain provides **immutability without trusting a central operator**: once an event is confirmed in a block, altering it requires rewriting all subsequent blocks — computationally infeasible on a distributed network.

Specifically, AgentLedger uses blockchain for two things: (1) the **attestation event log** — a tamper-evident, ordered record of which auditor attested which service when; and (2) the **Merkle root anchor** — a single 32-byte fingerprint that commits to the integrity of potentially thousands of off-chain audit records. The blockchain is not used for data storage (too expensive); it's used as a **commitment scheme** and **event log** — exactly the pattern where its properties are valuable.

### Q2: "Why Polygon instead of Ethereum mainnet?"

**A:** Three reasons: **cost**, **finality**, and **compatibility**.

- **Cost:** A Polygon PoS transaction costs ~$0.001 vs. ~$5–50 on Ethereum mainnet (variable with gas price). AgentLedger writes attestation events, audit batch roots, and version events continuously — mainnet costs would be prohibitive.
- **Finality:** Polygon PoS produces a block every ~2 seconds. The 20-block confirmation window is ~40 seconds. Ethereum with PoS takes ~12 seconds per block and recommends 64 blocks for final finality — ~12 minutes vs. 40 seconds.
- **EVM compatibility:** Polygon is fully EVM-compatible. The same Solidity contracts, the same `web3.py` integration, the same Hardhat tooling work on Polygon as they would on Ethereum. No new toolchain required.

Polygon Amoy is the testnet. The NorthStar upgrade path (mentioned in the spec) would allow migration to Polygon zkEVM or Ethereum mainnet if the cost/finality calculus changes.

### Q3: "How do you prevent a single malicious auditor from granting Tier 4?"

**A:** Tier 4 requires `≥2 confirmed attestations from ≥2 different auditor organizations` (`evaluate_trust_tier_4` in `ranker.py`). The organization identity is derived from the DID's domain segment — `did:web:audit.firm.io` → `audit.firm.io`. A single firm registering multiple DIDs under the same domain produces the same `auditor_org_id`, satisfying only one of the two required organizations.

Additionally, scope is enforced at two layers: the Python API rejects cross-scope attestations before any DB write, and the EVM contract's `onlyRole(AUDITOR_ROLE)` means only registered auditors can emit events at all — an unregistered caller's transaction reverts.

The current implementation is a strong deterrent but not a cryptographic guarantee: a sophisticated attacker could register two DIDs under different controlled domains. A production hardening would add DNS-verified domain ownership and TLD+1 distinctness requirements.

### Q4: "How do you prove an audit record hasn't been tampered with?"

**A:** Two-layer verification via `verify_audit_record()`:

1. **Hash integrity check:** Recompute `canonical_hash(_audit_payload(record))` from the current DB values and compare to the stored `record_hash`. If any field was modified, the hashes don't match → `integrity_valid: false`.

2. **Merkle proof verification:** Verify the stored `merkle_proof` path against the batch's `merkle_root` using `merkle.verify_proof(leaf, proof, root)`. Then compare the batch's `merkle_root` to the on-chain `BatchAnchorCommitted` event's `merkle_root`. If the DB's `record_hash` was changed, the Merkle proof doesn't verify → `merkle_proof_valid: false`.

The on-chain Merkle root is the tamper-evident anchor: it cannot be changed without a new blockchain transaction. An attacker who modifies the DB would need to also generate a valid Merkle proof for the modified hash — which requires finding a collision in keccak256, computationally infeasible.

### Q5: "What happens during a chain reorg?"

**A:** A blockchain reorganization (reorg) occurs when a competing chain branch becomes canonical, replacing recent blocks. Events in the replaced blocks are removed from the canonical chain.

AgentLedger's 20-block confirmation window is the primary defense: events are only promoted to `is_confirmed=true` when they are ≥20 blocks deep. On Polygon PoS, reorgs exceeding 20 blocks are cryptoeconomically impractical. This means events confirmed by AgentLedger are in blocks deep enough that reorging them would require an attack costing far more than any benefit from the reorg.

For a reorg shallower than 20 blocks: the event is still `is_confirmed=false` in AgentLedger's DB. `poll_remote_chain_events` will re-index the canonical state on the next 5-second beat. The orphaned event is never promoted. No manual intervention needed.

For a reorg deeper than 20 blocks (catastrophic, essentially impossible on Polygon PoS): events that were already promoted would have stale state. Manual remediation would be needed — identify orphaned tx hashes, reset `is_confirmed=false`, trigger trust recomputes. This is an acknowledged gap: **no automated reorg-detection exists beyond the 20-block window**.

---

## 🧪 Hands-On: Threat Response Exercise

For each of the four threat surfaces, write a one-paragraph response covering:
1. What the specific attack is
2. Which line of code mitigates it
3. What breaks if that code is removed
4. The recommended hardening extension

Use the four sections above as reference. This exercise prepares you to answer "walk me through your threat model" in a technical interview.

**Bonus:** Open a Python REPL and manually run the `verify_audit_record` path with a tampered `record_hash`:

```bash
docker compose exec api python3 -c "
import asyncio
from api.db import get_db_session
from api.services.audit import verify_audit_record

async def run():
    async for db in get_db_session():
        # Get a record ID from: SELECT id FROM audit_records LIMIT 1;
        record_id = '<YOUR_RECORD_ID>'
        result = await verify_audit_record(db, record_id)
        print('Before tamper:', result)
        break

asyncio.run(run())
"

# Tamper with the record directly
docker compose exec db psql -U agentledger -d agentledger \
  -c \"UPDATE audit_records SET action_context = '{\"tool\": \"TAMPERED\"}' WHERE id = '<YOUR_RECORD_ID>';\"

# Run verify again
docker compose exec api python3 -c "
import asyncio
from api.db import get_db_session
from api.services.audit import verify_audit_record

async def run():
    async for db in get_db_session():
        record_id = '<YOUR_RECORD_ID>'
        result = await verify_audit_record(db, record_id)
        print('After tamper:', result)
        break

asyncio.run(run())
"
```

**Expected after tamper:**
```json
{
  "integrity_valid": false,
  "merkle_proof_valid": false,
  "on_chain_root_matches": true
}
```

`integrity_valid=false` because the hash no longer matches the DB content. `merkle_proof_valid=false` because the stored `record_hash` no longer verifies against the batch's Merkle root. `on_chain_root_matches=true` because the on-chain root is unchanged — it still reflects the original batch.

---

## 🌉 Bridge to Layer 4

Layer 4 builds directly on these stable Layer 3 surfaces (per `spec/LAYER3_COMPLETION.md` §9):

1. **Context disclosure gating:** `attestation_records`, `attestation_score`, and `trust_tier` are the inputs to the Layer 4 decision of whether to disclose sensitive context to an agent.
2. **Evidence trail:** `audit_records` and `audit_batches` are the tamper-evident evidence trail for context mismatch detection.
3. **Blocklist enforcement:** The federation blocklist is checked before any context routing decision — revoked services receive no context.
4. **Contract upgrade path:** `AuditChain.sol` is UUPS upgradeable. Layer 4 adds a `ContextDisclosureAnchored` event to the same contract without redeployment.
5. **Indexer reuse:** The existing `chain_events` indexer, 20-block confirmation model, and `/v1/chain/status` verification path work for Layer 4 disclosure proofs without modification.

---

## 📊 Summary Reference Card

| Threat | Primary code | Key function |
|--------|-------------|--------------|
| Auditor collusion | `ranker.py:101–113` | `evaluate_trust_tier_4` |
| Chain reorg | `chain.py:confirm_pending_events` | 20-block window |
| Blocklist poisoning | `federation.py:230–237` + `crypto.py:92–96` | `sign_json` + `X-AgentLedger-Signature` |
| Audit tampering | `audit.py:verify_audit_record` | hash + Merkle proof |

| Metric | Value |
|--------|-------|
| Load test concurrency | 100 users |
| Total requests | 6,681 |
| Failures | 0 |
| p95 latency | 92ms |
| p99 latency | 130ms |
| Median latency | 8ms |
| Python tests passing | 232 |
| Contract tests passing | 4 |

---

## ✅ Key Takeaways

- Layer 3 has four threat surfaces: auditor collusion (org-independence quorum), chain reorg (20-block window), blocklist poisoning (Ed25519 signatures), and audit tampering (hash + Merkle proof)
- The caching strategy uses short TTLs (1–2 seconds) rather than explicit invalidation — making it correct-by-default without requiring write paths to enumerate affected cache keys
- The load test achieved 92ms p95 at 100 concurrent users with 0 failures — 5× below the 500ms spec target — after five targeted hardening changes
- The full Layer 3 test suite is 232 Python + 4 contract tests — unit testing covers the pure functions, integration testing covers the DB pipeline, and Hardhat covers on-chain behavior
- Layer 4 is designed to consume Layer 3 surfaces directly: `attestation_score`, `trust_tier`, `audit_records`, the blocklist, and the existing chain event infrastructure

---

## 🎓 Congratulations — You've Completed the Layer 3 Curriculum!

You've traced every component from the smart contracts through the trust scoring engine, the background workers, and the federation push pipeline. You understand how blockchain-anchored trust works, why Polygon was chosen, how auditor independence is enforced, and how the system defends against the four primary threat vectors.

**What comes next:** Lesson 29 — The Inspector General — is the live Amoy acceptance run. It's optional for understanding the system but essential for anyone deploying it on a real testnet. The acceptance run mirrors the 10 criteria recorded in `spec/LAYER3_COMPLETION.md` with live transaction hashes.

*Remember: An auditor who doesn't probe the controls is just a rubber stamp. Layer 3 is designed to be probed — and to hold.* 🔬
