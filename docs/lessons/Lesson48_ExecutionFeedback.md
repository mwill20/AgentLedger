# Lesson 48: The Feedback Machine â€” Execution Outcome Reporting

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_executor.py`
**Prerequisites:** Lessons 45, 47
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A restaurant rating app is only as good as the reviews it collects. Layer 5's outcome reporting endpoint is where agent platforms close the feedback loop: after executing a workflow, they call `POST /workflows/{id}/executions` to report what happened. The registry increments counters, recomputes the quality score, and schedules an audit trail verification.

This lesson traces `report_execution_outcome()` and `verify_execution()` â€” the two functions that move execution data from agent platforms into the quality signal.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Trace `report_execution_outcome()` through its six sequential steps
- Explain `_increment_workflow_counters()` and why it uses a single atomic UPDATE
- Explain `_recompute_and_store_quality()` and what caches it invalidates
- Describe the BackgroundTasks pattern and when sync vs. async verification is used
- Trace `verify_execution()` through its Layer 4 audit trail cross-check
- Explain the 35-minute + 5-minute time window used in the disclosure query

---

## `report_execution_outcome()` â€” Six Sequential Steps

```python
# api/services/workflow_executor.py:271â€“347
async def report_execution_outcome(...) -> ExecutionReportResponse:
```

The function is deliberately sequential â€” each step gates the next:

### Step 1 â€” Validate the workflow is published

```python
await _load_published_workflow(db, workflow_id)
```

Returns 404 if the workflow doesn't exist *or* is not published. A `draft` or `rejected` workflow cannot receive execution reports â€” the workflow must have passed human validation before outcome data is accepted.

### Step 2 â€” Validate the agent exists

```python
await _ensure_agent_exists(db, agent_did)
```

Checks `agent_identities WHERE did = :agent_did AND is_active = true AND is_revoked = false`. A revoked agent cannot report executions. This prevents a revoked agent's execution reports from inflating a workflow's quality score after revocation.

### Step 3 â€” Validate bundle ownership (if provided)

```python
await _ensure_context_bundle_belongs(db, workflow_id=workflow_id,
                                      agent_did=agent_did,
                                      context_bundle_id=context_bundle_id)
```

If a `context_bundle_id` is provided, verifies it belongs to this `(workflow_id, agent_did)` pair. A bundle from a different workflow or a different agent cannot be used here â€” this prevents bundle reuse across workflow executions.

### Step 4 â€” Insert execution record

```python
execution_id = await _insert_execution(db, ...)
```

Inserts with `verified=false, verified_at=NULL`. The record is initially unverified â€” verification happens after the commit, in a background task.

### Step 5 â€” Increment counters atomically

```python
await _increment_workflow_counters(db, workflow_id=workflow_id, outcome=outcome)
```

```sql
UPDATE workflows
SET execution_count = execution_count + 1,
    success_count = success_count + CASE WHEN :outcome = 'success' THEN 1 ELSE 0 END,
    failure_count = failure_count + CASE WHEN :outcome = 'failure' THEN 1 ELSE 0 END,
    updated_at = NOW()
WHERE id = :workflow_id
```

**Why a single CASE-expression UPDATE?** Three separate updates (`execution_count`, `success_count`, `failure_count`) with separate round-trips could leave the counters inconsistent if a concurrent request modifies the same row between the first and third update. A single atomic UPDATE with CASE expressions ensures all three counters change together.

**`partial` outcomes:** The `outcome` field accepts `'success'`, `'partial'`, and `'failure'`. A `partial` outcome increments only `execution_count` â€” neither `success_count` nor `failure_count`. A partial outcome represents a degraded workflow completion (some required steps failed, but the workflow completed with reduced results) and contributes 0 to the success rate.

### Step 6 â€” Commit + recompute quality

```python
await db.commit()
quality_score = await _recompute_and_store_quality(db=db, workflow_id=workflow_id, redis=redis)
```

After the commit, `_recompute_and_store_quality()` runs `compute_workflow_quality_score()` (reads the updated counters), stores the new score in `workflows.quality_score`, invalidates the rank cache (`workflow:rank:{id}:*`), and invalidates the workflow detail cache.

---

## `_recompute_and_store_quality()` â€” Two Cache Invalidations

```python
# api/services/workflow_executor.py:237â€“268
async def _recompute_and_store_quality(*, db, workflow_id, redis) -> float:
    quality_score = await compute_workflow_quality_score(workflow_id, db, redis)
    await db.execute(
        text("UPDATE workflows SET quality_score = :quality_score WHERE id = :workflow_id"),
        {"workflow_id": workflow_id, "quality_score": quality_score}
    )
    await db.commit()
    await _invalidate_rank_cache(redis, workflow_id)                 # workflow:rank:{id}:*
    await workflow_registry.invalidate_workflow_caches(redis, workflow_id=workflow_id)  # detail + list
    return quality_score
