# 🎓 Lesson 26: The Fingerprint File — Audit Records & Merkle Batching

> **Beginner frame:** A Merkle batch is a compact fingerprint for many records. AgentLedger can prove one audit record belongs to a sealed batch without putting every private detail on-chain.

## 🛡️ Welcome Back, Agent Architect!

A forensic database doesn't store photographs of suspects — it stores fingerprint hashes. A hash uniquely identifies an object without revealing its contents, and the match is mathematically verifiable.

AgentLedger's audit chain uses the same principle: when an AI agent interacts with a service, a PII-redacted record is created, hashed, grouped with other records into a Merkle tree, and the tree's root is anchored on-chain. Any single record can later be proven to be in that batch with a logarithmic-length proof — without re-reading every record.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain why `action_context` is PII-redacted and what it does and doesn't store
- ✅ Trace `create_audit_record()` from HTTP request to `is_anchored=false` DB row
- ✅ Build a Merkle tree by hand for 4 leaves and understand level construction
- ✅ Explain odd-leaf duplication and why it's necessary
- ✅ Trace `anchor_pending_records()` through `merkle.build_tree()` to `commitBatch()` on-chain
- ✅ Call `verify_audit_record()` and interpret `integrity_valid` vs. `merkle_proof_valid`

**Estimated time:** 90 minutes  
**Prerequisites:** Lessons 22–24

---

## 🔍 What This Component Does

```
POST /v1/audit/records
           |
           v
📋 audit.create_audit_record()
           |
           ├── verify session_assertion exists (agent did this action)
           ├── build _audit_payload (PII-redacted canonical dict)
           ├── record_hash = canonical_hash(_audit_payload)
           └── INSERT INTO audit_records (is_anchored=false)

                [Celery beat: every 60 seconds]
           |
           v
⚙️ anchor_pending_records()
           |
           ├── SELECT all is_anchored=false records
           ├── merkle.build_tree(all record_hashes)
           ├── INSERT INTO audit_batches (merkle_root)
           ├── record_chain_event("audit_batch", ...)  ← writes to AuditChain.sol
           └── UPDATE audit_records SET batch_id, merkle_proof, is_anchored=true

GET /v1/audit/records/{id}/verify
           |
           v
🔍 audit.verify_audit_record()
           |
           ├── recompute_hash = canonical_hash(_audit_payload(stored fields))
           ├── integrity_valid = (recompute_hash == stored record_hash)
           └── merkle_proof_valid = merkle.verify_proof(record_hash, proof, batch.merkle_root)
```

---

## 🏗️ Database Tables

**File:** [`db/migrations/versions/004_layer3_trust_verification.py`](../../db/migrations/versions/004_layer3_trust_verification.py)

### `audit_records` (lines 68–103)
```sql
CREATE TABLE audit_records (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did           TEXT NOT NULL,              -- who performed the action
    service_id          UUID NOT NULL REFERENCES services(id),
    ontology_tag        TEXT NOT NULL REFERENCES ontology_tags(tag),
    session_assertion_id UUID REFERENCES session_assertions(id),
    action_context      JSONB NOT NULL,             -- PII-redacted: capability + input types only
    outcome             TEXT NOT NULL,              -- "success", "failure", "timeout", "rejected"
    outcome_details     JSONB NOT NULL DEFAULT '{}',
    record_hash         TEXT NOT NULL,              -- keccak256 of canonical audit payload
    batch_id            UUID,                       -- populated after anchoring
    merkle_proof        JSONB,                      -- proof path: [{position, hash}, ...]
    tx_hash             TEXT,                       -- populated after anchoring
    block_number        BIGINT,
    is_anchored         BOOLEAN NOT NULL DEFAULT false,
    anchored_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `audit_batches` (lines 105–120)
```sql
CREATE TABLE audit_batches (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    merkle_root     TEXT NOT NULL,              -- root of the Merkle tree
    record_count    INTEGER NOT NULL,
    tx_hash         TEXT UNIQUE,                -- Amoy tx hash for commitBatch()
    block_number    BIGINT,
    chain_id        INTEGER NOT NULL DEFAULT 137,
    status          TEXT NOT NULL DEFAULT 'pending',  -- "pending", "submitted", "confirmed"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at    TIMESTAMPTZ,
    confirmed_at    TIMESTAMPTZ
);
```

---

## 📝 Code Walkthrough: `_audit_payload()` — PII Redaction

**File:** [`api/services/audit.py`](../../api/services/audit.py) lines 27–46

```python
def _audit_payload(
    *,
    agent_did: str,
    service_id: str,
    ontology_tag: str,
    session_assertion_id: str | None,
    action_context: dict[str, Any],
    outcome: str,
    outcome_details: dict[str, Any],
) -> dict[str, Any]:
    """Build the canonical payload used for audit hashing."""
    return {
        "agent_did": agent_did,
        "service_id": service_id,
        "ontology_tag": ontology_tag,
        "session_assertion_id": session_assertion_id,
        "action_context": action_context,
        "outcome": outcome,
        "outcome_details": outcome_details,
    }
