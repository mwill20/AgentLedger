# Lesson 56: The Final Verdict — Claim Determination, Resolution & Appeals

> **Beginner frame:** The claims lifecycle is a case docket. It moves a claim from filed to evidence gathered, review, determination, resolution, or appeal while preserving a traceable state history.

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/services/liability_claims.py`, `api/services/liability_attribution.py`
**Prerequisites:** Lessons 53–54
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A civil lawsuit doesn't end with evidence collection. After all depositions are taken and documents are submitted, a judge (or jury) weighs the evidence and issues a determination. If the losing party believes the process was flawed, they can appeal. Layer 6's claim lifecycle models this: after a claim moves through `filed` → `evidence_gathered` → `under_review`, the determination endpoint computes attribution weights from the gathered evidence, the resolution endpoint closes the claim, and the appeal endpoint reopens it for re-determination.

This lesson traces the determination, resolution, and appeal endpoints, explains the `determination_version` versioning pattern, and shows how the Redis claim status cache is maintained through every transition.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Trace `determine_attribution()` through its status check, attribution computation, INSERT, and cache update
- Explain `determination_version` and why appeals create new records rather than updating existing ones
- Explain `resolve_claim()` and what the resolution note field is for
- Trace `appeal_claim()` — what it checks, what status it sets, and why it does not re-determine immediately
- Explain the Redis claim status cache invalidation pattern across all status transitions

---

## The Full Claim State Machine

```
filed
  ↓ POST /claims/{id}/gather
evidence_gathered
  ↓ (admin: assign reviewer_did)
under_review
  ↓ POST /claims/{id}/determine
determined
  ↓ POST /claims/{id}/resolve
resolved
  ↓ POST /claims/{id}/appeal
appealed → under_review
  ↓ POST /claims/{id}/determine
determined (determination_version = 2)
  ↓ POST /claims/{id}/resolve
resolved (final)
```

Each arrow corresponds to exactly one API call. Status transitions are enforced: calling `determine` on a `filed` claim returns 409, calling `resolve` on an `evidence_gathered` claim returns 409, etc.

---

## `determine_attribution()` — Four Steps

```python
# api/services/liability_attribution.py:462–(end)
async def determine_attribution(claim_id, determined_by, db, redis) -> DeterminationResponse:
```

**Step 1 — Status check:**
```python
claim = await _load_claim(db, claim_id)
if claim["status"] != "under_review":
    raise HTTPException(409, "claim must be in 'under_review' status to determine attribution")
```

Only `under_review` claims can be determined. A `filed` or `evidence_gathered` claim hasn't been assigned a human reviewer yet.

**Step 2 — Load all context:**
```python
snapshot = await _load_snapshot(db, claim["snapshot_id"])
evidence = await _load_evidence(db, claim_id)
workflow = await _load_workflow(db, execution["workflow_id"])
workflow_steps = await _load_workflow_steps(db, execution["workflow_id"])
execution = await _load_execution(db, claim["execution_id"])
```

Five queries load everything the attribution engine needs. These are point-in-time reads — the snapshot and evidence records are frozen; the workflow and workflow steps are read from live tables (step definitions don't change after publication).

**Step 3 — Compute attribution:**
```python
result = compute_attribution(
    claim=claim,
    snapshot=snapshot,
    evidence=evidence,
    workflow=workflow,
    workflow_steps=workflow_steps,
    execution=execution,
)
```

`compute_attribution()` is a pure function — no I/O, no DB calls. It evaluates all 11 factors against the loaded data and returns an `AttributionResult` with normalized weights, applied factors list, and confidence.

**Step 4 — Insert determination and update claim:**
```python
# Get next version number
current_max = SELECT MAX(determination_version) FROM liability_determinations WHERE claim_id = :id
next_version = (current_max or 0) + 1

# Insert determination
INSERT INTO liability_determinations (
    claim_id, determination_version,
    agent_weight, service_weight, workflow_author_weight, validator_weight,
    agent_did, service_id, workflow_author_did, validator_did,
    attribution_factors, confidence, determined_by, determined_at
) VALUES (...)

