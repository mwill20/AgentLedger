# 🎓 Lesson 22: The Switchboard — Chain Abstraction Layer

## 🛡️ Welcome Back, Agent Architect!

In Lesson 21 you met the two smart contracts. Now here's the challenge: how do you write Python code that calls them during production — but also runs clean unit tests in CI without any Polygon tokens or RPC endpoint?

The answer is `api/services/chain.py` — the **switchboard** that routes every chain operation to the right destination. Your FastAPI services, Celery workers, and test suite all call the same functions. The switchboard decides what actually happens.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain the `local` vs. `web3` chain mode dispatch and how auto-detection works
- ✅ Trace a chain event from `record_chain_event()` through to the `chain_events` database row
- ✅ Explain `canonical_hash()` and `hash_identifier()` — what they hash and why
- ✅ Describe the `ON CONFLICT (tx_hash) DO NOTHING` idempotency pattern
- ✅ Read `poll_remote_chain_events()` and explain how it mirrors Amoy events into PostgreSQL
- ✅ Explain the 20-block confirmation window and trace the trust recompute trigger

**Estimated time:** 75 minutes  
**Prerequisites:** Lesson 21 (smart contracts and EVM basics)

---

## 🔍 What This File Does

```
📁 Any Layer 3 service function (attestation.py, audit.py, federation.py)
       |
       v  calls record_chain_event()
🔌 chain.py  ← The Switchboard
       |
       ├── CHAIN_MODE=local  →  synthesize fake tx_hash → persist to chain_events
       |
       └── CHAIN_MODE=web3   →  call real Polygon contract → wait for receipt
                                      → persist to chain_events
```

The key insight: **everything downstream of `chain.py` works identically in both modes**. Unit tests, Celery tasks, and the API all receive a `(tx_hash, block_number)` tuple and a `chain_events` row — they don't know whether Polygon was involved.

---

## 🏗️ Key Concepts

### Chain Mode

**File:** [`api/config.py`](../../api/config.py) lines 38–48

```python
# api/config.py — Layer 3 chain settings
chain_mode: str = "auto"           # "local", "web3", or "auto"
chain_network: str = "polygon-pos-local"
chain_id: int = 137                # 137=mainnet, 80002=Amoy testnet
chain_confirmation_blocks: int = 20
chain_start_block: int = 0
chain_index_window: int = 2000     # how many blocks to index in one poll
web3_provider_url: str = ""
chain_signer_private_key: str = ""
attestation_ledger_contract_address: str = ""
audit_chain_contract_address: str = ""
audit_anchor_batch_size: int = 100
```

Set these via environment variables (uppercase): `CHAIN_MODE=web3`, `CHAIN_ID=80002`, etc.

### `canonical_hash()` — EVM-style deterministic hashing

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 33–38

```python
def canonical_hash(payload: dict[str, Any]) -> str:
    """Hash one JSON payload in a deterministic EVM-style format."""
    payload_bytes = canonical_json_bytes(payload)
    if Web3 is not None:
        return Web3.keccak(payload_bytes).hex()
    return "0x" + sha3_256(payload_bytes).hexdigest()
```

**Why not Python's `hashlib.sha256`?**

The EVM uses keccak256 (not SHA-256) for everything — that's what `soliditySha3` and `abi.encode` produce. Using `Web3.keccak` means the same payload produces the same hash whether computed in Python or in Solidity. This is critical for audit record verification: the record_hash in the database must match what `AuditChain.sol` would compute independently.

The `sha3_256` fallback is used only when `web3.py` is not installed (like a stripped-down test environment). Note that Python's `sha3_256` is actually keccak256-compatible — Python's `hashlib.sha3_256` is the NIST SHA-3 standard, **not** keccak256. However, this codebase uses the `pysha3` fallback or web3's implementation. When `web3` is installed, `Web3.keccak` is used exclusively.

**`canonical_json_bytes`** (from `api/services/crypto.py`) produces deterministic JSON bytes: keys sorted alphabetically, no extra whitespace. The same dict always produces the same bytes regardless of insertion order.

### `hash_identifier()` — converting strings to EVM bytes32

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 41–46

```python
def hash_identifier(value: str) -> str:
    """Hash one identifier string into a fixed-width digest."""
    payload_bytes = value.encode("utf-8")
    if Web3 is not None:
        return Web3.keccak(payload_bytes).hex()
    return "0x" + sha3_256(payload_bytes).hexdigest()
```