```

**What `action_context` stores (PII-safe):**
```python
# Correct — capability invoked + input types:
{"capability": "diagnose", "inputs": ["symptom_list", "patient_age_range"]}

# NOT acceptable — raw input values:
{"capability": "diagnose", "symptoms": "chest pain, shortness of breath", "patient_id": "12345"}
```

The audit record proves *what happened* (which capability was called, what the outcome was) without storing *what was said* (the actual input values). This satisfies GDPR/HIPAA audit logging requirements while avoiding PII exposure.

> **Recommended (not implemented here):** A production deployment should add a field validation step that rejects `action_context` values containing identifiable patterns (email, phone, SSN patterns). The current implementation trusts the caller to redact properly.

---

## 📝 Code Walkthrough: `create_audit_record()`

**File:** [`api/services/audit.py`](../../api/services/audit.py) lines 49–141

```python
async def create_audit_record(db, request: AuditRecordCreateRequest) -> AuditRecordCreateResponse:

    # Step 1: Verify the session_assertion exists (links record to an authorized session)
    session_row = await db.execute(text("""
        SELECT id, agent_did, service_id, ontology_tag FROM session_assertions
        WHERE id = :session_assertion_id
    """), ...)
    if session_row is None:
        raise HTTPException(404, "session assertion not found")

    # Step 2: Validate ontology_tag matches the session (prevents spoofing)
    if session_row["ontology_tag"] != request.ontology_tag:
        raise HTTPException(422, "audit ontology_tag must match the source session assertion")

    # Step 3: Build the canonical payload and compute the hash
    payload = _audit_payload(
        agent_did=session_row["agent_did"],
        service_id=str(session_row["service_id"]),
        ontology_tag=request.ontology_tag,
        session_assertion_id=str(request.session_assertion_id),
        action_context=request.action_context,    # PII-redacted by caller
        outcome=request.outcome,
        outcome_details=request.outcome_details,
    )
    record_hash = chain.canonical_hash(payload)   # keccak256 of canonical JSON

    # Step 4: Insert with is_anchored=false (awaits next batch cycle)
    record_id = await db.execute(text("""
        INSERT INTO audit_records (
            agent_did, service_id, ontology_tag, session_assertion_id,
            action_context, outcome, outcome_details,
            record_hash,
            is_anchored,         -- starts false
            created_at
        ) VALUES (...)
        RETURNING id
    """), ...)

    return AuditRecordCreateResponse(
        record_id=record_id,
        record_hash=record_hash,
        status="pending_anchor"   # always this status at creation
    )
```

---

## 📝 Code Walkthrough: `merkle.py` — Deep Dive

**File:** [`api/services/merkle.py`](../../api/services/merkle.py) — full 62 lines

### `_hash_pair(left, right)` — The Atomic Unit

```python
def _hash_pair(left: str, right: str) -> str:
    """Hash one ordered pair of hex digests into a new digest."""
    payload = bytes.fromhex(_strip_0x(left)) + bytes.fromhex(_strip_0x(right))
    return "0x" + sha3_256(payload).hexdigest()
