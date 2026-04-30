# 🎓 Lesson 24: The Stamp of Approval — The Attestation Pipeline

> **Beginner frame:** The attestation pipeline is a stamp-and-file process. An auditor claim becomes a database record, an optional chain event, and eventually a confirmed signal that can affect trust.

## 🛡️ Welcome Back, Agent Architect!

The auditor is registered, the scope is validated, and the badge is issued. Now it's time to actually stamp a service.

An attestation is a government inspection stamp — the auditor says "I evaluated this service against these standards, here's my certification reference, and it's valid until this date." The stamp goes into a public register (the blockchain) that anyone can query.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Trace `submit_attestation()` from HTTP request to on-chain event to confirmed DB row
- ✅ Explain `evidence_hash` — what it hashes and why you hash instead of store
- ✅ Describe the attestation lifecycle: `pending → confirmed → (expired or revoked)`
- ✅ Trace `submit_revocation()` and explain the dual write (DB + chain)
- ✅ Explain what `verify_service_attestations()` actually proves
- ✅ Identify the cache invalidation pattern for attestation reads

**Estimated time:** 75 minutes  
**Prerequisites:** Lessons 22–23

---

## 🔍 What This Component Does

```
POST /v1/attestations
           |
           v
🔏 attestation.submit_attestation()
           |
           ├── validate auditor scope (_scope_allows)
           ├── compute evidence_hash = canonical_hash(evidence_package)
           ├── record_chain_event("attestation", ...)   ← writes to Polygon
           ├── INSERT INTO attestation_records (is_confirmed=false)
           └── invalidate caches

GET /v1/attestations/{service_id}/verify
           |
           v
🔍 attestation.verify_service_attestations()
           |
           ├── query DB: all active attestation_records for service
           ├── query DB: all chain_events WHERE event_type='attestation' for service
           └── compare: does each DB row have a matching chain event with same hashes?
                → on_chain_matches_db: bool
```

---

## 📝 The `attestation_records` Table

**File:** [`db/migrations/versions/004_layer3_trust_verification.py`](../../db/migrations/versions/004_layer3_trust_verification.py) lines 36–65

```sql
CREATE TABLE attestation_records (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id      UUID NOT NULL REFERENCES services(id),
    auditor_id      UUID NOT NULL REFERENCES auditors(id),
    ontology_scope  TEXT NOT NULL,          -- e.g. "health.*"
    certification_ref TEXT,                 -- e.g. "ISO-27001:2022"
    evidence_hash   TEXT NOT NULL,          -- keccak256(evidence_package JSON)
    tx_hash         TEXT NOT NULL UNIQUE,   -- matches a chain_events row
    block_number    BIGINT NOT NULL,
    chain_id        INTEGER NOT NULL DEFAULT 137,
    is_confirmed    BOOLEAN NOT NULL DEFAULT false,  -- promoted after 20 blocks
    confirmed_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,            -- optional expiry from request
    is_active       BOOLEAN NOT NULL DEFAULT true,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Three indexes for query performance:
CREATE INDEX attestation_records_service ON attestation_records(service_id, is_active);
CREATE INDEX attestation_records_auditor ON attestation_records(auditor_id);
CREATE INDEX attestation_records_unconfirmed ON attestation_records(is_confirmed, block_number)
    WHERE is_confirmed = false;   -- partial index: only on unconfirmed rows
```

The **partial index** `WHERE is_confirmed = false` is a performance optimization: once rows are confirmed they'll never be re-scanned by the confirmation task, so the index only needs to cover the unconfirmed subset.

---

## 📝 Code Walkthrough: `submit_attestation()`

**File:** [`api/services/attestation.py`](../../api/services/attestation.py) lines 42–180