Used to convert human-readable identifiers (service domain, auditor DID) into `bytes32` keys for contract calls:
```python
# When calling recordAttestation on-chain:
chain.hash_identifier(request.service_domain)  # "healthservice.example.com" → 0xabc123...
chain.hash_identifier(request.auditor_did)      # "did:web:auditor.io" → 0xdef456...
```

The smart contract maps service IDs using `keccak256(abi.encodePacked(serviceIdString))` — but in Python we use `hash_identifier(domain)` which produces the same result.

---

## 📝 Code Walkthrough: `_chain_mode()`

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 58–70

```python
def _chain_mode() -> str:
    mode = settings.chain_mode.lower()
    if mode in {"local", "web3"}:       # explicit override
        return mode
    if (                                # auto-detection: all four required
        Web3 is not None
        and settings.web3_provider_url
        and settings.attestation_ledger_contract_address
        and settings.audit_chain_contract_address
        and settings.chain_signer_private_key
    ):
        return "web3"
    return "local"                      # default: safe synthetic mode
```

⚠️ **Production footgun:** If `CHAIN_MODE` is not set (defaults to `"auto"`) and any one of the four web3 credentials is missing, the system silently falls back to `local` mode. All API calls succeed but nothing actually hits the chain. **Always set `CHAIN_MODE=web3` explicitly in production** and monitor `chain_events.tx_hash` to confirm real transactions are being written.

---

## 📝 Code Walkthrough: `record_chain_event()`

This is the single entry point for all chain writes. Every service function that needs to write to the chain calls this.

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 251–280

```python
async def record_chain_event(
    db: AsyncSession,
    event_type: str,                    # "attestation", "revocation", "version", "audit_batch"
    event_data: dict[str, Any],         # payload specific to this event type
    service_id: UUID | str | None = None,
) -> tuple[str, int]:
    """Persist one Layer 3 chain event into the indexed event log."""

    # Step 1: Try remote write (no-op in local mode)
    remote_result = _remote_write(event_type, event_data)

    # Step 2: If no remote result, synthesize a local tx_hash
    if remote_result is None:
        block_number = await next_block_number(db)
        tx_hash = canonical_hash({
            "event_type": event_type,
            "block_number": block_number,
            "nonce": str(uuid4()),     # ensures uniqueness even for identical payloads
            "event_data": event_data,
        })
    else:
        tx_hash, block_number = remote_result  # real Polygon tx hash + block number

    # Step 3: Persist to chain_events regardless of which path was taken
    await _persist_indexed_event(
        db=db,
        event_type=event_type,
        service_id=service_id,
        tx_hash=tx_hash,
        block_number=block_number,
        event_data=event_data,
    )
    return tx_hash, block_number
```

**Why return `(tx_hash, block_number)`?** The caller (e.g., `attestation.submit_attestation`) needs these values to populate the `attestation_records` row — so the database row can reference the chain event.

---

## 📝 Code Walkthrough: `_persist_indexed_event()`

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 142–185

```python
async def _persist_indexed_event(db, *, event_type, service_id, tx_hash, block_number, event_data):
    await db.execute(
        text("""
            INSERT INTO chain_events (
                event_type, service_id, tx_hash, block_number,
                chain_id, is_confirmed, event_data, indexed_at
            )
            VALUES (
                :event_type, :service_id, :tx_hash, :block_number,
                :chain_id, false,                   -- <-- always starts unconfirmed
                CAST(:event_data AS JSONB), NOW()
            )
            ON CONFLICT (tx_hash) DO NOTHING        -- <-- idempotency guard
        """),
        {...}
    )
```

Two design decisions worth noting:

1. **`is_confirmed = false`** — every new event starts unconfirmed. The `confirm_pending_events()` function (Celery beat, every 5 seconds) promotes events to `is_confirmed = true` after `chain_confirmation_blocks` (20) blocks have passed. Trust decisions (tier upgrades, blocklist inclusion) only happen on confirmed events.

2. **`ON CONFLICT (tx_hash) DO NOTHING`** — `tx_hash` has a unique constraint. If the Celery indexer polls a block range that overlaps with a previous poll (can happen due to restart), the duplicate INSERT silently does nothing instead of erroring. This makes the indexer **idempotent** — safe to run multiple times with overlapping ranges.