```

Order matters. `_hash_pair(A, B) ≠ _hash_pair(B, A)`. The proof tracks which side the sibling is on (`"position": "left"` or `"right"`) so `verify_proof()` knows which order to apply each hash.

### `build_tree(leaves)` — Building Levels

```python
def build_tree(leaves: list[str]) -> dict[str, object]:
    """Build a Merkle root and proofs for a sequence of leaf digests."""
    if not leaves:
        return {"root": ZERO_HASH, "proofs": []}

    levels: list[list[str]] = [list(leaves)]     # level 0 = the leaf hashes

    while len(levels[-1]) > 1:                   # while more than one node at this level
        current = levels[-1]
        parent_level: list[str] = []
        for index in range(0, len(current), 2):  # step by 2 (pairs)
            left = current[index]
            right = current[index + 1] if index + 1 < len(current) else current[index]
            # ^^^ ODD-LEAF DUPLICATION: if no right sibling, pair leaf with itself
            parent_level.append(_hash_pair(left, right))
        levels.append(parent_level)

    # root = levels[-1][0]
```

**Visualizing a 4-leaf tree:**
```
Level 0 (leaves):  [H0, H1, H2, H3]
                   /  \       /  \
Level 1:       H(H0,H1)   H(H2,H3)
                   \        /
Level 2 (root): H(H(H0,H1), H(H2,H3))
```

**Visualizing a 3-leaf tree (odd duplication):**
```
Level 0:  [H0, H1, H2]
           /  \     |
Level 1: H(H0,H1) H(H2,H2)   <- H2 paired with itself!
               \  /
Level 2:    H(H01, H22)
```

### `build_tree(leaves)` — Building Proofs

```python
    proofs: list[list[dict[str, str]]] = []
    for leaf_index in range(len(leaves)):
        proof: list[dict[str, str]] = []
        index = leaf_index
        for level in levels[:-1]:               # for each level except the root
            sibling_index = index + 1 if index % 2 == 0 else index - 1
            if sibling_index >= len(level):
                sibling_index = index           # sibling is self (odd duplication)
            position = "right" if sibling_index >= index else "left"
            proof.append({"position": position, "hash": level[sibling_index]})
            index //= 2                         # move to parent index
        proofs.append(proof)
```

For leaf H0 in the 4-leaf tree:
- Level 0: sibling is H1 (index+1), position "right" → proof step `{position: "right", hash: H1}`
- Level 1: sibling is H(H2,H3) (index+1), position "right" → proof step `{position: "right", hash: H(H2,H3)}`

The proof for H0 is: `[{"position": "right", "hash": H1}, {"position": "right", "hash": H(H2,H3)}]`

### `verify_proof(leaf_hash, proof, root_hash)`

```python
def verify_proof(leaf_hash: str, proof: list[dict[str, str]], root_hash: str) -> bool:
    """Verify one Merkle inclusion proof."""
    current = leaf_hash
    for step in proof:
        sibling_hash = step["hash"]
        if step["position"] == "left":
            current = _hash_pair(sibling_hash, current)   # sibling on left
        else:
            current = _hash_pair(current, sibling_hash)   # sibling on right
    return current == root_hash
