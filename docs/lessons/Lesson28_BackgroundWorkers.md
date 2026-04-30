# 🎓 Lesson 28: The Night Watchman — Celery Background Workers

> **Beginner frame:** Layer 3 workers are the control-room staff. They index chain events, wait for confirmations, anchor audit batches, and push revocations while the API handles user requests.

## 🌙 Welcome Back, Agent Architect!

The API handles requests. But who handles the world when no request is coming in? Who polls the chain for new events, confirms that enough blocks have passed, batches audit records into Merkle trees, and pushes revocations to subscribers?

Think of a **night watchman**: a scheduled worker who walks the perimeter on a fixed cycle — checking, confirming, batching, notifying — without being explicitly called. AgentLedger's four Layer 3 Celery tasks are that watchman, each with a different beat interval and a specific scope of responsibility.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain why all four Layer 3 tasks are necessary and cannot be merged into one
- ✅ Trace `index_chain_events` from Celery beat to `_persist_indexed_event`
- ✅ Explain why `confirm_chain_events` triggers `recompute_service_trust` and what happens next
- ✅ Describe `anchor_audit_batch` and how it interacts with `merkle.build_tree`
- ✅ Explain `run_with_fresh_session` and why `NullPool` is critical for forked workers
- ✅ Describe the beat schedule (5s / 5s / 60s / 60s) and the rationale for each interval

**Estimated time:** 60 minutes
**Prerequisites:** Lessons 22 (Chain Abstraction), 26 (Merkle/Audit), 27 (Federation)

---

## 🔍 What This Component Does

```
Celery Beat (scheduler)
    │
    ├── every 5s  → index_chain_events    → poll_remote_chain_events()
    │                                        └── eth_getLogs → _persist_indexed_event()
    │
    ├── every 5s  → confirm_chain_events  → confirm_pending_events()
    │                                        └── 20-block check → recompute_service_trust()
    │
    ├── every 60s → anchor_audit_batch    → anchor_pending_records()
    │                                        └── build_tree() → commitBatch() on-chain
    │
    └── every 60s → push_revocations      → dispatch_revocation_pushes()
                                             └── get_blocklist(since=) → POST webhooks
```

**Key files:**
- [`crawler/worker.py`](../../crawler/worker.py) — Celery app, beat schedule
- [`crawler/tasks/_async_db.py`](../../crawler/tasks/_async_db.py) — `run_with_fresh_session`
- [`crawler/tasks/index_chain_events.py`](../../crawler/tasks/index_chain_events.py) — beat wrapper
- [`crawler/tasks/confirm_chain_events.py`](../../crawler/tasks/confirm_chain_events.py) — beat wrapper
- [`crawler/tasks/anchor_audit_batch.py`](../../crawler/tasks/anchor_audit_batch.py) — beat wrapper
- [`crawler/tasks/push_revocations.py`](../../crawler/tasks/push_revocations.py) — beat wrapper

---

## 🏗️ The Beat Schedule

**File:** [`crawler/worker.py`](../../crawler/worker.py) lines 36–68

```python
app.conf.beat_schedule = {
    "index-chain-events": {
        "task": "crawler.index_chain_events",
        "schedule": 5,    # every 5 seconds
    },
    "confirm-chain-events": {
        "task": "crawler.confirm_chain_events",
        "schedule": 5,    # every 5 seconds
    },
    "anchor-audit-batch": {
        "task": "crawler.anchor_audit_batch",
        "schedule": 60,   # every minute
    },
    "push-revocations": {
        "task": "crawler.push_revocations",
        "schedule": 60,   # every minute
    },
}
```

**Why these intervals?**

| Task | Interval | Reason |
|------|----------|--------|
| `index_chain_events` | 5s | Need to see new blocks quickly; Polygon produces a block every ~2s |
| `confirm_chain_events` | 5s | Must promote events that crossed the 20-block window promptly |
| `anchor_audit_batch` | 60s | Gas cost makes frequent anchoring wasteful; 1-minute latency is acceptable for audit trails |
| `push_revocations` | 60s | Network I/O to subscribers; <60s delivery target is achievable with 1-minute beat + ~40s confirmation |

