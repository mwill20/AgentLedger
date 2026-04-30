# Lesson 43: The Submissions Window — Workflow CRUD: Create, List & Retrieve

> **Beginner frame:** Workflow CRUD is the filing desk for workflow records. It creates, lists, retrieves, updates, and publishes specs while keeping lifecycle state explicit.

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_registry.py` (lines 537â€“948), `api/routers/workflows.py`
**Prerequisites:** Lesson 42
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

A government patent office has three operations at its core: accept a submission, search the archive, and retrieve a filing. Layer 5's workflow CRUD is exactly that office: `create_workflow()` takes the submission, `list_workflows()` powers the archive search, and `get_workflow()` retrieves the full dossier.

This lesson traces the complete database path for each operation â€” including the three Redis caches that make the read surface fast enough to pass the p95 < 200ms load test.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Trace `create_workflow()` through its four inserts and one commit
- Explain why `update_workflow_spec()` deletes and re-inserts step rows rather than updating them
- Read the `list_workflows()` SQL and identify why it uses two queries (not a window function)
- Describe the three Redis cache keys and their invalidation strategy
- Explain why the list cache key is a SHA-256 hash of its filter parameters
- Trace `enforce_workflow_query_rate_limit()` and explain why it fails open

---

## `create_workflow()` â€” Four Inserts, One Commit

```python
# api/services/workflow_registry.py:537â€“648
async def create_workflow(db, request) -> WorkflowCreateResponse:
    workflow_id = request.workflow_id or uuid4()
    spec_payload = _spec_payload(request, workflow_id)

    await _ensure_author_exists(db, request.accountability.author_did)  # 1 query
    ontology_rows = await _validate_workflow_spec(db, request)           # 2-N queries

    await db.execute("INSERT INTO workflows ...", {...})         # Insert 1: workflow row
    await _insert_steps(db, workflow_id, request.steps)         # Insert 2: all steps (bulk)
    await db.execute("INSERT INTO workflow_validations ...",    # Insert 3: initial validation record
                     RETURNING id)
    await db.commit()
```

**Why `_ensure_author_exists()` first?** The `workflows` table has a FK constraint on `author_did` referencing `agent_identities.did`. A submission from an unknown or revoked DID would fail with an FK violation. Checking first gives a clean 404 error instead of a 500 from the FK constraint.

**Why bulk-insert steps?** `_insert_steps()` calls `db.execute(insert_sql, rows)` where `rows` is a list of dicts. SQLAlchemy batch-inserts all step rows in a single round-trip. An N-step workflow requires only one step INSERT call, not N individual inserts.

**The initial validation record** is inserted at creation time with `validator_did = VALIDATION_QUEUE_DID` (the sentinel `"did:agentledger:validation-queue"`). This acts as a placeholder that an admin later replaces with a real validator DID via `POST /workflows/{id}/validate`.

**All three inserts use the same transaction.** If the validation-record INSERT fails (e.g., a race condition with another submission with the same slug), the rollback removes the workflow row and step rows atomically.

---

## `update_workflow_spec()` â€” Delete and Re-insert

```python
# api/services/workflow_registry.py:651â€“733
async def update_workflow_spec(db, workflow_id, request) -> WorkflowRecord:
    workflow_row = await _get_workflow_row_by_id(db, workflow_id)

    if workflow_row["status"] == "published":
        raise HTTPException(409, "published workflow spec is immutable...")

    # Re-validate the new spec
    await _validate_workflow_spec(db, request)

    await db.execute("UPDATE workflows SET name=:name, slug=:slug, ..., spec_hash=NULL ...", ...)
    await db.execute("DELETE FROM workflow_steps WHERE workflow_id = :workflow_id", ...)
    await _insert_steps(db, workflow_id, request.steps)
    await db.commit()
