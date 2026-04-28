# Lesson 19 — The Night Shift: Background Workers & Redis Patterns

**Layer:** 2 — Identity & Credentials  
**Files:** `crawler/tasks/expire_identity_records.py` (50 lines), `crawler/tasks/revalidate_service_identity.py` (85 lines), `crawler/worker.py` (85 lines)  
**Prerequisites:** Lessons 14–16 — the workers maintain the state managed by those service functions  
**Estimated time:** 60 minutes

---

## Welcome

A law firm doesn't wait for clients to call and ask whether their retainer has expired. Clerks run a nightly sweep: pull up every active engagement, check the expiry date, and send a reminder or close the file. The lawyers never think about this — it just happens.

Layer 2 background workers do the same thing. Two Celery tasks run on a schedule without any operator trigger:

| Task | Schedule | Purpose |
|---|---|---|
| `expire_identity_records` | Every 60 seconds | Bulk-expire stale authorization requests; delete old session assertions |
| `revalidate_service_identity` | Every 24 hours | Re-fetch `did:web` documents for all active services; update `last_verified_at` |

By the end of this lesson you will be able to:

- Explain why Celery workers use `psycopg2` (synchronous) rather than asyncpg/SQLAlchemy
- Describe the `_impl()` / Celery-wrapper separation pattern and why it enables testing
- Trace `expire_identity_records` end-to-end, including the DELETE that controls table growth
- Explain why `revalidate_service_identity` uses `asyncio.run()` and `force_refresh=True`
- Describe the per-service error isolation in revalidation
- Read the beat schedule in `worker.py` and map each task to its interval

---

## What This Connects To

**Lesson 14:** `prewarm_revocation_set()` seeds the Redis revocation SET at startup. The background workers don't use Redis directly, but the cleanup they perform (expiring authorization records) keeps the data that Redis caches accurate and current.

**Lesson 15:** `get_session_status()` has a lazy expiry for authorization requests. `expire_identity_records` is the scheduled equivalent — it catches all requests that were never polled.

**Lesson 16:** `activate_service_identity()` sets `last_verified_at` on initial activation. `revalidate_service_identity` refreshes that timestamp nightly so services don't quietly accumulate stale identity states.

---

## Architecture Position

```
Celery Beat scheduler
     │
     ├─ every 60s ──► expire_identity_records
     │                    │
     │                    ├── UPDATE authorization_requests (pending → expired)
     │                    └── DELETE session_assertions (expired rows)
     │
     └─ every 24h ──► revalidate_service_identity
                          │
                          ├── SELECT services WHERE last_verified_at IS NOT NULL
                          └── for each service:
                                ├── validate_signed_manifest(force_refresh=True)
                                ├── UPDATE services.last_verified_at
                                └── INSERT crawl_events (pass or fail)
```

---

## Core Concepts

### Why psycopg2 Instead of asyncpg?