```python
async def submit_attestation(db, request: AttestationCreateRequest) -> AttestationCreateResponse:

    # --- VALIDATION PHASE ---

    # Step 1: Look up the auditor and verify they're active
    auditor_row = await db.execute(text("SELECT id, did, name, ontology_scope, is_active
                                         FROM auditors WHERE did = :did"), ...)
    if auditor_row is None or not auditor_row["is_active"]:
        raise HTTPException(403, "auditor is not active")

    # Step 2: Verify scope authorization
    if not _scope_allows(list(auditor_row["ontology_scope"] or []), request.ontology_scope):
        raise HTTPException(403, "attestation scope is outside the auditor's approved ontology scope")

    # Step 3: Look up the service being attested
    service_row = await db.execute(text("SELECT id, domain FROM services WHERE domain = :domain"), ...)
    if service_row is None:
        raise HTTPException(404, "service not found")

    # --- HASHING PHASE ---

    # Step 4: Hash the evidence package
    evidence_hash = chain.canonical_hash(request.evidence_package)
    # e.g. {"type": "soc2", "cert_id": "ABC-123"} → "0x4f3a..."

    # --- CHAIN WRITE PHASE ---

    # Step 5: Write to blockchain (or local synthetic) + persist to chain_events
    tx_hash, block_number = await chain.record_chain_event(
        db=db,
        event_type="attestation",
        service_id=service_row["id"],
        event_data={
            "service_domain": request.service_domain,
            "auditor_did": request.auditor_did,
            "ontology_scope": request.ontology_scope,
            "certification_ref": request.certification_ref,
            "expires_at": ...,
            "evidence_hash": evidence_hash,
            "service_chain_id": chain.hash_identifier(request.service_domain),
            "auditor_chain_id": chain.hash_identifier(request.auditor_did),
        },
    )

    # --- DB WRITE PHASE ---

    # Step 6: Deactivate any previous attestation for same service+auditor+scope
    await db.execute(text("""
        UPDATE attestation_records
        SET is_active = false
        WHERE service_id = :service_id AND auditor_id = :auditor_id
          AND ontology_scope = :ontology_scope AND is_active = true
    """), ...)

    # Step 7: Insert the new attestation record (starts unconfirmed!)
    attestation_id = await db.execute(text("""
        INSERT INTO attestation_records (
            service_id, auditor_id, ontology_scope, certification_ref,
            evidence_hash, tx_hash, block_number, chain_id,
            is_confirmed,           -- <-- always starts false
            expires_at, is_active, recorded_at
        ) VALUES (...)
        RETURNING id
    """), ...)

    await db.commit()

    # Step 8: Invalidate caches for this service
    runtime_cache.invalidate_prefix(f"attestations:{service_row['id']}")
    runtime_cache.invalidate_prefix(f"attestation-verify:{service_row['id']}")
```

### Why `is_confirmed=false` at insert?

The attestation is immediately persisted to the database for auditability, but it doesn't affect trust scores or tier eligibility until `confirm_pending_events()` promotes it after 20 blocks (~40 seconds on Polygon). This prevents a race condition where an attestation could briefly elevate a service's trust tier, only to be rolled back if the chain reorgs.

### Why deactivate previous attestations (Step 6)?

An auditor can re-attest a service — for example, after a renewal or scope change. Step 6 soft-deletes the old record before inserting the new one, ensuring only one active attestation exists per `(service_id, auditor_id, ontology_scope)` triple. The old record is kept for audit history (`is_active=false` rows are never deleted).

### What is `evidence_hash`?

```python
evidence_hash = chain.canonical_hash(request.evidence_package)
# request.evidence_package = {"type": "soc2", "cert_id": "ABC-2025", "audit_date": "2025-01-15"}
# evidence_hash = "0x4f3a8b..." (keccak256 of canonical JSON)
```

The `evidence_package` can contain anything — audit report IDs, certification numbers, review dates. Instead of storing the raw evidence on-chain (too expensive), we store its hash. This allows:
1. Anyone to verify that the evidence the database claims was submitted matches what was hashed on-chain
2. The original evidence package to remain off-chain (private to the auditor)
3. Future dispute resolution: the auditor can reveal the evidence package, and any verifier can confirm `canonical_hash(revealed_package) == evidence_hash`

---

## 📝 Code Walkthrough: `submit_revocation()`

**File:** [`api/services/attestation.py`](../../api/services/attestation.py) lines 329–419

Revocation is structurally similar to attestation, with two key differences:

```python
async def submit_revocation(db, request: RevocationCreateRequest) -> RevocationCreateResponse:
    # (similar validation: check auditor is active, check service exists)

    evidence_hash = chain.canonical_hash(request.evidence_package)

    # Write to chain: calls recordRevocation() on AttestationLedger
    # This sets isGloballyRevoked[serviceId] = true on-chain (the only state write)
    tx_hash, block_number = await chain.record_chain_event(
        db=db,
        event_type="revocation",
        service_id=service_row["id"],
        event_data={
            "service_domain": request.service_domain,
            "auditor_did": request.auditor_did,
            "reason_code": request.reason_code,          # e.g. "data_breach"
            "evidence_hash": evidence_hash,
            "service_chain_id": chain.hash_identifier(request.service_domain),
            "auditor_chain_id": chain.hash_identifier(request.auditor_did),
        },
    )

    # DUAL WRITE: also insert into revocation_events (Layer 2 table)
    revocation_id = await db.execute(text("""
        INSERT INTO revocation_events (target_type, target_id, reason_code, revoked_by, evidence)
        VALUES ('service', :target_id, :reason_code, :revoked_by, ...)
        RETURNING id
    """), ...)

    await db.commit()
    # Invalidate: attestation reads, verify reads, AND the blocklist cache
    runtime_cache.invalidate_prefix(f"attestations:{service_row['id']}")
    runtime_cache.invalidate_prefix(f"attestation-verify:{service_row['id']}")
    runtime_cache.invalidate_prefix("blocklist:")       # <-- federation cache too
```