```

**Two separate cache invalidations:**

1. `_invalidate_rank_cache()` â€” clears `workflow:rank:{workflow_id}:*` (all geo/pricing/agent variants). The rank endpoint includes the workflow's quality context implicitly through the step trust scores â€” strictly speaking, rank results don't include quality_score. But invalidating rank on quality change ensures the next rank request is fresh.

2. `workflow_registry.invalidate_workflow_caches()` â€” clears the detail cache (`workflow:detail:{id}`), slug cache, and all list caches (`workflow:list:*`). List results are sorted by `quality_score DESC` â€” a quality score change must bust all list caches.

---

## Background vs. Sync Verification

```python
if _sync_verification_enabled(verify_sync):
    verification = await verify_execution(execution_id, db=db, redis=redis)
    quality_score = verification.quality_score
elif background_tasks is not None:
    background_tasks.add_task(verify_execution, execution_id, db, redis)
```

**Normal path (async):** `background_tasks.add_task(verify_execution, ...)` schedules verification to run after the HTTP response is returned to the caller. The response always includes `verified=False` â€” verification has not run yet.

**Sync path (tests):** When `WORKFLOW_VERIFY_SYNC=true` (or `verify_sync=True` in tests), verification runs inline before the response. The response may include `verified=True` if audit evidence exists. This mode exists exclusively for test determinism â€” production always uses the async path.

**Why not always sync?** `verify_execution()` makes two DB queries (load execution + query `context_disclosures`). Running these inline would add ~10â€“50ms to every execution report response. At high volume, the async path is essential.

---

## `verify_execution()` â€” The Layer 4 Audit Trail Cross-Check

```python
# api/services/workflow_executor.py:447â€“490
async def verify_execution(execution_id, db, redis=None) -> VerificationResult:
    execution = await _load_execution(db, execution_id)
    required_tags = await _load_required_step_tags(db, execution["workflow_id"])
    verified = False

    if execution["context_bundle_id"] is not None:
        disclosed_tags = await _load_disclosure_tags_for_execution(db, execution)
        verified = all(tag in disclosed_tags for tag in required_tags)

    UPDATE workflow_executions SET verified = :verified, verified_at = NOW()
    ...
    quality_score = await _recompute_and_store_quality(db, workflow_id, redis)
    return VerificationResult(execution_id=execution_id, verified=verified, quality_score=quality_score)
```

### The audit trail query

```sql
SELECT DISTINCT ontology_tag
FROM context_disclosures
WHERE agent_did = :agent_did
  AND created_at BETWEEN
        (:reported_at - INTERVAL '35 minutes')
    AND (:reported_at + INTERVAL '5 minutes')
```

**Why Â±35 minutes before, +5 minutes after?** A workflow with a 30-minute context bundle TTL means disclosures could start up to 30 minutes before the execution report (bundle was created, workflow executed over time, then reported). The 35-minute window adds 5 minutes of buffer for slow workflows. The +5-minute window after the report time handles the case where the final disclosure happens fractionally after the report timestamp.

**The verification logic:**

```python
verified = all(tag in disclosed_tags for tag in required_tags)
```

Every required step's `ontology_tag` must appear in the set of tags disclosed in the time window. If any required step's tag is missing from the `context_disclosures` audit trail, the execution is `verified=False`.

**What it proves:** If `verified=True`, there is Layer 4 audit evidence that the agent disclosed context to services capable of each required step's ontology tag, at approximately the right time. It does not prove that the *correct* service was used â€” only that *some* service with that capability received context from this agent.

**What if `context_bundle_id` is None?** The execution is always `verified=False`. Without a bundle, there is no way to trace which context disclosures are associated with this execution. `verified=False` executions still count toward the counters but cannot push the quality score above 70.0.

---

## Exercise 1 â€” Report a Successful Execution

```bash
WORKFLOW_ID="<published-workflow-uuid>"
BUNDLE_ID="<approved-bundle-uuid>"