The FastAPI application uses `asyncpg` (via SQLAlchemy's async engine) because it runs in an `asyncio` event loop. Celery workers are **synchronous processes** — they don't run an event loop by default. Using `asyncpg` in a synchronous context would require managing an event loop manually and is error-prone.

`psycopg2` is the standard synchronous PostgreSQL driver for Python. `get_sync_connection()` (in `worker.py`) returns a plain psycopg2 connection that works naturally in a synchronous task without any async machinery.

The trade-off: background tasks can't share the same connection pool as the FastAPI app. Each task opens its own connection, uses it, and closes it in the `finally` block. This is correct for batch operations that are scheduled infrequently — connection overhead per run is negligible compared to the work done.

### The `_impl()` / Task Wrapper Pattern

Both worker files follow the same structure:

```python
def _expire_identity_records_impl() -> dict[str, int]:
    """The actual work — no Celery dependency."""
    ...

if celery_app is not None:
    @celery_app.task(name="crawler.expire_identity_records")
    def expire_identity_records() -> dict[str, int]:
        return _expire_identity_records_impl()
```

**Why separate them?**

1. **Testability** — `_impl()` is a plain function. Tests can call it directly without a Celery worker running, a Redis broker, or any Celery machinery. The test suite calls `_expire_identity_records_impl()` directly with a test database connection.

2. **Optional dependency** — `celery_app` is `None` when Celery is not installed (the `try/except ImportError` in `worker.py`). The `if celery_app is not None` guard means the module imports cleanly in environments without Celery — for example, the test suite running with minimal dependencies.

3. **Decoupled responsibility** — the `@celery_app.task` decorator registers the function with the broker and handles serialization, retries, and result backend. The `_impl` function only handles database logic. If you ever need to call the task logic from a different context (a migration script, a one-off maintenance command), you call `_impl()` directly.

### The Beat Schedule

From `worker.py` (lines 36–69):

```python
app.conf.beat_schedule = {
    "expire-identity-records": {
        "task": "crawler.expire_identity_records",
        "schedule": 60,           # every minute
    },
    "revalidate-service-identity": {
        "task": "crawler.revalidate_service_identity",
        "schedule": 60 * 60 * 24, # every 24 hours
    },
    # Layer 3 tasks:
    "index-chain-events":   {"schedule": 5},    # every 5 seconds
    "confirm-chain-events": {"schedule": 5},    # every 5 seconds
    "anchor-audit-batch":   {"schedule": 60},   # every minute
    "push-revocations":     {"schedule": 60},   # every minute
    # Layer 1 tasks:
    "crawl-all-active-services":     {"schedule": 86400},  # every 24 hours
    "verify-all-pending-domains":    {"schedule": 86400},  # every 24 hours
}
```

The Layer 2 tasks sit in the middle of the spectrum:
- `expire_identity_records` at 60 seconds: short enough that a session assertion with a 5-minute TTL won't accumulate more than ~1 minute of stale rows before being cleaned
- `revalidate_service_identity` at 24 hours: appropriate for a property that changes rarely (DID documents) and is expensive to re-fetch for every service in the registry

---

## Code Walkthrough

### 1. `expire_identity_records` (lines 8–49)

```python
def _expire_identity_records_impl() -> dict[str, int]:
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            # Step 1: Expire stale pending authorization requests
            cur.execute("""
                UPDATE authorization_requests
                SET status = 'expired',
                    decided_at = COALESCE(decided_at, NOW())
                WHERE status = 'pending'
                  AND expires_at <= NOW()
            """)
            expired_authorizations = cur.rowcount

            # Step 2: Delete expired session assertions
            cur.execute("""
                DELETE FROM session_assertions
                WHERE expires_at <= NOW()
            """)
            pruned_sessions = cur.rowcount

        conn.commit()
        return {
            "expired_authorizations": expired_authorizations,
            "pruned_sessions": pruned_sessions,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**Step 1 — Authorization expiry:** The same UPDATE that `list_pending_authorizations()` runs lazily (Lesson 17), but here it runs on a schedule without an operator trigger. `COALESCE(decided_at, NOW())` protects against overwriting a `decided_at` that was somehow already set (shouldn't happen for pending records, but defensive).

**Step 2 — Session assertion DELETE:** This is different from the authorization expiry — it permanently deletes rows rather than updating a status flag. Why?

- Authorization requests need a history trail: operators might want to see "this request expired at 14:32" in audit logs. The `status='expired'` row persists for reporting.
- Session assertions are security tokens. Once expired, they provide no information value. Keeping them would grow the `session_assertions` table unboundedly over time. Deleting expired rows is both correct (an expired token can never be redeemed) and operationally necessary.

**Return counts:** The task returns `{"expired_authorizations": N, "pruned_sessions": M}`. Celery stores this in the result backend (Redis). Monitoring systems can observe these counts via `celery inspect result <task_id>` or Flower.

**The `with conn.cursor()` block:** The cursor is a context manager — `__exit__` calls `cursor.close()`. The `finally: conn.close()` ensures the connection is returned to the OS even if the transaction block raises. This is the correct resource management pattern for psycopg2 in one-shot tasks.

### 2. `revalidate_service_identity` (lines 13–84)

```python
def _revalidate_service_identity_impl() -> dict[str, int]:
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            # Select all services that have been identity-activated at least once
            cur.execute("""
                SELECT s.id, s.domain, m.raw_json
                FROM services s
                JOIN manifests m
                    ON m.service_id = s.id
                   AND m.is_current = true
                WHERE s.last_verified_at IS NOT NULL
            """)
            rows = cur.fetchall()

            checked = 0; revalidated = 0; failed = 0
            for service_id, domain, raw_json in rows:
                checked += 1
                try:
                    manifest = ServiceManifest.model_validate(raw_json)
                    asyncio.run(
                        service_identity.validate_signed_manifest(
                            manifest=manifest,
                            force_refresh=True,    # bypass the 10-minute Redis cache
                        )
                    )
                    cur.execute(
                        "UPDATE services SET last_verified_at = NOW(), updated_at = NOW() WHERE id = %s",
                        (service_id,),
                    )
                    cur.execute(
                        "INSERT INTO crawl_events ... VALUES (%s, 'service_identity_revalidated', ...)",
                        (service_id, domain),
                    )
                    revalidated += 1
                except Exception as exc:
                    cur.execute(
                        "INSERT INTO crawl_events ... VALUES (%s, 'service_identity_revalidation_failed', ...)",
                        (service_id, domain, json.dumps({"error": str(exc)})),
                    )
                    failed += 1

        conn.commit()
        return {"checked": checked, "revalidated": revalidated, "failed": failed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

**`WHERE s.last_verified_at IS NOT NULL`:** Only revalidate services that have been through the Layer 2 identity activation flow at least once. Services with no identity state don't have a `did:web` document to re-fetch, and attempting to validate them would only generate errors.

**`asyncio.run(...)` inside a sync task (line 37):**

`validate_signed_manifest` is an async function — it calls `_fetch_did_web_document` which uses `httpx.AsyncClient`. From within a synchronous Celery task, the correct way to call an async function is `asyncio.run()`, which:
1. Creates a new event loop
2. Schedules the coroutine and runs it to completion
3. Closes the loop

Each service gets its own `asyncio.run()` call and its own event loop lifecycle. This is slightly inefficient (creating/destroying a loop per service) but correct. The alternative — reusing one event loop across iterations — requires careful management to avoid loop reuse after exceptions.

**`force_refresh=True`:** The standard `validate_signed_manifest` path uses a 10-minute Redis cache for `did:web` documents. The revalidation job's entire purpose is to detect changed DID documents — using the cache would defeat that purpose. `force_refresh=True` forces a live HTTPS fetch regardless of cache state.

**Per-service error isolation (lines 60–68):**

```python
try:
    ...validate...
    revalidated += 1
except Exception as exc:
    cur.execute("INSERT INTO crawl_events ... 'service_identity_revalidation_failed' ...",
                (service_id, domain, json.dumps({"error": str(exc)})))
    failed += 1
```

The `except` is inside the `for` loop. If service A's `did:web` document is down, service A is marked as failed in `crawl_events` and the loop continues to service B. One unavailable service does not abort the entire nightly revalidation run.

The failure record writes the error string to `crawl_events.details`. This makes failures visible in any query against `crawl_events` — no separate error log table is needed.

**The outer `except Exception: conn.rollback()`:** This catches catastrophic failures (DB connection dropped, psycopg2 error in the cursor context). Individual service failures are handled by the inner try/except and committed with the rest of the batch. The outer guard only triggers for infrastructure-level failures.

---

## The Full Worker Module

From `worker.py`:

```python
try:
    from celery import Celery
except ImportError:
    Celery = None

def create_celery_app() -> Celery | None:
    if Celery is None:
        return None
    app = Celery("agentledger", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
    )
    app.conf.include = [...]
    app.conf.beat_schedule = {...}
    return app

celery_app = create_celery_app()

def get_sync_connection():
    import psycopg2
    return psycopg2.connect(settings.database_url_sync)
```

**Why `celery_app = create_celery_app()` at module level?** Celery's task decorator (`@celery_app.task`) requires the app object at decoration time (when the module is imported). Defining it at module level ensures it's available when `crawler/tasks/*.py` modules are imported.

**`task_serializer="json"` and `result_serializer="json"`:** Task arguments and results are serialized as JSON. This ensures task payloads are human-readable, schema-validatable, and safe to transmit through the broker without security concerns around binary serialization formats.

**`timezone="UTC"` and `enable_utc=True`:** All beat schedule times and task timestamps use UTC. Without this, Celery might interpret schedule times in the local timezone of the worker process, which can shift unexpectedly with DST changes.

**`database_url_sync`:** This is a separate config value from `database_url` (the async URL). The difference is the driver prefix: `postgresql+asyncpg://...` for async SQLAlchemy vs. `postgresql://...` for psycopg2.

---

## Exercises

### Exercise 1 — Run `expire_identity_records` manually

```bash
# Create some session assertions that are already expired (set expires_at to the past)
docker compose exec db psql -U agentledger -c "
INSERT INTO session_assertions (assertion_jti, agent_did, service_id, ontology_tag,
    assertion_token, expires_at, was_used, issued_at)
SELECT
    gen_random_uuid()::text,
    'did:key:z6MkTest',
    (SELECT id FROM services LIMIT 1),
    'search.manifest.lookup',
    'fake-expired-token',
    NOW() - interval '10 minutes',
    false,
    NOW() - interval '10 minutes'
FROM generate_series(1, 5);
"

# Check the count before
docker compose exec db psql -U agentledger -c "SELECT COUNT(*) FROM session_assertions WHERE expires_at <= NOW();"

# Run the task directly (in the API container where the code is importable)
docker compose run --rm api python -c "
from crawler.tasks.expire_identity_records import _expire_identity_records_impl
result = _expire_identity_records_impl()
print('Pruned:', result)
"

# Check the count after
docker compose exec db psql -U agentledger -c "SELECT COUNT(*) FROM session_assertions WHERE expires_at <= NOW();"
```

Expected output:
```
Pruned: {'expired_authorizations': 0, 'pruned_sessions': 5}
```

### Exercise 2 — Run `revalidate_service_identity` with a real service

```bash
# First activate a service identity (requires a running service with did:web)
# Then run the revalidation task
docker compose run --rm api python -c "
from crawler.tasks.revalidate_service_identity import _revalidate_service_identity_impl
result = _revalidate_service_identity_impl()
print('Revalidation result:', result)
"

# Check the crawl_events for revalidation activity
docker compose exec db psql -U agentledger -c "
SELECT domain, event_type, details->>'error' AS error, created_at
FROM crawl_events
WHERE event_type IN ('service_identity_revalidated', 'service_identity_revalidation_failed')
ORDER BY created_at DESC
LIMIT 5;
"
```

Expected output (all passing):
```
Revalidation result: {'checked': 3, 'revalidated': 3, 'failed': 0}
```

Expected output (one DID document unavailable):
```
Revalidation result: {'checked': 3, 'revalidated': 2, 'failed': 1}
```

And in the database:
```
  domain            | event_type                           | error
--------------------+--------------------------------------+----------------------
 broken.example.com | service_identity_revalidation_failed | unable to resolve ...
 ok.example.com     | service_identity_revalidated         |
```

### Exercise 3 — Observe the Celery beat schedule

```bash
# With the worker running
docker compose run --rm celery celery -A crawler.worker beat --loglevel=info

# Look for lines like:
# [2026-04-28 12:00:00,000: INFO/MainProcess] Scheduler: Sending due task expire-identity-records
```

Or inspect the beat schedule programmatically:

```python
docker compose run --rm api python -c "
from crawler.worker import celery_app
if celery_app:
    for name, config in celery_app.conf.beat_schedule.items():
        sched = config['schedule']
        if sched < 60:
            unit = f'{sched}s'
        elif sched < 3600:
            unit = f'{sched // 60}m'
        else:
            unit = f'{sched // 3600}h'
        print(f'{name:40} every {unit}')
else:
    print('Celery not available')
"
```

### Exercise 4 (failure) — Verify per-service error isolation

```bash
# Set one service's manifest to have an invalid signature
docker compose exec db psql -U agentledger -c "
UPDATE manifests
SET raw_json = raw_json || '{\"signature\": {\"value\": \"invalidsignature\"}}'::jsonb
WHERE service_id = (SELECT id FROM services WHERE last_verified_at IS NOT NULL LIMIT 1)
  AND is_current = true;
"

# Run revalidation — one failure should not abort the batch
docker compose run --rm api python -c "
from crawler.tasks.revalidate_service_identity import _revalidate_service_identity_impl
result = _revalidate_service_identity_impl()
print('Result:', result)
"
```

Expected output (one failure doesn't abort the batch):
```
Result: {'checked': N, 'revalidated': N-1, 'failed': 1}
```

---

## Best Practices

### What AgentLedger does

- **`_impl()` / task wrapper separation** — implementations are testable without Celery infrastructure
- **Per-service error isolation in revalidation** — one failing service logs the error and continues; the batch doesn't abort
- **DELETE for session assertions** — expired tokens have no historical value; hard delete controls table size
- **UPDATE (not delete) for authorization records** — expiry history is operationally valuable; status update preserves the record
- **`force_refresh=True` for revalidation** — bypasses the Redis cache so DID document changes take effect within one revalidation cycle
- **`try/except ImportError` for Celery** — module imports cleanly in test environments without Celery installed

### Recommended (not implemented here)

- **Incremental expiry with `LIMIT`** — the current DELETE and UPDATE have no row limit. Under very high load (millions of rows), a single large DELETE could hold a table lock for seconds. Adding `WHERE id IN (SELECT id FROM ... LIMIT 10000)` and looping would keep lock times bounded.
- **Revalidation staggering** — the nightly revalidation fetches all active services' DID documents in a tight loop. Under a large registry, this could spike HTTPS traffic to external hosts. A rate limit (e.g., `asyncio.sleep(0.1)` between services, or a semaphore) would spread the load.
- **Task failure alerting** — when `_revalidate_service_identity_impl` raises from the outer `except Exception`, the Celery result backend records the failure, but no alert fires automatically. Configuring a failure handler or monitoring on failure count would surface infrastructure-level failures to on-call operators.

---

## Interview Q&A

**Q: Why do background workers use psycopg2 instead of the async SQLAlchemy engine?**

A: Celery tasks execute in synchronous Python processes without an event loop. The async SQLAlchemy engine (asyncpg) requires a running `asyncio` event loop — it can't be called from synchronous code without managing the loop manually. psycopg2 is the standard synchronous PostgreSQL driver and works naturally in Celery tasks. The two drivers are used side-by-side: asyncpg for the FastAPI app (I/O-bound, event-loop-based), psycopg2 for Celery workers (synchronous, batch-oriented).

**Q: Why does `revalidate_service_identity` call `asyncio.run()` for each service instead of running the entire loop inside one event loop?**

A: `validate_signed_manifest` is async, but the calling code (`_revalidate_service_identity_impl`) is synchronous. `asyncio.run()` is the correct bridge — it creates a fresh event loop, runs the coroutine to completion, and closes the loop. Reusing a single event loop across iterations is possible but requires explicit lifecycle management and careful error handling to ensure the loop isn't closed prematurely. The per-iteration `asyncio.run()` is simpler and equally correct for a task that runs once per day with no latency-sensitivity.

**Q: What happens if `expire_identity_records` runs while a user is simultaneously approving an authorization request?**

A: The task's UPDATE targets rows `WHERE status = 'pending' AND expires_at <= NOW()`. An authorization request being actively approved has `status='pending'` but (if it's being processed promptly) `expires_at > NOW()` — so it doesn't match the UPDATE's WHERE clause. The `FOR UPDATE` lock in `approve_authorization_request` further ensures that if the task and the approve handler both read the same row simultaneously, only one can hold the write lock. In practice, conflicts are rare because the task only targets expired rows.

**Q: Why is session assertion pruning done with DELETE rather than a status flag?**

A: Session assertions are one-use cryptographic tokens. Once expired (or redeemed), they serve no operational purpose — they can't be reused, re-issued, or meaningfully queried. Unlike authorization records (which represent human decisions that operators may want to review), expired session assertions have no residual value. Hard deletion is both correct (the token is gone) and practical (without it, a high-volume deployment would accumulate millions of expired rows, degrading query performance on the `session_assertions` table).

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 19 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ Worker schedule (Layer 2)                                       │
│   expire_identity_records:       every 60s                     │
│   revalidate_service_identity:   every 24h                     │
│                                                                 │
│ expire_identity_records writes                                  │
│   UPDATE authorization_requests: pending → expired             │
│   DELETE session_assertions:     expires_at <= NOW()           │
│   Returns: {expired_authorizations, pruned_sessions}           │
│                                                                 │
│ revalidate_service_identity writes                             │
│   SELECT services WHERE last_verified_at IS NOT NULL           │
│   Per-service: validate_signed_manifest(force_refresh=True)    │
│   Pass: UPDATE services.last_verified_at + crawl_event         │
│   Fail: INSERT crawl_event (error details preserved)           │
│   Returns: {checked, revalidated, failed}                      │
│                                                                 │
│ Implementation patterns                                        │
│   _impl() function: testable without Celery                    │
│   if celery_app is not None: task wrapper                      │
│   get_sync_connection(): psycopg2 for sync workers             │
│   asyncio.run(): bridge from sync Celery to async services     │
│   force_refresh=True: bypass Redis cache in revalidation       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 20 — The Final Debrief** is the Layer 2 synthesis lesson: a full end-to-end walkthrough from HTTP request to chain of custody, the test coverage map, a hardening analysis, and a complete interview preparation section covering every Layer 2 component you've studied in Lessons 11–19.