```

Walk: `current = H0 → hash_pair(H0, H1) → hash_pair(H01, H23) → should equal root`

A **Merkle proof for 1000 records is only log₂(1000) ≈ 10 steps long**. Compare to verifying a list of 1000 hashes directly — this is the logarithmic efficiency of Merkle trees.

---

## 📝 Code Walkthrough: `anchor_pending_records()`

**File:** [`api/services/audit.py`](../../api/services/audit.py) lines 334–442

```python
async def anchor_pending_records(db: AsyncSession) -> dict[str, object]:
    """Batch-anchor all current unanchored audit records."""

    # Step 1: Collect unanchored records (up to batch_size)
    rows = await db.execute(text("""
        SELECT id, record_hash FROM audit_records
        WHERE is_anchored = false
        ORDER BY created_at ASC
        LIMIT :limit
    """), {"limit": settings.audit_anchor_batch_size})   # default 100

    if not rows:
        return {"record_count": 0, "status": "noop"}

    # Step 2: Build Merkle tree
    leaf_hashes = [row["record_hash"] for row in rows]
    merkle_tree = merkle.build_tree(leaf_hashes)
    # merkle_tree = {"root": "0x...", "proofs": [[proof_for_row0], [proof_for_row1], ...]}

    # Step 3: Create batch record
    batch_id = await db.execute(text("""
        INSERT INTO audit_batches (merkle_root, record_count, status, created_at, submitted_at)
        VALUES (:merkle_root, :record_count, 'submitted', NOW(), NOW())
        RETURNING id
    """), {"merkle_root": merkle_tree["root"], "record_count": len(rows)})

    # Step 4: Write Merkle root to AuditChain.sol on Polygon (or synthetic)
    tx_hash, block_number = await chain.record_chain_event(
        db=db,
        event_type="audit_batch",
        event_data={
            "batch_id": str(batch_id),
            "merkle_root": merkle_tree["root"],
            "record_count": len(rows),
        },
    )

    # Update batch with tx_hash
    await db.execute(text("""
        UPDATE audit_batches SET tx_hash = :tx_hash, block_number = :block_number
        WHERE id = :batch_id
    """), ...)

    # Step 5: For each record, store its proof and mark anchored
    anchored_at = datetime.now(timezone.utc)
    for row, proof in zip(rows, merkle_tree["proofs"], strict=True):
        await db.execute(text("""
            UPDATE audit_records
            SET batch_id = :batch_id,
                merkle_proof = CAST(:merkle_proof AS JSONB),
                tx_hash = :tx_hash,
                block_number = :block_number,
                is_anchored = true,
                anchored_at = :anchored_at
            WHERE id = :record_id
        """), {"merkle_proof": json.dumps(proof), ...})

    await db.commit()
    return {"record_count": len(rows), "status": "submitted", "batch_id": str(batch_id), ...}
```

**Why `strict=True` in `zip(rows, merkle_tree["proofs"])`?**

`strict=True` raises `ValueError` if the two sequences are different lengths. This would catch a bug where the Merkle tree builder produced a different number of proofs than there are records — a data consistency guard.

**The 100x cost reduction:**
- Without batching: 100 records → 100 `commitBatch` calls → ~$0.10 in gas
- With batching: 100 records → 1 `commitBatch` call with Merkle root → ~$0.001 in gas

The trade-off: batch records are anchored every 60 seconds (Celery beat), not immediately. For most compliance use cases, a 60-second anchoring delay is acceptable.

---

## 📝 Code Walkthrough: `verify_audit_record()`

**File:** [`api/services/audit.py`](../../api/services/audit.py) lines 207–256

```python
async def verify_audit_record(db, record_id) -> AuditRecordVerifyResponse:

    # Step 1: Fetch the stored record
    detail = await get_audit_record(db=db, record_id=record_id)
    record = detail.record

    # Step 2: Re-derive the hash from first principles
    recomputed_hash = chain.canonical_hash(
        _audit_payload(
            agent_did=record.agent_did,
            service_id=str(record.service_id),
            ...
        )
    )

    # Step 3: Compare
    integrity_valid = (recomputed_hash == record.record_hash)
    if not integrity_valid:
        raise HTTPException(409, "record hash mismatch - possible tampering")

    # Step 4: Verify Merkle inclusion proof
    merkle_valid = False
    if record.is_anchored and record.batch_id and record.merkle_proof:
        batch_row = await db.execute(text("""
            SELECT merkle_root FROM audit_batches WHERE id = :batch_id
        """), ...)
        if batch_row:
            merkle_valid = merkle.verify_proof(
                leaf_hash=record.record_hash,
                proof=record.merkle_proof,     # stored in the record
                root_hash=batch_row["merkle_root"],   # stored in audit_batches
            )

    return AuditRecordVerifyResponse(
        integrity_valid=integrity_valid,
        merkle_proof_valid=merkle_valid,
        tx_hash=record.tx_hash,
        block_number=record.block_number,
    )