```

**Why delete and re-insert steps instead of updating them?** Step rows have a `UNIQUE(workflow_id, step_number)` constraint. An update that changes `step_number` values could violate this constraint temporarily (step 2 renamed to step 1 before step 1 is renamed to step 3). Delete + re-insert avoids this ordering problem entirely.

**`spec_hash = NULL`** is explicitly set on update. A draft workflow that had `spec_hash` set (impossible today but defensible) would have the hash cleared, preventing a stale hash from carrying over to a new version.

**Published workflows cannot be updated.** The 409 check fires before any DB write. The spec_hash, once set by the validator at approval time, is permanent for that workflow UUID.

---

## `get_workflow()` â€” Two Queries + Cache

```python
# api/services/workflow_registry.py:812â€“833
async def get_workflow(db, workflow_id, redis=None) -> WorkflowRecord:
    cache_key = workflow_detail_cache_key(workflow_id)
    cached = await _cache_get_model(redis, cache_key, WorkflowRecord)
    if cached is not None:
        return cached

    workflow_row = await _get_workflow_row_by_id(db, workflow_id)     # Query 1
    step_rows = await _get_steps_for_workflow(db, workflow_row["id"]) # Query 2
    response = _to_workflow_record(workflow_row, step_rows)

    await _cache_set_model(redis, cache_key, response)               # Cache by ID
    await _cache_set_model(redis, workflow_slug_cache_key(response.slug), response)  # Cache by slug too
    return response