**Why run index and confirm at the same 5-second interval?** An event is indexed first (Celery beat fires `index_chain_events`), then on the next few beats it accumulates block depth, then `confirm_chain_events` promotes it. They're independent pipelines that can advance in parallel — running both every 5 seconds ensures neither creates a bottleneck for the other.

---

## 🏗️ The Celery App Configuration

**File:** [`crawler/worker.py`](../../crawler/worker.py) lines 13–70

```python
def create_celery_app() -> Celery | None:
    """Create the Celery app if Celery is installed."""
    if Celery is None:
        return None    # graceful degradation — test envs without Celery installed

    app = Celery("agentledger", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
    )
    app.conf.include = [
        "crawler.tasks.anchor_audit_batch",
        "crawler.tasks.confirm_chain_events",
        "crawler.tasks.crawl",
        "crawler.tasks.expire_identity_records",
        "crawler.tasks.index_chain_events",
        "crawler.tasks.push_revocations",
        "crawler.tasks.revalidate_service_identity",
        "crawler.tasks.verify_domain",
    ]
```

**`broker=settings.redis_url`:** Redis serves as both the task queue (broker) and result storage (backend). Tasks are serialized as JSON and enqueued in Redis. Workers pop tasks and execute them.

**`app.conf.include`:** Celery auto-discovers task modules listed here. Without this, the `@celery_app.task(name=...)` decorator is never executed and the task doesn't exist in the registry.

**Graceful Celery absence (line 15–16):** If Celery is not installed (e.g., in a stripped test environment), `Celery = None` from the `try/except ImportError` and `create_celery_app()` returns `None`. Every task module then uses the `else` branch — falling back to a plain Python function that can be called directly without Celery.

---

## 🏗️ `run_with_fresh_session` — The Isolation Helper

**File:** [`crawler/tasks/_async_db.py`](../../crawler/tasks/_async_db.py) lines 16–36

```python
async def run_with_fresh_session(
    operation: Callable[[AsyncSession], Awaitable[_T]],
) -> _T:
    """Run one async DB operation on a fresh engine/session pair.

    Celery background tasks are forked worker processes. Reusing the
    module-level async engine/session factory across those workers can lead to
    inherited asyncpg connection state. A per-task engine with ``NullPool``
    keeps these short periodic jobs isolated and deterministic.
    """
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        poolclass=NullPool,    # no connection pool — open/close per task
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await operation(session)
    finally:
        await engine.dispose()   # always clean up the engine, even on exception
```

**Why `NullPool`?** Normal SQLAlchemy connection pools maintain a pool of open connections. Celery workers are forked from the parent process. A forked process inherits the parent's file descriptors — including the pool's open TCP sockets to PostgreSQL. PostgreSQL connections are not fork-safe: both the parent and child would try to use the same socket, causing protocol corruption. `NullPool` avoids this by never holding connections between uses — each task opens a fresh connection and closes it when done.

**Why not reuse a module-level engine?** A module-level engine is created when the module is first imported — in the Celery worker process before forking. Each forked worker process then inherits that engine's internal state. With `NullPool`, the engine doesn't hold sockets, so this is safe. But the pattern `run_with_fresh_session` goes further: it creates a completely new engine on every task invocation. This maximizes isolation at the cost of one TCP handshake per task. For tasks running every 5 seconds, this is an acceptable overhead.

**The `finally: await engine.dispose()` guard:** Even if the `operation` raises an exception, the engine is disposed and its connections are closed. Without this, a long-running Celery worker would leak engine objects.

---

## 📝 Task 1: `index_chain_events`

**File:** [`crawler/tasks/index_chain_events.py`](../../crawler/tasks/index_chain_events.py)

```python
@celery_app.task(name="crawler.index_chain_events")
def index_chain_events_task() -> dict[str, int | str]:
    """Index remote Layer 3 chain events into the local query store."""
    return asyncio.run(_index_chain_events_async())

async def _index_chain_events_async() -> dict[str, int | str]:
    return await run_with_fresh_session(chain.poll_remote_chain_events)
```