---

## 📝 Code Walkthrough: `_remote_write()`

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 188–248

```python
def _remote_write(event_type, event_data) -> tuple[str, int] | None:
    if not is_web3_enabled():
        return None             # local mode: skip entirely

    if event_type == "attestation":
        contract = _get_contract("AttestationLedger")
        tx_hash, block_number = _send_contract_transaction(
            contract.functions.recordAttestation(
                _hex_to_bytes32(event_data["service_chain_id"]),   # keccak256 of domain
                event_data["ontology_scope"],
                event_data.get("certification_ref") or "",
                int(event_data.get("expires_at_unix") or 0),
                _hex_to_bytes32(event_data["evidence_hash"]),      # keccak256 of evidence
            )
        )
        return tx_hash, block_number

    if event_type == "audit_batch":
        contract = _get_contract("AuditChain")
        tx_hash, block_number = _send_contract_transaction(
            contract.functions.commitBatch(
                _uuid_to_bytes32(event_data["batch_id"]),
                _hex_to_bytes32(event_data["merkle_root"]),
                int(event_data["record_count"]),
            )
        )
        return tx_hash, block_number
    ...
```

The helper `_send_contract_transaction()` (lines 109–130):
1. Builds the transaction with `build_transaction()` (sets gas, chain ID, nonce)
2. Signs with the private key from `settings.chain_signer_private_key`
3. Sends via `eth.send_raw_transaction()`
4. **Waits for receipt** (`wait_for_transaction_receipt`, 120s timeout) — this is synchronous/blocking in the current implementation, so the FastAPI response is held until the transaction is mined

---

## 📝 Code Walkthrough: `confirm_pending_events()`

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 482–561

```python
async def confirm_pending_events(db: AsyncSession) -> dict[str, int]:
    """Promote indexed chain events past the confirmation window."""

    # Step 1: Get latest block
    latest_block = _latest_chain_block_from_provider()  # from Polygon RPC
    if latest_block is None:
        latest_block = snapshot.latest_block             # fallback: DB max

    confirm_before = latest_block - settings.chain_confirmation_blocks  # latest - 20

    # Step 2: Promote events past the window
    result = await db.execute(text("""
        UPDATE chain_events
        SET is_confirmed = true, confirmed_at = NOW()
        WHERE is_confirmed = false
          AND block_number <= :confirm_before       -- older than 20 blocks
        RETURNING event_type, service_id, tx_hash
    """), {"confirm_before": confirm_before})

    # Step 3: Cascade confirmations to related tables
    rows = result.mappings().all()
    affected_service_ids = set()
    for row in rows:
        if row["event_type"] == "attestation":
            # Flip is_confirmed on the matching attestation_records row
            await db.execute(text("""
                UPDATE attestation_records SET is_confirmed = true, confirmed_at = NOW()
                WHERE tx_hash = :tx_hash
            """), {"tx_hash": row["tx_hash"]})
            if row["service_id"]:
                affected_service_ids.add(str(row["service_id"]))

        elif row["event_type"] == "audit_batch":
            await db.execute(text("""
                UPDATE audit_batches SET status = 'confirmed', confirmed_at = NOW()
                WHERE tx_hash = :tx_hash
            """), {"tx_hash": row["tx_hash"]})

        elif row["event_type"] == "revocation":
            if row["service_id"]:
                affected_service_ids.add(str(row["service_id"]))

    # Step 4: Trigger trust recompute for every affected service
    if affected_service_ids:
        from api.services import trust
        for service_id in affected_service_ids:
            await trust.recompute_service_trust(db=db, service_id=service_id)

    await db.commit()
    return {"confirmed_events": len(rows)}
```

**The 20-block window explained:** Polygon PoS can theoretically reorganize (reorg) the chain — if miners produce competing blocks, a shorter chain gets abandoned. Events on the abandoned chain effectively disappear. By waiting 20 blocks (~40 seconds on Polygon) before treating an event as confirmed, AgentLedger absorbs any reorg that affects fewer than 20 blocks. Deeper reorgs are extremely rare on Polygon PoS.

**Why is trust recompute triggered here?** Because confirmation is when trust state actually changes: an unconfirmed attestation doesn't activate tier 4. The trigger ensures trust scores and tier values are always up to date immediately after confirmation, not on the next scheduled recompute.