```

**Two types of validity:**

| Flag | What it proves |
|------|---------------|
| `integrity_valid=true` | The stored `record_hash` matches a fresh hash of the record's own fields — the record wasn't tampered with in the database |
| `merkle_proof_valid=true` | The `record_hash` is cryptographically included in the batch's Merkle tree — the batch wasn't constructed with fabricated records |

To fully verify against the on-chain anchor, a verifier should also:
1. Call `GET /v1/chain/status?tx_hash={batch_tx_hash}` to confirm `is_confirmed=true`
2. Query Amoy directly: verify the `BatchAnchorCommitted` event contains the matching `merkleRoot`

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Build a Merkle tree by hand in Python

```python
# In a Python REPL (or: python3 -c "...")
from api.services.merkle import build_tree, verify_proof
from api.services.chain import canonical_hash

# Create 3 fake record hashes
h0 = canonical_hash({"record": "agent action 1", "outcome": "success"})
h1 = canonical_hash({"record": "agent action 2", "outcome": "failure"})
h2 = canonical_hash({"record": "agent action 3", "outcome": "success"})

tree = build_tree([h0, h1, h2])
print("Root:", tree["root"])
print("Proofs:")
for i, proof in enumerate(tree["proofs"]):
    print(f"  Leaf {i}: {proof}")

# Verify each leaf
for i, (leaf, proof) in enumerate(zip([h0, h1, h2], tree["proofs"])):
    valid = verify_proof(leaf, proof, tree["root"])
    print(f"Leaf {i} valid: {valid}")
```

**Expected output:**
```
Root: 0x...
Proofs:
  Leaf 0: [{'position': 'right', 'hash': '0x...'}, {'position': 'right', 'hash': '0x...'}]
  Leaf 1: [{'position': 'left', 'hash': '0x...'}, {'position': 'right', 'hash': '0x...'}]
  Leaf 2: [{'position': 'right', 'hash': '0x...'}]   <- shorter! only 1 level for odd leaf
Leaf 0 valid: True
Leaf 1 valid: True
Leaf 2 valid: True
```

### 🔬 Exercise 2: Create records, anchor, and verify via API

```bash
# Assuming you have a session assertion UUID from a prior API interaction:
SESSION_ID="<your session assertion UUID>"

# Create 3 audit records
for i in 1 2 3; do
  curl -s -X POST http://localhost:8000/v1/audit/records \
    -H "X-API-Key: dev-local-only" \
    -H "Content-Type: application/json" \
    -d "{
      \"session_assertion_id\": \"${SESSION_ID}\",
      \"ontology_tag\": \"health.records.read\",
      \"action_context\": {\"capability\": \"read\", \"input_type\": \"record_id\"},
      \"outcome\": \"success\",
      \"outcome_details\": {}
    }" | python3 -m json.tool
done
```

Now trigger the batch anchor (normally done by Celery every 60s):
```bash
# Run the anchor task directly in test mode
docker compose exec api python3 -m pytest tests/test_api/test_layer3.py -k "anchor" -v
```

Check the result:
```bash
# Get a record ID from the output of the create step, then verify
RECORD_ID="<uuid from create response>"
curl -s "http://localhost:8000/v1/audit/records/${RECORD_ID}/verify" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected output:**
```json
{
  "integrity_valid": true,
  "merkle_proof_valid": true,
  "tx_hash": "0x...",
  "block_number": 2
}
```

### 🔬 Exercise 3 (Failure): Tamper with record_hash