**Why write to both `chain_events` AND `revocation_events`?**

- `chain_events` is the Layer 3 on-chain event log — it's used for confirmation tracking, chain vs. DB verification, and the federation blocklist
- `revocation_events` is the Layer 2 table — it's used for credential revocation flows, search result filtering, and service ban flags

These two tables serve different consumers. Writing to both ensures that both the Layer 2 trust pipeline (which reads `revocation_events`) and the Layer 3 trust pipeline (which reads `chain_events`) react to the revocation.

**What does `isGloballyRevoked[serviceId] = true` mean on-chain?**

It's the one mutable state in `AttestationLedger.sol`. It enables an O(1) contract call to check "is this service globally revoked?" without scanning event logs. Any downstream verifier can call `attestation_ledger.isGloballyRevoked(keccak256(domain))` and get an instant boolean response — no off-chain indexer required.

---

## 📝 Code Walkthrough: `verify_service_attestations()`

**File:** [`api/services/attestation.py`](../../api/services/attestation.py) lines 251–326

This is the "show your work" endpoint — it compares what's in the database against what's in `chain_events` and flags any discrepancy.

```python
async def verify_service_attestations(db, service_id) -> AttestationVerifyResponse:

    # Query 1: All active attestation_records for this service
    db_rows = await db.execute(text("""
        SELECT ar.tx_hash, ar.evidence_hash, ar.ontology_scope, a.did AS auditor_did
        FROM attestation_records ar JOIN auditors a ON a.id = ar.auditor_id
        WHERE ar.service_id = :service_id AND ar.is_active = true
    """), ...)

    # Query 2: All indexed chain_events of type 'attestation' for this service
    chain_rows = await db.execute(text("""
        SELECT tx_hash, event_data
        FROM chain_events
        WHERE service_id = :service_id AND event_type = 'attestation'
    """), ...)
    chain_by_tx = {row["tx_hash"]: row["event_data"] for row in chain_rows}

    # Compare counts (same number of rows?)
    matches = len(db_rows) == len(chain_rows)

    # Compare content (does each DB row have a matching chain event with same data?)
    for row in db_rows:
        event_data = chain_by_tx.get(row["tx_hash"])
        if event_data is None:
            matches = False     # DB row has no matching chain event
            continue
        if (
            event_data.get("evidence_hash") != row["evidence_hash"]   # was it tampered?
            or event_data.get("ontology_scope") != row["ontology_scope"]
            or event_data.get("auditor_did") != row["auditor_did"]
        ):
            matches = False

    return AttestationVerifyResponse(
        on_chain_matches_db=matches,
        attestation_count=len(db_rows),
        trust_tier_eligible=ranker.evaluate_trust_tier_4(confirmed_attestations, is_globally_revoked=False),
    )
```

**What does `on_chain_matches_db=false` actually mean?**

It means the database record doesn't agree with the indexed chain event. This can happen legitimately (if `poll_remote_chain_events` hasn't run yet — the event is on-chain but not yet indexed) or suspiciously (if someone modified the `evidence_hash` column directly in the database). The endpoint is primarily a diagnostic and trust verification tool, not a security gate.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Submit an attestation and fetch it

```bash
# First get a service UUID (swap in your domain)
SERVICE_DOMAIN="test-service.example.com"

# Submit attestation
curl -s -X POST http://localhost:8000/v1/attestations \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"auditor_did\": \"did:web:healthauditor.example.com\",
    \"service_domain\": \"${SERVICE_DOMAIN}\",
    \"ontology_scope\": \"health.*\",
    \"certification_ref\": \"ISO-27001:2022\",
    \"evidence_package\": {\"type\": \"manual_review\", \"passed\": true}
  }" | python3 -m json.tool
```

**Expected output:**
```json
{
  "attestation_id": "<UUID>",
  "tx_hash": "0x...",
  "block_number": 1
}
```

Get the service UUID from the domain:
```bash
SERVICE_UUID=$(docker compose exec db psql -U agentledger -d agentledger -tAc \
  "SELECT id FROM services WHERE domain = '${SERVICE_DOMAIN}';")

# Fetch attestations for the service
curl -s "http://localhost:8000/v1/attestations/${SERVICE_UUID}" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected output:**
```json
[
  {
    "attestation_id": "<UUID>",
    "auditor": {"did": "did:web:healthauditor.example.com", ...},
    "scope": "health.*",
    "tx_hash": "0x...",
    "is_confirmed": false
  }
]
```

### 🔬 Exercise 2: Detect tampered evidence_hash

First get the attestation's `tx_hash` from Exercise 1, then:
```bash
# Verify the attestation (should match)
curl -s "http://localhost:8000/v1/attestations/${SERVICE_UUID}/verify" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
# Expected: {"on_chain_matches_db": true, ...}