# Update claim status
UPDATE liability_claims
SET status = 'determined', determined_at = NOW(), updated_at = NOW()
WHERE id = :claim_id
```

Then: `await refresh_claim_status_cache(redis, claim_id, "determined")`

---

## `determination_version` — Why Appeals Create New Records

On first determination: `determination_version = 1`.
After an appeal + re-determination: `determination_version = 2`.

New determination records are inserted — existing records are never updated. This append-only pattern means:

- The original determination is preserved for audit purposes
- The appeal record shows how the determination changed and what factors applied differently
- If a regulatory body later requests the "history" of a determination, all versions are available with their attribution factors and confidence levels

The `GET /liability/claims/{claim_id}` response returns the latest determination (max version). The full version history is available by querying `liability_determinations WHERE claim_id = :id ORDER BY determination_version`.

---

## `resolve_claim()` — Closing the Claim

```python
# api/services/liability_claims.py
async def resolve_claim(claim_id, resolution_note, resolved_by, db, redis):
    claim = await _load_claim_row(db, claim_id)
    if claim["status"] != "determined":
        raise HTTPException(409, "claim must be 'determined' before resolving")

    await db.execute(
        text("""
            UPDATE liability_claims
            SET status = 'resolved',
                resolution_note = :resolution_note,
                resolved_at = NOW(),
                reviewer_did = :resolved_by,
                updated_at = NOW()
            WHERE id = :claim_id
        """),
        ...
    )
    await refresh_claim_status_cache(redis, claim_id, "resolved")
```

**`resolution_note`** is a free-text field for the human reviewer to record what action was taken: "Service operator acknowledged the capability gap and committed to a fix by May 2026. Claim settled with no financial exchange." The note is the human layer on top of the algorithmic attribution.

A resolved claim is terminal — it cannot be further updated. Only an appeal can reopen it.

---

## `appeal_claim()` — Reopening for Re-Review

```python
async def appeal_claim(claim_id, appeal_reason, appellant_did, db, redis):
    claim = await _load_claim_row(db, claim_id)
    if claim["status"] != "resolved":
        raise HTTPException(409, "only resolved claims can be appealed")
    if claim["claimant_did"] != appellant_did:
        raise HTTPException(403, "only the original claimant can appeal")

    await db.execute(
        text("""
            UPDATE liability_claims
            SET status = 'under_review',
                resolution_note = :appeal_reason,
                resolved_at = NULL,
                updated_at = NOW()
            WHERE id = :claim_id
        """),
        ...
    )
    await refresh_claim_status_cache(redis, claim_id, "under_review")
```

**Why `under_review` not `appealed`?** The spec defines `appealed` as a transient status that immediately transitions to `under_review`. The implementation directly sets `under_review`, simplifying the state machine. The appeal reason is recorded in `resolution_note`, overwriting the prior resolution note.

**Only the original claimant can appeal.** This prevents third parties from manipulating the claims lifecycle. The check uses `claim["claimant_did"] == appellant_did` — the DID of the party that filed the claim.

**`resolved_at` is reset to `NULL`** when a claim is appealed. This ensures the `resolved_at` timestamp only reflects completed (non-appealed) resolutions, making it accurate for compliance reporting.

---

## The Redis Claim Status Cache

```python
# Cache key: liability:claim_status:{claim_id}
# TTL: 60 seconds
```

Every status transition calls `refresh_claim_status_cache()`:
```python
async def refresh_claim_status_cache(redis, claim_id, claim_status):
    await invalidate_claim_status_cache(redis, claim_id)  # DELETE the old key
    await cache_claim_status(redis, claim_id, claim_status)  # SETEX with 60s TTL
```

The cache is invalidated (DELETE) before re-populating (SETEX), preventing a stale status from being read between the DELETE and the SET. In a single-Redis-server setup, the DELETE + SETEX sequence is not atomic — but the 60-second TTL ensures stale values expire quickly even if a cache miss occurs during the transition.

**Fail-open:** All Redis operations in the cache layer are wrapped in try/except. If Redis is unavailable, the cache is bypassed and the claim status is read from the database on every request. The fail-open pattern ensures claims remain accessible during Redis outages.

---

## Exercise 1 — Complete the Claim Lifecycle

Start from a filed claim (Lesson 53) and complete all transitions:

```bash
CLAIM_ID="<claim-uuid>"

