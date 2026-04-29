# Lesson 52: The Snapshot in Time — Liability Snapshot Creation & Read Path

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/services/liability_snapshot.py`
**Prerequisites:** Lesson 51, Lesson 48 (execution reporting), Lesson 34 (Layer 4 matching)
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

A surveillance camera at a bank records every transaction in real time. When an incident occurs later, investigators review the footage from the exact moment — not the current state of the branch. Layer 6's liability snapshot is that recording: a forensic capture of the trust state for every actor involved in a workflow execution, committed to the database before the transaction even returns a response.

This lesson traces `create_snapshot()` through its seven sequential queries, explains the `step_trust_states` JSONB structure, and shows how the snapshot becomes the evidentiary anchor for attribution.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Trace `create_snapshot()` through its seven SQL queries
- Explain the lateral join in `_load_step_trust_states()` and why it resolves unpinned services
- Explain why the 35-minute + 5-minute window appears in both snapshot and verification logic
- Describe the `step_trust_states` JSONB structure and the `trust_score_source` field
- Explain why `create_snapshot()` does not call `db.commit()`
- Trace the read path: `GET /liability/snapshots/{execution_id}`

---

## Where `create_snapshot()` Is Called

```python
# api/services/workflow_executor.py (Layer 5)
async def report_execution_outcome(...):
    # ... six sequential steps ...
    await db.commit()                           # execution record committed
    await liability_snapshot.create_snapshot(   # snapshot created synchronously
        db=db, execution_id=execution_id
    )
    await db.commit()                           # snapshot committed
    return ExecutionReportResponse(...)
```

The snapshot is created synchronously after the execution record is committed but before the 201 response returns. It runs in two commits: the first commits the execution record; the second commits the snapshot. If snapshot creation fails, the exception propagates and the caller receives a 500 — but the execution record itself is already committed (this is a deliberate trade-off: a failed snapshot is a monitoring alert, not a lost execution).

---

## `create_snapshot()` — Seven Sequential Queries

```python
# api/services/liability_snapshot.py:351–456
async def create_snapshot(db, execution_id) -> LiabilitySnapshotRecord:
```

### Query 1 — Idempotency Check

```python
existing = await _load_existing_snapshot(db, execution_id)
if existing is not None:
    return _to_snapshot_record(existing)
```

`SELECT ... FROM liability_snapshots WHERE execution_id = :execution_id`

If a snapshot already exists for this `execution_id` (due to retry behavior), return the existing record immediately. The `UNIQUE (execution_id)` constraint on `liability_snapshots` enforces this at the database level too — a second `INSERT` would fail with a unique violation. The idempotency check prevents hitting that constraint under normal retry conditions.

### Query 2 — Load the Execution

```python
execution = await _load_execution(db, execution_id)
```

```sql
SELECT id, workflow_id, agent_did, context_bundle_id, reported_at
FROM workflow_executions
WHERE id = :execution_id
```

Returns 404 if the execution doesn't exist. The `reported_at` timestamp is the anchor for all time-window queries in subsequent steps.

### Query 3 — Load Workflow State

```python
workflow = await _load_workflow_state(db, execution["workflow_id"])
```

```sql
SELECT id, quality_score, author_did
FROM workflows
WHERE id = :workflow_id
```

Captures `workflows.quality_score` at snapshot time — not the current value after future executions. This is why snapshot quality scores can differ from the current `quality_score` for a long-running workflow.

### Query 4 — Load Latest Validation

```python
validation = await _load_latest_validation(db, execution["workflow_id"])
```

```sql
SELECT validator_did, checklist
FROM workflow_validations
WHERE workflow_id = :workflow_id
  AND decision = 'approved'
ORDER BY decision_at DESC NULLS LAST, assigned_at DESC
LIMIT 1
```

Gets the most recent approval — including the validator DID (a named actor in attribution) and the checklist (evidence for whether `context_minimal` and `trust_thresholds_appropriate` were properly verified). Returns `None` if the workflow was never formally validated, in which case `validator_did` is stored as `NULL` in the snapshot.

### Query 5 — Load Step Trust States (The Critical Query)

```python
step_trust_states = await _load_step_trust_states(db, execution)
```

```sql
SELECT
    ws.step_number,
    ws.ontology_tag,
    ws.min_trust_tier,
    ws.min_trust_score,
    COALESCE(ws.service_id, cd.service_id) AS service_id,
    s.name AS service_name,
    s.trust_score,
    s.trust_tier