---

## 📝 Code Walkthrough: `poll_remote_chain_events()`

**File:** [`api/services/chain.py`](../../api/services/chain.py) lines 414–471

```python
async def poll_remote_chain_events(db: AsyncSession) -> dict[str, int | str]:
    """Poll remote chain logs and mirror them into chain_events."""
    if not is_web3_enabled():
        return {"indexed_events": 0, "status": "noop"}   # skip in local mode

    # Determine the block range to poll
    from_block = last_indexed_block + 1
    to_block = min(latest_block, from_block + settings.chain_index_window - 1)

    # Build a service domain → service UUID lookup table
    service_hashes = await _service_hash_map(db)

    # Poll all four event types
    event_specs = [
        ("attestation", attestation_contract.events.AttestationRecorded, "serviceId"),
        ("revocation",  attestation_contract.events.RevocationRecorded, "serviceId"),
        ("version",     attestation_contract.events.VersionRecorded, "serviceId"),
        ("audit_batch", audit_contract.events.BatchAnchorCommitted, None),
    ]
    for event_type, event_factory, service_key in event_specs:
        logs = event_factory.get_logs(fromBlock=from_block, toBlock=to_block)
        for event in logs:
            args = dict(event["args"])
            # Resolve serviceId (bytes32) back to UUID via the hash map
            service_id = service_hashes.get("0x" + args[service_key].hex()) if service_key else None
            await _persist_indexed_event(
                db=db,
                event_type=event_type,
                service_id=service_id,
                tx_hash=event["transactionHash"].hex(),
                block_number=int(event["blockNumber"]),
                event_data={k: _jsonable_event_value(v) for k, v in args.items()},
            )
    await db.commit()
    return {"indexed_events": indexed_events, "status": "indexed"}
```

**The service hash map** (`_service_hash_map`): The chain stores `serviceId` as `keccak256(domain)` (bytes32). When indexing an event, we need to resolve that hash back to a UUID. The map is built once per poll by querying all `(id, domain)` pairs from `services` and computing `hash_identifier(domain)` for each.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Trace a local synthetic attestation

```bash
# Start the stack with local chain mode
docker compose up -d db redis

CHAIN_MODE=local \
API_KEYS=dev-local-only \
uvicorn api.main:app --reload --port 8000 &

# Register a service first (if needed)
# Then submit a synthetic attestation
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:auditor.example.com",
    "name": "Test Auditor",
    "ontology_scope": ["health.*"],
    "chain_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
  }'
```

**Expected output:**
```json
{"application_id": "<UUID>", "status": "active"}
```

Now check the `chain_events` table:
```bash
docker compose exec db psql -U agentledger -d agentledger -c \
  "SELECT event_type, tx_hash, block_number, is_confirmed FROM chain_events ORDER BY indexed_at DESC LIMIT 5;"
```

**Expected output:**
```
 event_type  |           tx_hash                | block_number | is_confirmed
-------------+----------------------------------+--------------+--------------
 attestation | 0xabc123...                      |     1        | f
```

### 🔬 Exercise 2: Manually confirm pending events

```bash
# Trigger the confirmation task directly via Python
docker compose exec api python3 -c "
import asyncio
from api.services.chain import confirm_pending_events
from api.dependencies import get_db_session
# ... (run in test context)
"
```

Or more practically, with a test:
```bash
# Run the specific test
docker compose exec api python3 -m pytest tests/test_api/test_layer3.py -k "confirm" -v
```

**Expected output:**
```
PASSED tests/test_api/test_layer3.py::test_confirm_chain_events
```

After confirmation, check the row:
```bash
docker compose exec db psql -U agentledger -d agentledger -c \
  "SELECT tx_hash, is_confirmed, confirmed_at FROM chain_events LIMIT 5;"
```

### 🔬 Exercise 3 (Failure): Missing web3 credentials

```bash
# With CHAIN_MODE=web3 but no provider URL, the mode auto-detection will...
CHAIN_MODE=web3 \
WEB3_PROVIDER_URL="" \
python3 -c "
from api.config import settings
from api.services.chain import _chain_mode
print('Mode:', _chain_mode())
"
```

**Expected output:**
```
Mode: local
```