# 1. Gather evidence
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/gather" \
  -H "X-API-Key: dev-local-only" | python -c "import sys,json; print(json.load(sys.stdin).get('status'))"

# 2. Assign reviewer (uses PUT in some implementations, or simulate)
# (In test environment with WORKFLOW_VERIFY_SYNC, status may auto-advance)

# 3. Determine attribution
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/determine" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"determined_by": "did:key:z6MkTestReviewer"}' | python -m json.tool

# 4. Resolve
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/resolve" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"resolution_note": "Service acknowledged capability gap. No financial exchange.", "resolved_by": "did:key:z6MkTestReviewer"}' | python -m json.tool
```

**Expected:** Each call advances the status. Final status: `resolved`.

---

## Exercise 2 — Appeal and Re-Determine

```bash
# 5. Appeal
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/appeal" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"appeal_reason": "New revocation evidence available", "appellant_did": "did:key:z6MkTestContextAgent"}' | python -m json.tool

# 6. Determine again (version 2)
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/determine" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"determined_by": "did:key:z6MkTestReviewer"}' | python -c "
import sys, json
d = json.load(sys.stdin)
det = d.get('determination', {})
print(f'Version: {det.get(\"determination_version\")}')
print(f'Confidence: {det.get(\"confidence\")}')
"
```

**Expected:** Second determination shows `determination_version: 2`.

---

## Exercise 3 — Inspect Determination History

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "
SELECT claim_id, determination_version, agent_weight, service_weight,
       workflow_author_weight, validator_weight, confidence, determined_at
FROM liability_determinations
WHERE claim_id = '$CLAIM_ID'
ORDER BY determination_version;
"
```

**Expected:** Two rows — version 1 (original) and version 2 (post-appeal).

---

## Interview Q&A

**Q: Why is `determination_version` an incrementing integer rather than a UUID for each determination?**
A: The incrementing version makes it easy to query "latest determination" (`MAX(determination_version)`) without a timestamp comparison, and makes the history ordering unambiguous. Two determinations could theoretically have the same `determined_at` timestamp if system clocks are imprecise. The integer version provides a clear, conflict-free ordering.

**Q: Why does the appeal endpoint set status to `under_review` directly rather than to `appealed`?**
A: The spec defines `appealed` as a transient state that immediately becomes `under_review` — they are functionally identical (both allow the `determine` endpoint to be called). Skipping the transient state simplifies the state machine: there is no need to write a status check for `appealed` in the `determine` handler. The appeal is recorded via the `resolution_note` override and `resolved_at = NULL`.

**Q: How does the Redis cache handle the gap between `invalidate_claim_status_cache()` and `cache_claim_status()`?**
A: Between the DELETE and the SETEX, a concurrent request might query Redis for the claim status, find no cached value, and fall through to the database — returning the correct (just-transitioned) status. This brief cache miss is acceptable: the database is authoritative and the miss lasts only the microseconds between the two Redis calls. The 60s TTL prevents stale values from persisting regardless.

---

## Key Takeaways

- State machine: filed → evidence_gathered → under_review → determined → resolved / appealed (→ under_review)
- `determine_attribution()` runs 5 DB queries then calls `compute_attribution()` (pure function, no I/O)
- `determination_version` increments on each re-determination; old records are preserved (append-only)
- Appeals set status directly to `under_review`; only original claimant can appeal
- `resolution_note` carries the human layer of the determination — what was done, by whom
- Redis claim status cache: fail-open, 60s TTL, invalidate-then-set on every transition

---

## Next Lesson

**Lesson 57 — Data Models & API Routes: Layer 6 Pydantic Models** covers `api/models/liability.py` — the Pydantic models for all nine endpoints, the constraint validators (weights sum to 1.0, non-negative weights), and how the router wires all six services into a consistent API surface.