# Tamper with the evidence_hash directly in the DB
docker compose exec db psql -U agentledger -d agentledger -c \
  "UPDATE attestation_records SET evidence_hash = '0xdeadbeef' WHERE service_id = '${SERVICE_UUID}';"

# Verify again (should now fail)
curl -s "http://localhost:8000/v1/attestations/${SERVICE_UUID}/verify" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected output after tampering:**
```json
{"on_chain_matches_db": false, "attestation_count": 1, "trust_tier_eligible": false}
```

### 🔬 Exercise 3 (Failure): Revoke without prior attestation

```bash
curl -s -X POST http://localhost:8000/v1/attestations/revoke \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"auditor_did\": \"did:web:healthauditor.example.com\",
    \"service_domain\": \"nonexistent.example.com\",
    \"reason_code\": \"data_breach\",
    \"evidence_package\": {}
  }" | python3 -m json.tool
```

**Expected output:**
```json
{"detail": "service not found"}
```

---

## 📊 Summary Reference Card

| Item | Location | Notes |
|------|----------|-------|
| `submit_attestation()` | `attestation.py:42` | Full validation + chain write + DB insert |
| `submit_revocation()` | `attestation.py:329` | Chain write + dual DB write (chain_events + revocation_events) |
| `verify_service_attestations()` | `attestation.py:251` | DB vs. chain_events cross-check |
| `list_attestations_for_service()` | `attestation.py:183` | Cached (2s TTL), returns active non-expired rows |
| `evidence_hash` | `attestation.py:75` | `canonical_hash(request.evidence_package)` |
| `is_confirmed` initial value | `attestation.py:144` | Always `false` — promoted by `confirm_pending_events` |
| Cache TTL | `attestation.py:27` | `_ATTESTATION_READ_TTL_SECONDS = 2.0` |
| `attestation_records` table | migration `004:36` | `tx_hash UNIQUE`, `is_confirmed`, partial index on unconfirmed |
| Revocation on-chain effect | `AttestationLedger.sol:66` | `isGloballyRevoked[serviceId] = true` |

---

## 📚 Interview Preparation

**Q: Why hash the evidence package instead of storing it on-chain?**

**A:** On-chain storage costs ~$0.03 per 32 bytes on Polygon. A typical evidence package with audit report references, certification IDs, and review dates is 200–500 bytes — that's $0.20–$0.50 per attestation just for the evidence. The hash (`bytes32`) is a fixed 32 bytes regardless of evidence size. The hash preserves integrity (anyone can re-hash a provided evidence package to verify it), while the raw evidence stays off-chain. This is a standard pattern called "hash commitment" — you commit to the data by publishing its hash, then reveal the data later if needed.

**Q: What's the difference between `is_confirmed` in `attestation_records` and `is_confirmed` in `chain_events`?**

**A:** They're two sides of the same state. `chain_events.is_confirmed` is the primary truth: `confirm_pending_events()` sets it to `true` when the event is 20 blocks old. When that happens, the code also cascades the update to `attestation_records.is_confirmed`. Having it in both places allows efficient queries: `SELECT * FROM attestation_records WHERE is_confirmed=true` is a direct table scan with no join required. Trust tier and attestation_score calculations only use confirmed attestations.

**Q: Can the same auditor attest a service multiple times?**

**A:** Yes, and this is intentional for renewal flows. Each new attestation for the same `(service_id, auditor_id, ontology_scope)` triple soft-deletes the previous one (`is_active=false`) before inserting the new one. This means only one active attestation per `(service, auditor, scope)` exists at any time, but the full history is preserved in `is_active=false` rows for audit purposes.

---

## ✅ Key Takeaways

- The attestation lifecycle is: API request → validate scope → hash evidence → write chain event → insert DB row (`is_confirmed=false`) → 20-block window → confirm → trust recompute
- `evidence_hash = canonical_hash(evidence_package)` — hashing preserves integrity at 32 bytes instead of hundreds
- `submit_revocation()` does a dual write: `chain_events` (for the federation blocklist) AND `revocation_events` (for Layer 2 trust pipeline)
- `verify_service_attestations()` is a diagnostic tool — it compares DB rows to `chain_events` to detect if either was tampered post-write
- Caches are invalidated **immediately on write** so the next read sees fresh data

---

## 🚀 Ready for Lesson 26?

Next up: **The Fingerprint File — Audit Records & Merkle Batching**. We'll explore how agent transaction records are hashed, grouped into Merkle trees, and anchored on-chain at 100x lower cost than individual anchoring.

*(We'll come back to trust scoring — Lesson 25 — after the audit chain, since the scoring engine consumes outputs from both.)*

*Remember: The stamp creates a public, verifiable record. The hash proves the evidence was genuine at the moment of attestation.* 🛡️