FROM workflow_steps ws
LEFT JOIN LATERAL (
    SELECT context_disclosures.service_id
    FROM context_disclosures
    WHERE context_disclosures.agent_did = :agent_did
      AND context_disclosures.ontology_tag = ws.ontology_tag
      AND context_disclosures.created_at BETWEEN
            (CAST(:reported_at AS TIMESTAMPTZ) - INTERVAL '35 minutes')
        AND (CAST(:reported_at AS TIMESTAMPTZ) + INTERVAL '5 minutes')
    ORDER BY context_disclosures.created_at DESC
    LIMIT 1
) cd ON ws.service_id IS NULL
LEFT JOIN services s ON s.id = COALESCE(ws.service_id, cd.service_id)
WHERE ws.workflow_id = :workflow_id
ORDER BY ws.step_number ASC
```

**The lateral join:** For workflow steps where `service_id IS NULL` (the step doesn't pin a specific service), this query uses a `LATERAL` subquery to look up which service actually received context for that step's ontology tag within the 35+5 minute window. This resolves the service dynamically from the Layer 4 `context_disclosures` audit trail — the same window used by Layer 5 verification.

**`COALESCE(ws.service_id, cd.service_id)`:** Uses the pinned service if one exists; falls back to the context-disclosure-resolved service for unpinned steps. If neither exists (no disclosure found), `service_id` is `NULL` and `trust_score_source` is stored as `"unresolved_service"`.

**`trust_score_source` field:** The resulting JSONB record tags each step's trust data with its provenance:
- `"services.trust_score_at_snapshot"` — a service was identified and its current `trust_score` was captured
- `"unresolved_service"` — no service was identified; trust data is `NULL`

This field is critical for attribution: an `"unresolved_service"` step is itself an evidence signal that the execution may not have been fully verified.

### Query 6 — Load Context Summary

```python
context_summary, critical_mismatch_count = await _load_context_summary(db, execution, service_ids)
```

Two sub-queries:

**Disclosure fields:**
```sql
SELECT fields_disclosed, fields_withheld, fields_committed
FROM context_disclosures
WHERE agent_did = :agent_did
  AND created_at BETWEEN (:reported_at - 35min) AND (:reported_at + 5min)
```

Aggregates all fields disclosed, withheld, and committed during the workflow execution window into a deduplicated `context_summary` JSONB:
```json
{
  "fields_disclosed": ["user.name", "user.email"],
  "fields_withheld": ["user.ssn"],
  "fields_committed": ["user.frequent_flyer_id"],
  "mismatch_count": 2
}
```

**Mismatch count:**
```sql
SELECT
    COUNT(*) AS mismatch_count,
    COUNT(*) FILTER (WHERE severity = 'critical') AS critical_count
FROM context_mismatch_events
WHERE agent_did = :agent_did
  AND service_id = ANY(:service_ids)
  AND created_at BETWEEN (:reported_at - 35min) AND (:reported_at + 5min)
```

`critical_mismatch_count` is stored as a top-level integer column (not inside the JSONB), enabling fast filtering: `WHERE critical_mismatch_count > 0` without JSONB extraction.

### Query 7 — Load Agent Profile Default Policy

```python
agent_policy = await _load_agent_profile_default_policy(db, execution["agent_did"])
```

```sql
SELECT default_policy
FROM context_profiles
WHERE agent_did = :agent_did
  AND is_active = true
ORDER BY updated_at DESC
LIMIT 1
```

Captures whether the agent was running with `default_policy="permit"` or `default_policy="deny"` at execution time. An agent with `default_policy="permit"` in a context that should have been restrictive is an attribution signal.

### Insert

```sql
INSERT INTO liability_snapshots (
    execution_id, workflow_id, agent_did, captured_at,
    workflow_quality_score, workflow_author_did, workflow_validator_did,
    workflow_validation_checklist, step_trust_states, context_summary,
    critical_mismatch_count, agent_profile_default_policy, created_at
)
VALUES (...)
RETURNING id, execution_id, ...
```

The INSERT uses `CAST(:value AS JSONB)` for the three JSONB columns — necessary because SQLAlchemy's `text()` binds send strings and PostgreSQL needs the explicit cast to store them as JSONB.

---

## Why `create_snapshot()` Does Not Call `db.commit()`

The docstring states: "This function intentionally does not commit. It is called inside the Layer 5 execution-report transaction so a failed snapshot rolls back the execution."

In practice, Layer 5 calls `db.commit()` before and after `create_snapshot()`:

```python
await db.commit()                    # commits execution record
await create_snapshot(db, ...)       # inserts snapshot (un-committed)
await db.commit()                    # commits snapshot
```

If `create_snapshot()` called `db.commit()` internally, it would break this two-commit pattern and make it harder to maintain transactional boundaries. By leaving commit to the caller, the snapshot service is composable — it can be called inside any transaction boundary the caller controls.

---

## The `step_trust_states` JSONB Structure

Each element in the `step_trust_states` array:

```json
{
  "step_number": 1,
  "ontology_tag": "travel.air.search",
  "service_id": "d91a4c2e-...",
  "service_name": "Amadeus Travel API",
  "min_trust_tier": 2,
  "min_trust_score": 50.0,
  "trust_score": 87.3,
  "trust_tier": 3,
  "trust_score_source": "services.trust_score_at_snapshot"
}
```

**Attribution reads this directly.** When the attribution engine evaluates `service_trust_below_step_minimum`, it compares `trust_score` to `min_trust_score` within this JSONB. The data doesn't need to be re-queried from the live `services` table — the snapshot is the authoritative record.

---

## The Read Path: `GET /liability/snapshots/{execution_id}`

```python
# api/services/liability_snapshot.py:458–492
async def get_snapshot_for_execution(db, execution_id) -> LiabilitySnapshotRecord:
    row = await _load_existing_snapshot(db, execution_id)
    if row is None:
        raise HTTPException(404, "no snapshot for this execution")
    return _to_snapshot_record(row)