⚠️ This is the **silent fallback** footgun. If `CHAIN_MODE=web3` is set but the provider URL is empty, it falls back to `local`. To guard against this in production, set `CHAIN_MODE=web3` explicitly and add a startup health check that verifies `GET /v1/chain/status` returns a non-zero `latest_block`.

---

## 📊 Summary Reference Card

| Item | Location | Notes |
|------|----------|-------|
| `canonical_hash(payload)` | `chain.py:33` | keccak256 of canonical JSON |
| `hash_identifier(value)` | `chain.py:41` | keccak256 of UTF-8 string |
| `_chain_mode()` | `chain.py:58` | `local`/`web3`/auto-detect |
| `record_chain_event()` | `chain.py:251` | Single entry point for all chain writes |
| `_persist_indexed_event()` | `chain.py:142` | DB write with `ON CONFLICT DO NOTHING` |
| `_remote_write()` | `chain.py:188` | Routes to correct contract function |
| `poll_remote_chain_events()` | `chain.py:414` | Indexes `eth_getLogs` into `chain_events` |
| `confirm_pending_events()` | `chain.py:482` | Promotes events past 20-block window |
| Config key `chain_mode` | `config.py:38` | `"local"`, `"web3"`, or `"auto"` |
| Config key `chain_confirmation_blocks` | `config.py:41` | Default: 20 |
| Config key `audit_anchor_batch_size` | `config.py:48` | Default: 100 |
| `chain_events` table | migration `004` | `tx_hash UNIQUE`, `is_confirmed`, `event_data JSONB` |

---

## 📚 Interview Preparation

**Q: What does `canonical_hash` guarantee that Python's `hashlib.sha256` does not?**

**A:** Two things. First, it uses keccak256 — the same hash function the EVM uses — so a hash computed in Python will match what a Solidity contract would compute independently for the same input. This is critical for audit record verification: the `record_hash` in the database must agree with what an on-chain verifier would compute. Second, it uses `canonical_json_bytes` which sorts dict keys and strips whitespace — guaranteeing that the same logical object always produces the same bytes regardless of how Python happened to build the dict internally.

**Q: Why is `ON CONFLICT (tx_hash) DO NOTHING` important in `_persist_indexed_event`?**

**A:** The chain indexer (`poll_remote_chain_events`) polls a block window and may re-scan blocks it has already seen — for example, if the process restarts mid-window or if the window overlaps with a previous run. Without the idempotency guard, re-scanning would produce duplicate rows in `chain_events` and break uniqueness-dependent queries (like the DB vs. chain cross-check in `verify_service_attestations`). The `UNIQUE` constraint on `tx_hash` combined with `DO NOTHING` makes the indexer safe to run multiple times with any overlapping range.

**Q: What happens in local mode when a test submits an attestation?**

**A:** `_remote_write()` returns `None` because `is_web3_enabled()` is false. `record_chain_event()` falls through to the local path: it computes the next synthetic block number (max + 1 from `chain_events`), generates a deterministic `tx_hash` by hashing `{event_type, block_number, nonce, event_data}`, and calls `_persist_indexed_event()` with that synthetic hash. The result is a fully populated `chain_events` row that all downstream code treats identically to a real Polygon transaction. Tests can then call `confirm_pending_events()` to simulate the 20-block window passing.

---

## ✅ Key Takeaways

- `api/services/chain.py` is the **single switchboard** between Python and the EVM — all other Layer 3 services call it; none talk to web3 directly
- `canonical_hash()` uses keccak256 to produce EVM-compatible hashes; `hash_identifier()` converts human strings to `bytes32`
- `_chain_mode()` dispatches to `local` or `web3` based on explicit config or auto-detection; **silent fallback to local is a production footgun**
- Every new chain event starts with `is_confirmed=false`; `confirm_pending_events()` promotes events after 20 blocks and triggers trust recomputes
- The `ON CONFLICT (tx_hash) DO NOTHING` pattern makes the indexer idempotent — safe to restart or overlap

---

## 🚀 Ready for Lesson 23?

Next up: **The Badge Office — Auditor Registration & Credentialing**. We'll meet the auditor registration flow, the ontology scope wildcard system, and how a single `_scope_allows()` check enforces that no health auditor can attest a finance service.

*Remember: The switchboard makes every test and deployment scenario work from the same code — the mode changes, the logic stays the same.* 🛡️