```

**Two queries instead of one JOIN:** The workflow row query uses `SELECT ... FROM workflows WHERE id = :workflow_id`. The step query uses `SELECT ... FROM workflow_steps WHERE workflow_id = :workflow_id ORDER BY step_number ASC`. A single JOIN with `GROUP BY` or array aggregation is possible but adds complexity. Two simple queries are easier to read and both are covered by primary key or indexed FK lookups.

**Cross-cache population:** When fetching by ID, the result is also stored under the slug cache key. When fetching by slug, the result is also stored under the ID cache key. Either lookup path warms both caches â€” the next request via the other path is a cache hit.

**Cache key format:**
```python
f"workflow:detail:{workflow_id}"   # UUID
f"workflow:slug:{slug}"            # Text slug
```

Both use a 60-second TTL. When a workflow is published or updated, `invalidate_workflow_caches()` deletes both keys.

---

## `list_workflows()` â€” Two Queries + Hashed Cache Key

Unlike `get_workflow()`, list responses depend on multiple filter parameters. A single cache key would require one cache slot per unique combination of `(domain, tags, status, quality_min, limit, offset)`. Instead, the cache key is a SHA-256 hash of the filter payload:

```python
# api/services/workflow_registry.py:46â€“65
def workflow_list_cache_key(*, domain, tags, status_filter, quality_min, limit, offset) -> str:
    payload = {
        "domain": domain.upper() if domain else None,
        "tags": sorted(tags or []),
        "status": status_filter,
        "quality_min": quality_min,
        "limit": limit,
        "offset": offset,
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"workflow:list:{digest}"
```

**Why sort tags?** `tags=['travel.air.book', 'travel.lodging.book']` and `tags=['travel.lodging.book', 'travel.air.book']` are semantically identical. Sorting before hashing ensures they map to the same cache key â€” no duplicate cache entries for the same logical query.

**Why SHA-256?** The hash creates a fixed-length key regardless of how many filters or tags are specified. It avoids key-length issues with Redis and prevents cache key collision via canonical JSON serialization.

### The SQL

`list_workflows()` uses **two separate queries**:

```sql
-- Query 1: count
SELECT COUNT(*) AS total FROM workflows
WHERE status = :status [AND ontology_domain = :domain] [AND tags @> CAST(:tags AS TEXT[])]
  [AND quality_score >= :quality_min]

-- Query 2: page
SELECT w.id, w.name, w.slug, w.description, w.ontology_domain, w.tags,
       w.status, w.quality_score, w.execution_count, w.published_at,
       w.created_at, w.updated_at,
       COUNT(ws.id)::int AS step_count
FROM workflows w
LEFT JOIN workflow_steps ws ON ws.workflow_id = w.id
WHERE [same filters]
GROUP BY w.id
ORDER BY w.quality_score DESC, w.updated_at DESC
LIMIT :limit OFFSET :offset
```

**Why two queries instead of a window function?** Layer 4 used `COUNT(*) OVER ()` to get the total in one round-trip. Layer 5 uses two queries because the list query includes a `LEFT JOIN` with `GROUP BY`. The window function approach works cleanly with simple queries; with aggregations, `COUNT(*) OVER ()` counts after the GROUP BY, not the raw row count.

**`tags @> CAST(:tags AS TEXT[])`** â€” PostgreSQL array containment operator. Returns rows where the workflow's tags array contains all elements of the filter array. Backed by the GIN index on `workflows.tags` for efficient evaluation.

**Sort order:** `quality_score DESC, updated_at DESC`. Highest-quality workflows appear first; ties broken by most-recently-updated.

---

## Cache Invalidation â€” `invalidate_workflow_caches()`

```python
# api/services/workflow_registry.py:143â€“160
async def invalidate_workflow_caches(redis, *, workflow_id=None, slug=None) -> None:
    keys = []
    if workflow_id is not None:
        keys.append(workflow_detail_cache_key(workflow_id))
    if slug:
        keys.append(workflow_slug_cache_key(slug))
    else:
        keys.extend(await _matching_cache_keys(redis, "workflow:slug:*"))
    keys.extend(await _matching_cache_keys(redis, "workflow:list:*"))
    await _delete_cache_keys(redis, keys)
```

**All list caches are invalidated on every workflow status change.** Because the list query results depend on status, quality_score, and domain â€” any of which can change when a workflow is published or a quality score is updated â€” it is not possible to invalidate only the affected list entries without scanning all keys. The entire `workflow:list:*` key space is cleared.

This is aggressive but correct. With a 60-second TTL, list caches naturally expire quickly anyway. The invalidation ensures that a workflow publication is immediately visible to list queries without waiting for TTL expiry.

---

## Rate Limit â€” `enforce_workflow_query_rate_limit()`

```python
# api/services/workflow_registry.py:163â€“197
async def enforce_workflow_query_rate_limit(redis, api_key: str) -> None:
    cache_key = f"workflow:query:rate:{sha256(api_key.encode()).hexdigest()}"
    try:
        count = await redis.incr(cache_key)
        if int(count) == 1:
            await redis.expire(cache_key, 60)
        if int(count) <= 200:
            return
        raise HTTPException(429, {"limit": 200, ...})
    except AttributeError:
        return
    except HTTPException:
        raise
    except Exception:
        return   # fail open
```

**Rate limit target: API key, not IP.** The workflow query limit is per-API-key. Multi-tenant gateways behind the same IP each get their own limit.

**Limit: 200 queries / 60 seconds.** An agent platform discovering workflows makes burst queries when building its workflow catalog, then makes infrequent reads afterwards. 200/minute is generous for catalog sync but still blocks automated scraping loops.

**Fail open:** If Redis is unavailable, `except Exception: return` allows the request to proceed. The rate limit is a performance protection, not a security gate. Workflow discovery is a read-only operation â€” the worst case of bypassing the rate limit is excess DB load, not a security breach.

**API key is hashed before being used as the cache key.** The API key itself is a secret. Storing it in a Redis key would expose it if someone can enumerate keys. The SHA-256 hash is a one-way opaque identifier.

---

## Exercise 1 â€” Full Create and Retrieve Cycle

```bash
# Create
RESPONSE=$(curl -s -X POST http://localhost:8000/v1/workflows \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "spec_version": "1.0",
    "name": "Finance Report Pull",
    "slug": "finance-report-pull",
    "description": "Pull a financial report from a registered provider",
    "ontology_domain": "FINANCE",
    "tags": ["finance.report.pull"],
    "steps": [
      {
        "step_number": 1,
        "name": "Pull report",
        "ontology_tag": "finance.report.pull",
        "is_required": true,
        "context_fields_required": ["user.name"],
        "min_trust_tier": 2,
        "min_trust_score": 50.0,
        "timeout_seconds": 30
      }
    ],
    "accountability": {"author_did": "did:key:z6MkTestContextAgent"}
  }')
echo "$RESPONSE" | python -m json.tool

WORKFLOW_ID=$(echo "$RESPONSE" | python -c "import sys,json; print(json.load(sys.stdin)['workflow_id'])")

# Retrieve
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

---

## Exercise 2 â€” Confirm Redis Caching

Make two GET requests for the same workflow and compare response headers or DB query logs:

```bash
# Call twice
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID" -H "X-API-Key: dev-local-only" > /dev/null
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID" -H "X-API-Key: dev-local-only" > /dev/null

# Check Redis key
docker exec agentledger-redis-1 redis-cli KEYS "workflow:detail:*"
docker exec agentledger-redis-1 redis-cli TTL "workflow:detail:$WORKFLOW_ID"
```

**Expected:** The key exists with TTL â‰¤ 60 seconds.

---

## Exercise 3 â€” List Filters

```bash
# List all published workflows (should be empty if no published workflows exist)
curl -s "http://localhost:8000/v1/workflows?status=published" \
  -H "X-API-Key: dev-local-only" | python -m json.tool

# List draft workflows
curl -s "http://localhost:8000/v1/workflows?status=draft" \
  -H "X-API-Key: dev-local-only" | python -m json.tool

# Filter by domain
curl -s "http://localhost:8000/v1/workflows?status=draft&domain=FINANCE" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

**Expected:** Each filter returns the correct subset. The `step_count` field in each summary should reflect the number of steps without exposing step detail.

---

## Best Practices

**Invalidate caches on write, not on read.** The cache is warmed on the first read and invalidated on every write. This "cache-aside" pattern means stale data is possible only between a write and the next read (bounded by the 60s TTL if invalidation fails). It is simpler and more predictable than write-through caching.

**Recommended (not implemented here):** A `RETURNING` clause on the workflow INSERT to avoid the second query in `get_workflow()` immediately after creation. The create response currently returns only the minimal `WorkflowCreateResponse`; adding `RETURNING *` would allow returning the full `WorkflowRecord` in one round-trip.

---

## Interview Q&A

**Q: Why does `list_workflows()` use two queries instead of `COUNT(*) OVER ()`?**
A: The list query uses `LEFT JOIN workflow_steps` with `GROUP BY` to compute `step_count`. When a window function is applied to a query with `GROUP BY`, it counts groups, not raw rows. Two separate queries â€” one for count, one for the page â€” gives precise total counts without the ambiguity of window functions over aggregated results.

**Q: Why is the rate limit key a hash of the API key rather than the API key itself?**
A: API keys are secrets. If an operator can enumerate Redis keys (via `KEYS *` or `SCAN`), using the raw API key as the key would expose it. The SHA-256 hash is a one-way opaque identifier â€” it cannot be reversed to recover the API key, but it is stable across requests from the same key.

**Q: What happens if two requests arrive simultaneously for the same workflow before it is cached?**
A: Both requests miss the cache, both run the two DB queries, both compute the same `WorkflowRecord`, and both write to the cache. The second write overwrites the first with an identical value. This is a cache stampede for a single key â€” acceptable at the workflow level since the response is deterministic and the race lasts only milliseconds.

---

## Key Takeaways

- `create_workflow()` runs three inserts in one transaction: workflow, steps (bulk), validation record
- `update_workflow_spec()` deletes and re-inserts step rows to avoid UNIQUE constraint ordering issues
- Three Redis cache types: detail by ID, detail by slug, list by filter hash (all 60s TTL)
- Cache invalidation clears all `workflow:list:*` keys on any status or quality change
- `list_workflows()` uses two queries (count + page) because the aggregating JOIN makes `COUNT(*) OVER()` ambiguous
- Rate limit: 200/60s per API key, fail-open if Redis unavailable

---

## Next Lesson

**Lesson 44 â€” The Inspection Panel: Human Validation Queue** covers `workflow_validator.py` â€” the five-item validation checklist, the draft â†’ in_review â†’ published state machine, `compute_spec_hash()` and spec immutability enforcement, and `compute_initial_quality_score()` at the moment of first publication.