```

One query, no cache. Snapshots are rarely read (only during dispute resolution), so a Redis cache would provide marginal benefit at the cost of cache invalidation complexity for append-only data. The `GET /liability/snapshots` list endpoint is admin-only and used for monitoring — its p95 target (< 200ms @ 100 concurrent) was verified by the Layer 6 acceptance load test.

---

## Exercise 1 — Inspect a Snapshot

After reporting a workflow execution (Lesson 48), retrieve the auto-created snapshot:

```bash
EXECUTION_ID="<execution-uuid>"
curl -s "http://localhost:8000/v1/liability/snapshots/$EXECUTION_ID" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

**Expected:** A JSON object with `step_trust_states` (array), `context_summary`, `workflow_quality_score`, `workflow_author_did`, and `captured_at`.

---

## Exercise 2 — Verify Step Trust State Capture

After reporting an execution, query the snapshot and compare `step_trust_states[0].trust_score` with the current value in the `services` table:

```bash
EXECUTION_ID="<execution-uuid>"

# Get snapshot trust score for step 1
curl -s "http://localhost:8000/v1/liability/snapshots/$EXECUTION_ID" \
  -H "X-API-Key: dev-local-only" | \
  python -c "import sys,json; d=json.load(sys.stdin); print(d['step_trust_states'][0])"

# Compare with current trust score
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, name, trust_score FROM services WHERE name = '<service-name>';"
```

**Expected:** The values match (no crawl has run since the snapshot). If a crawl has run, they may differ — illustrating exactly why snapshots exist.

---

## Exercise 3 — Inspect the JSONB Directly

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "
SELECT
  id,
  captured_at,
  workflow_quality_score,
  jsonb_array_length(step_trust_states) AS steps,
  critical_mismatch_count
FROM liability_snapshots
ORDER BY captured_at DESC
LIMIT 5;
"
```

**Expected:** One row per reported execution, with `steps` matching the workflow's step count.

---

## Best Practices

**The 35+5 minute window is intentional.** The same window appears in both Layer 5 verification and Layer 6 snapshot creation — the bundle TTL is 30 minutes, so disclosures could have started up to 30 minutes before the report, plus 5 minutes of buffer. Using a different window in Layer 6 would mean the snapshot's `context_summary` and the execution's `verified` flag could be based on different evidence sets.

**`_dedupe_sorted()` is idempotent.** When fields appear in multiple disclosure records across steps (e.g., `user.name` disclosed in both Step 1 and Step 2), the function deduplicates and sorts. Deterministic field lists in JSONB make the snapshot comparable across execution reports.

---

## Interview Q&A

**Q: Why does `_load_step_trust_states()` use a `LATERAL` join rather than a simple `LEFT JOIN`?**
A: The lateral join resolves the service per step conditionally — only when `ws.service_id IS NULL` (unpinned steps). A simple `LEFT JOIN` against `context_disclosures` would need to join across all rows, with no way to restrict the join to only unpinned steps. The `LATERAL` subquery allows per-row conditional logic: "for this step, if there's no pinned service, find the most recent disclosure in the time window for this step's ontology tag."

**Q: What happens to the snapshot if the Layer 5 execution reporter retries the request?**
A: The idempotency check at the top of `create_snapshot()` returns the existing snapshot immediately if one already exists for the given `execution_id`. The `UNIQUE (execution_id)` constraint in the database provides a second safety net. A retry produces no duplicate.

**Q: Why is `critical_mismatch_count` stored as an integer column rather than inside the JSONB `context_summary`?**
A: Top-level integer columns can be indexed and filtered directly with `WHERE critical_mismatch_count > 0`. Querying `context_summary->>'critical_mismatch_count'` requires a JSONB cast and cannot use a standard B-tree index efficiently. Storing the critical count as a first-class column enables the admin list endpoint to filter for "snapshots with at least one critical mismatch" without a full JSONB scan.

---

## Key Takeaways

- `create_snapshot()` runs seven queries: idempotency check, execution, workflow, validation, step trust states, context summary, agent profile
- The lateral join in `_load_step_trust_states()` resolves actual service used for unpinned steps via `context_disclosures`
- The 35+5 minute window matches Layer 5 verification — same evidence set, consistent records
- `create_snapshot()` does not call `db.commit()` — the caller controls transaction boundaries
- `step_trust_states` JSONB captures `trust_score`, `trust_tier`, `min_trust_score`, `min_trust_tier`, and `trust_score_source` per step
- Snapshots are append-only and never modified after creation — the evidence is frozen at the moment of execution

---

## Next Lesson

**Lesson 53 — The Claim Window: Filing a Liability Claim** traces `file_claim()` through its validation checks, explains the five claim types and their legal significance, shows the rate limiting and deduplication design, and introduces the six-state claim lifecycle.