**What `poll_remote_chain_events` does** (from `api/services/chain.py`):

In `CHAIN_MODE=web3`:
1. Calls `eth_getLogs` on the Polygon node with the contract address filter and the last known block range (`chain_start_block` → `latest_block`)
2. Decodes each log using the contract ABI event signatures
3. Calls `_persist_indexed_event()` for each decoded event

In `CHAIN_MODE=local`:
- Returns `{"indexed": 0, "mode": "local"}` immediately — local mode has no remote chain to poll

**`_persist_indexed_event()` idempotency** (from `api/services/chain.py`):
```python
INSERT INTO chain_events (...)
ON CONFLICT (tx_hash) DO NOTHING
```

If the same event is seen in multiple `eth_getLogs` windows (because the polling window overlaps), the `ON CONFLICT` guard silently discards the duplicate. This makes the indexer safe to run repeatedly without accumulating duplicate rows.

**Failure mode:** If the web3 provider is unavailable, `eth_getLogs` raises an exception. The task catches it, logs it, and returns `{"indexed": 0, "error": "..."}`. The next 5-second beat tries again. No data is lost — the event will be seen in the next successful window.

---

## 📝 Task 2: `confirm_chain_events`

**File:** [`crawler/tasks/confirm_chain_events.py`](../../crawler/tasks/confirm_chain_events.py)

```python
@celery_app.task(name="crawler.confirm_chain_events")
def confirm_chain_events_task() -> dict[str, int]:
    """Confirm synthetic Layer 3 chain events past the safety window."""
    return asyncio.run(_confirm_chain_events_async())

async def _confirm_chain_events_async() -> dict[str, int]:
    return await run_with_fresh_session(chain.confirm_pending_events)
```

**What `confirm_pending_events` does** (from `api/services/chain.py`):

```python
# Find events past the 20-block window
events = SELECT id, service_id, event_type FROM chain_events
         WHERE is_confirmed = false
           AND block_number <= latest_block - confirmation_blocks

# For each event:
UPDATE chain_events SET is_confirmed=true, confirmed_at=NOW()
UPDATE attestation_records SET is_confirmed=true  (for attestation events)
recompute_service_trust(db, service_id)           ← the cascade
```

**Why this task is separate from indexing:** The 20-block window requires knowledge of the current latest block — which changes over time. An event indexed when the chain was at block 1000 needs to wait until block 1020 to confirm. The indexer doesn't know the future block number; the confirmer runs later and checks. Separating them lets each run at its own pace.

**The cascade:** When `confirm_pending_events` confirms an event, it calls `recompute_service_trust(db, service_id)`. This is the moment the service's `trust_score`, `trust_tier`, and `is_banned` fields update. The trust recompute is synchronous within the confirmation task — it completes before the task returns.

**In `CHAIN_MODE=local`:** Synthetic events are written with `block_number=0` and `is_confirmed=false`. The `confirm_pending_events` function treats `latest_block - confirmation_blocks` as a sentinel: in local mode, `latest_block` is set to a large number so that `block_number=0 <= latest_block - 20` is always true, confirming all local events immediately on the next beat.

> **Recommended (not implemented here):** In a production deployment with high-throughput attestations, `confirm_pending_events` could batch its `recompute_service_trust` calls and run them concurrently using `asyncio.gather`. Currently they run sequentially — fine for low volume, but would slow under heavy load.

---

## 📝 Task 3: `anchor_audit_batch`

**File:** [`crawler/tasks/anchor_audit_batch.py`](../../crawler/tasks/anchor_audit_batch.py)

```python
@celery_app.task(name="crawler.anchor_audit_batch")
def anchor_audit_batch_task() -> dict[str, object]:
    """Anchor pending audit records into the next Layer 3 batch."""
    return asyncio.run(_anchor_audit_batch_async())

async def _anchor_audit_batch_async() -> dict[str, object]:
    return await run_with_fresh_session(audit.anchor_pending_records)
```

**What `anchor_pending_records` does** (from `api/services/audit.py`, covered in Lesson 26):