curl -s -X POST "http://localhost:8000/v1/workflows/$WORKFLOW_ID/executions" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"agent_did\": \"did:key:z6MkTestContextAgent\",
    \"context_bundle_id\": \"$BUNDLE_ID\",
    \"outcome\": \"success\",
    \"steps_completed\": 2,
    \"steps_total\": 2,
    \"duration_ms\": 3200
  }" | python -m json.tool
```

**Expected:** 201 with `execution_id`, `verified=false`, and updated `quality_score`.

---

## Exercise 2 â€” Verify Counter Increment

After reporting an execution, check the workflow counters:

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, quality_score, execution_count, success_count, failure_count
      FROM workflows WHERE id = '$WORKFLOW_ID';"
```

**Expected:** `execution_count` incremented by 1; `success_count` incremented by 1 (for outcome='success').

---

## Exercise 3 â€” Observe the Unverifiable Cap in Action

Report 10 executions with outcome='success' but no `context_bundle_id` (unverified). After each report, observe the quality_score change:

```bash
for i in {1..10}; do
  curl -s -X POST "http://localhost:8000/v1/workflows/$WORKFLOW_ID/executions" \
    -H "X-API-Key: dev-local-only" \
    -H "Content-Type: application/json" \
    -d "{
      \"agent_did\": \"did:key:z6MkTestContextAgent\",
      \"outcome\": \"success\",
      \"steps_completed\": 2,
      \"steps_total\": 2
    }" | python -c "import sys,json; d=json.load(sys.stdin); print(f'quality_score={d[\"quality_score\"]}')"
done
```

**Expected:** Quality score grows but never exceeds 70.0 (verification_rate = 0.0 throughout, cap is active).

---

## Best Practices

**Execution reports are advisory, not authoritative.** Agent platforms report what they claim happened. The verification cross-check against Layer 4 `context_disclosures` is the only objective evidence. Design workflows and context bundles to enable verification â€” a workflow that never produces verified executions will never reach quality_score > 70.0.

**Recommended (not implemented here):** A `POST /workflows/{id}/executions/{execution_id}/verify` endpoint that allows an operator to manually trigger re-verification for a specific execution. Useful when verification failed due to a temporary Layer 4 audit trail lag.

---

## Interview Q&A

**Q: Why is the execution always returned as `verified=false` even when background verification succeeds shortly after?**
A: The HTTP response is sent before the background task runs. FastAPI's `BackgroundTasks` execute after the response is dispatched â€” the caller receives the response first, then verification runs. The `verified=False` in the response is the state at the moment of the commit, not the final state. The caller can query the workflow's quality_score later to see the post-verification update.

**Q: What does `outcome='partial'` mean for quality score calculation?**
A: A partial execution increments only `execution_count`, not `success_count` or `failure_count`. So `success_rate = success_count / execution_count` decreases as more partial outcomes are reported. Partial outcomes represent real workflow runs (increasing volume_factor) but do not contribute to success_rate. They represent a degraded but completed workflow â€” worse than success, better than failure.

**Q: What happens if the same `context_bundle_id` is used in two execution reports?**
A: The bundle ownership check (`_ensure_context_bundle_belongs()`) only verifies that the bundle belongs to the right `(workflow_id, agent_did)` â€” it does not check whether the bundle has already been used. Two execution reports with the same bundle_id would both pass validation. However, the second execution's verification might produce the same `verified=True` result (the audit trail evidence is still there). The bundle's own status transitions (pendingâ†’approvedâ†’consumed) are managed separately and do not block the second report.

---

## Key Takeaways

- Six sequential steps: validate workflow â†’ validate agent â†’ validate bundle â†’ insert â†’ increment counters â†’ commit + recompute
- Counters use a single atomic UPDATE with CASE expressions â€” avoids race conditions
- `_recompute_and_store_quality()` invalidates both rank cache and workflow detail/list caches
- Verification is async by default (BackgroundTasks); sync mode for tests via `WORKFLOW_VERIFY_SYNC=true`
- Verification cross-checks `context_disclosures` in a 35min-before + 5min-after window around the report time
- `verified=False` executions cannot push quality_score above 70.0 â€” the unverifiable cap

---

## Next Lesson

**Lesson 49 â€” The Four Threats: Anti-Gaming & Hardening** covers the four Layer 5 threat mitigations â€” workflow spec laundering, step poisoning, quality gaming, and context bundle abuse â€” plus the Redis caching strategy and the rate limiting design for the workflow query surface.