```bash
# Get the record ID from above, then mutate the hash directly in the DB
docker compose exec db psql -U agentledger -d agentledger -c \
  "UPDATE audit_records SET record_hash = '0xdeadbeef00000000000000000000000000000000000000000000000000000000'
   WHERE id = '${RECORD_ID}';"

# Now verify — should get integrity_valid=false and a 409
curl -s "http://localhost:8000/v1/audit/records/${RECORD_ID}/verify" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected output:**
```json
{"detail": "record hash mismatch - possible tampering"}
```

---

## 📊 Summary Reference Card

| Item | Location | Notes |
|------|----------|-------|
| `_audit_payload()` | `audit.py:27` | Canonical dict for hashing — PII-redacted |
| `create_audit_record()` | `audit.py:49` | Hashes payload, inserts with `is_anchored=false` |
| `anchor_pending_records()` | `audit.py:334` | Batch collect → Merkle → commitBatch → update records |
| `verify_audit_record()` | `audit.py:207` | Checks hash integrity + Merkle proof |
| `_hash_pair(left, right)` | `merkle.py:15` | sha3_256 of left+right bytes (order matters) |
| `build_tree(leaves)` | `merkle.py:21` | Returns `{"root": ..., "proofs": [...]}` |
| `verify_proof(leaf, proof, root)` | `merkle.py:52` | Recomputes root from leaf + proof steps |
| Batch interval | `worker.py:61` | Every 60 seconds |
| Batch size | `config.py:48` | `audit_anchor_batch_size = 100` |
| `audit_records` table | migration `004:68` | `merkle_proof JSONB`, `batch_id` FK |
| `audit_batches` table | migration `004:105` | `merkle_root`, `status` lifecycle |

---

## 📚 Interview Preparation

**Q: How does a Merkle proof allow you to verify one record without downloading all records?**

**A:** A Merkle proof is a path from the leaf to the root. For a batch of 100 records, the proof is `log₂(100) ≈ 7` steps long. Each step provides the hash of the sibling node at that tree level. Starting from the record's own hash, you reconstruct each parent by hashing the current node with its sibling, following the position field (left/right). If the final hash equals the batch's Merkle root, the record is proven to be in the batch — without reading any other record. This is O(log n) verification instead of O(n).

**Q: Why does `_hash_pair` concatenate bytes in a fixed order? What would happen if it sorted them?**

**A:** Sorted concatenation (always smaller hash first) is used in some Merkle tree implementations to avoid having to track sibling position. This codebase keeps left-right order explicit and records `position` in the proof because it matches the natural tree traversal — leaf indices map directly to their proofs without resorting. More importantly, the current scheme makes it straightforward to extend to on-chain proof verification in a future Solidity verifier (which would need to agree on concatenation order).

**Q: What's the difference between `integrity_valid` and `merkle_proof_valid` in the verify response?**

**A:** `integrity_valid` checks that the stored `record_hash` still matches a fresh hash of the record's own database fields — it detects tampering with the record's content. `merkle_proof_valid` checks that the `record_hash` is cryptographically included in its batch's Merkle tree — it detects fabricated records being added to a batch after anchoring. You need both to have full integrity: a tampered `record_hash` could pass `merkle_proof_valid` if the proof was also updated to match, but the recomputed hash check would catch it.

---

## ✅ Key Takeaways

- Audit records store PII-redacted `action_context` (capability + input types, never raw values)
- `record_hash = canonical_hash(_audit_payload(all fields))` — the hash commits to every field
- Merkle batching anchors 100 records in one on-chain transaction (100x cost reduction vs. individual anchoring)
- Odd-leaf duplication pairs the last leaf with itself when the level has an odd count
- `verify_audit_record()` checks two things: hash integrity (record wasn't tampered) and Merkle inclusion (record is legitimately in the anchored batch)

---

## 🚀 Ready for Lesson 25?

Next up: **The Ledger of Trust — Trust Tier 4 & Scoring Engine**. Now that we understand attestations (Lesson 24) and audit records (this lesson), we'll see how `ranker.py` synthesizes them into a single trust score — and why Tier 4 requires a quorum of independent auditors, not just a high score.

*Remember: A fingerprint proves identity without revealing private information. The hash proves integrity without revealing private data.* 🛡️