1. `SELECT ... FROM audit_records WHERE is_anchored=false LIMIT :batch_size`
2. Build a Merkle tree from the `record_hash` values
3. `INSERT INTO audit_batches (merkle_root, record_count, status)` → get `batch_id`
4. Call `commitBatch(merkle_root)` on the `AuditChain` contract
5. `UPDATE audit_records SET batch_id=:batch_id, merkle_proof=:proof, tx_hash=:tx_hash, is_anchored=true`

**The 100x gas savings:** Without batching, 100 audit records would require 100 separate `commitBatch` calls — 100 transactions, 100 gas payments. With Merkle batching, the same 100 records produce one Merkle root and require exactly 1 transaction. The `audit_anchor_batch_size` config variable (default 100) sets the maximum batch size.

**Why 60 seconds between anchoring beats?** Audit records don't need real-time on-chain anchoring. A 60-second batch window accumulates records from all API activity and then commits them in one transaction. Shorter intervals would waste gas on small batches; longer intervals would delay `integrity_valid` status for new records.

**If no records are pending:** `anchor_pending_records` returns `{"anchored": 0, "batch_id": null}` immediately. No on-chain transaction is made. The task is idempotent.

---

## 📝 Task 4: `push_revocations`

**File:** [`crawler/tasks/push_revocations.py`](../../crawler/tasks/push_revocations.py)

```python
@celery_app.task(name="crawler.push_revocations")
def push_revocations_task() -> dict[str, int]:
    """Push confirmed Layer 3 revocations to subscribers."""
    return asyncio.run(_push_revocations_async())

async def _push_revocations_async() -> dict[str, int]:
    return await run_with_fresh_session(federation.dispatch_revocation_pushes)
```

**What `dispatch_revocation_pushes` does** (covered fully in Lesson 27):
- Fetches all active subscribers with a webhook URL
- For each subscriber: builds a differential payload using `last_push_at` as cursor
- Signs the payload with Ed25519, POSTs to the webhook
- Advances `last_push_at` on success; increments `push_failure_count` on failure

**Interaction with `confirm_chain_events`:** The push task fires at 60-second intervals. For a revocation to appear in the blocklist that `dispatch_revocation_pushes` fetches, it must be `is_confirmed=true`. Since `confirm_chain_events` fires every 5 seconds, the typical sequence is:

```
t=0:   Revocation submitted (is_confirmed=false)
t≈5s:  Celery: confirm_chain_events fires
       In local mode: event confirmed immediately
       In web3 mode: must wait for 20 blocks (~40s)
t≈60s: Celery: push_revocations fires
       get_blocklist(since=last_push_at) includes the newly confirmed revocation
       Subscriber receives the push
```

---

## 🏗️ Why Four Separate Tasks?

Could all four be one task? No — they have incompatible:

| Property | index | confirm | anchor | push |
|----------|-------|---------|--------|------|
| Interval | 5s | 5s | 60s | 60s |
| Depends on external chain | Yes (web3) | Reads block height | Maybe | No |
| Writes to chain | No | No | Yes | No |
| Triggers recompute | No | Yes | No | No |
| Modifies blocklist | No | Yes | No | No |

Merging `index` and `confirm` would force both to run at the same interval — but confirm is harmless to run at 5s while index's `eth_getLogs` call is the expensive one. Merging `anchor` with `confirm` would fire the on-chain anchor transaction every 5 seconds (expensive). Merging `push` with `confirm` would push revocations before the confirmation window has fully passed. The separation is correct.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Create audit records, manually trigger anchor, verify batch

```bash
# Create 3 audit records via the API
for i in 1 2 3; do
  curl -s -X POST http://localhost:8000/v1/audit/records \
    -H "X-API-Key: dev-local-only" \
    -H "Content-Type: application/json" \
    -d "{
      \"agent_did\": \"did:key:test-agent-$i\",
      \"action_type\": \"tool_invocation\",
      \"action_context\": {\"tool\": \"web_search\", \"input_type\": \"query_string\"},
      \"outcome\": \"success\"
    }" | python3 -m json.tool
done

# Check that records are unanchored
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT id, is_anchored, batch_id FROM audit_records ORDER BY created_at DESC LIMIT 3;"
```

**Expected:** `is_anchored=false`, `batch_id=NULL` for all three.

```bash
# Manually run the anchor task
docker compose exec api python3 -c "
import asyncio
from crawler.tasks._async_db import run_with_fresh_session
from api.services import audit

async def run():
    result = await run_with_fresh_session(audit.anchor_pending_records)
    print(result)

asyncio.run(run())
"
```

**Expected output:**
```json
{"anchored": 3, "batch_id": "<uuid>", "tx_hash": "0x..."}
```

```bash
# Verify records are now anchored with Merkle proofs
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT id, is_anchored, batch_id, merkle_proof IS NOT NULL AS has_proof FROM audit_records ORDER BY created_at DESC LIMIT 3;"
```

**Expected:** `is_anchored=true`, `batch_id=<same uuid>`, `has_proof=true`.

### 🔬 Exercise 2: Manually run confirm task and observe trust recompute

```bash
# First, make sure an attestation exists (from previous lessons)
# Check current trust tier and score for a service
SERVICE_DOMAIN="<YOUR_DOMAIN>"
curl -s "http://localhost:8000/v1/services/${SERVICE_DOMAIN}" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool

# Manually run the confirm task
docker compose exec api python3 -c "
import asyncio
from crawler.tasks._async_db import run_with_fresh_session
from api.services import chain

async def run():
    result = await run_with_fresh_session(chain.confirm_pending_events)
    print(result)

asyncio.run(run())
"
```

**Expected output:**
```json
{"confirmed": 1, "recomputed": ["<service_id>"]}
```

```bash
# Check that trust score and tier updated
curl -s "http://localhost:8000/v1/services/${SERVICE_DOMAIN}" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

### 🔬 Exercise 3 (Failure): Run index task with invalid web3 provider URL

```bash
# Temporarily override the provider URL to something invalid
docker compose exec api python3 -c "
import asyncio
import os
os.environ['WEB3_PROVIDER_URL'] = 'https://invalid.provider.fake'
os.environ['CHAIN_MODE'] = 'web3'

from api.config import settings
# Force reload — show what would happen
from api.services.chain import poll_remote_chain_events
from crawler.tasks._async_db import run_with_fresh_session

async def run():
    try:
        result = await run_with_fresh_session(poll_remote_chain_events)
        print('Result:', result)
    except Exception as e:
        print('Caught exception (task would log and continue):', type(e).__name__, str(e)[:100])

asyncio.run(run())
"
```

**Expected behavior:** An exception is caught (connection error to the invalid provider). In a real Celery worker, this exception propagates to Celery, which logs it and schedules the next beat normally. The worker process does **not** crash — Celery's task isolation means one failed task doesn't kill the worker. The 5-second interval continues, and the next beat retries.

> **Key point:** No task in the Layer 3 suite uses `autoretry_for` or `max_retries`. They rely on the beat interval itself for retry — the next beat is the retry. This is appropriate for periodic polling tasks but not for tasks that must guarantee delivery (which would need explicit retry logic).

---

## 📊 Summary Reference Card

| Task | File | Beat Interval | Core Function |
|------|------|---------------|---------------|
| `index_chain_events` | `crawler/tasks/index_chain_events.py` | 5s | `chain.poll_remote_chain_events` |
| `confirm_chain_events` | `crawler/tasks/confirm_chain_events.py` | 5s | `chain.confirm_pending_events` |
| `anchor_audit_batch` | `crawler/tasks/anchor_audit_batch.py` | 60s | `audit.anchor_pending_records` |
| `push_revocations` | `crawler/tasks/push_revocations.py` | 60s | `federation.dispatch_revocation_pushes` |

| Item | Value |
|------|-------|
| Celery broker | Redis (`settings.redis_url`) |
| Session isolation | `NullPool` per task (`_async_db.py`) |
| Task serializer | JSON |
| Timezone | UTC |
| Layer 3 task discovery | `app.conf.include` list in `worker.py` |
| Idempotency guard | `ON CONFLICT (tx_hash) DO NOTHING` in index |
| Graceful Celery absence | `if Celery is None: return None` in `create_celery_app()` |

---

## 📚 Interview Preparation

**Q: Why not anchor audit records individually as each one is created?**

**A:** On-chain transactions cost gas. If the system creates 1000 audit records per hour, anchoring each one individually would cost 1000 transactions. Merkle batching anchors all 1000 with 1 transaction. The 60-second batch interval is the latency cost of this optimization: a newly created audit record's `is_anchored` status takes up to 60 seconds to flip. For the audit use case (compliance trail, not real-time verification), this latency is acceptable.

**Q: What's the trade-off between a 5-second and a 60-second confirmation interval?**

**A:** A 5-second interval means trust score updates propagate within 5 seconds of an event clearing the 20-block window. A 60-second interval would add up to 60 seconds of additional latency to trust changes. For a revocation, 5 seconds means the service is marked `is_banned=true` quickly, minimizing the window where it appears trustworthy after being revoked. The cost is more frequent DB queries (every 5s vs. every 60s), but since `confirm_pending_events` is a simple query with no expensive joins, this is acceptable.

**Q: Why use `asyncio.run()` inside a Celery task?**

**A:** Celery workers are synchronous by default — they run in a regular Python thread, not an asyncio event loop. The service functions (`poll_remote_chain_events`, `anchor_pending_records`, etc.) are all `async def` because the FastAPI app uses async SQLAlchemy. `asyncio.run()` creates a fresh event loop for each task invocation, runs the async function to completion, and tears the loop down. This is the standard bridge pattern between synchronous Celery and async Python code.

**Q: What happens if two Celery workers try to anchor the same audit records simultaneously?**

**A:** `anchor_pending_records` uses a `SELECT ... WHERE is_anchored=false LIMIT :batch_size` query followed by an `UPDATE ... SET is_anchored=true` before the on-chain call. Without explicit row locking (`SELECT FOR UPDATE`), two workers could read the same unanchored records. The current implementation doesn't use `FOR UPDATE` — in a high-concurrency scenario, the same records could be included in two batches, producing duplicate Merkle proofs with different roots. In the current single-worker Celery deployment this isn't an issue, but it's a known race condition if workers scale horizontally.

> **Recommended (not implemented here):** Add `SELECT ... FOR UPDATE SKIP LOCKED` to the anchor query to atomically lock the batch. `SKIP LOCKED` means a second concurrent worker would skip already-locked records and process the next available batch.

---

## ✅ Key Takeaways

- All four Layer 3 tasks are distinct and cannot be merged: they have different intervals (5s vs 60s), different dependencies (chain read vs chain write), and different downstream effects (trust recompute vs push delivery)
- `run_with_fresh_session` creates a fresh async engine with `NullPool` on every task invocation — critical because Celery workers are forked processes that cannot safely inherit parent-process connection state
- `index_chain_events` uses `ON CONFLICT (tx_hash) DO NOTHING` for idempotent upsert — the same on-chain event can be seen multiple times without creating duplicates
- `confirm_chain_events` is the trust update trigger: when it promotes an event to `is_confirmed=true`, it immediately calls `recompute_service_trust()` for the affected service
- `anchor_audit_batch` achieves 100x gas cost reduction by batching up to 100 audit records into a single Merkle root and calling `commitBatch()` once
- If Celery is not installed, all tasks fall back to plain Python functions callable directly — useful for testing without a Redis broker

---

## 🚀 Ready for Lesson 29?

Next up: **The Inspector General — Live Amoy Acceptance Run**. We'll run the full 10-criterion acceptance sequence against the real Polygon Amoy testnet, record our own transaction hashes, and independently verify an attestation event with a direct `eth_getLogs` call — without trusting AgentLedger's API at all.

*Remember: A night watchman who falls asleep once is more dangerous than no watchman at all. These tasks are designed to fail gracefully — and then try again on the next beat.* 🌙
