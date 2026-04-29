# Lesson 53: The Claim Window — Filing a Liability Claim & Evidence Gathering

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/services/liability_claims.py`
**Prerequisites:** Lesson 52
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

A plaintiff files a lawsuit by submitting a complaint that names the parties, describes the harm, and states the legal theory. The court then issues subpoenas and gathers evidence before any judgment is made. Layer 6's claim filing flow works the same way: a claimant submits a structured claim against an execution, the system gathers evidence from all eight source layers, and only then can attribution be determined.

This lesson traces the full `file_claim()` flow, explains the five claim types and their legal significance, shows how evidence gathering deduplicates across eight sources, and maps the six-state claim lifecycle.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Name the five claim types and the actor most likely to be at fault for each
- Trace `file_claim()` through its validation checks and INSERT
- Explain the rate limit design: 10 claims per hour per claimant DID, hashed, fail-open
- Explain `_insert_evidence_if_missing()` and the `ON CONFLICT DO NOTHING` deduplication
- Name all eight evidence sources and the layer each comes from
- Trace the six-state claim lifecycle: filed → evidence_gathered → under_review → determined → resolved / appealed

---

## The Five Claim Types

Each claim type signals a different actor as likely responsible:

| Claim type | Harm description | Primary suspect |
|-----------|-----------------|----------------|
| `service_failure` | Service did not fulfill its declared capability | Service |
| `data_misuse` | Service mishandled disclosed context data | Service |
| `wrong_outcome` | Agent completed workflow but result was incorrect | Workflow author / validator |
| `unauthorized_action` | Agent acted outside its declared scope | Agent |
| `workflow_design_flaw` | The workflow spec itself caused the harm | Workflow author / validator |

Claim type is not a legal determination — it is a starting hypothesis. The attribution engine may shift weights significantly based on evidence. A `service_failure` claim may ultimately assign most weight to the agent if evidence shows the agent used an undertrusted service despite the snapshot capturing a warning signal.

---

## `file_claim()` — Four Validation Checks

```python
# api/services/liability_claims.py
async def file_claim(*, execution_id, claimant_did, claim_type, description,
                     harm_value_usd, db, redis, background_tasks,
                     verify_sync) -> ClaimResponse:
```

**Check 1 — Rate limit:**
```python
await enforce_claim_filing_rate_limit(redis, claimant_did)
```

10 claims per claimant DID per hour. Rate limit key: `liability:claim_rate:{sha256(claimant_did)}`. Hashed to avoid exposing agent DIDs in Redis keyspace. Fail-open: if Redis is unavailable, the limit is bypassed — claim filing is not safety-critical.

**Check 2 — Execution exists:**
```python
execution = await _load_execution(db, execution_id)
```

Returns 404 if the execution doesn't exist. The full execution record is loaded because its fields are needed for evidence gathering.

**Check 3 — Snapshot exists:**
```python
snapshot = await _load_snapshot_for_execution(db, execution_id)
```

Returns 422 if no snapshot exists. A claim cannot be filed without a snapshot — the snapshot is the evidentiary anchor. This check enforces the invariant: every claim has a frozen trust state record at its foundation.

**Check 4 — Deduplication:**
```python
existing = await _check_duplicate_claim(db, execution_id, claimant_did, claim_type)
if existing is not None:
    raise HTTPException(409, "duplicate claim")
```

One claimant cannot file the same claim type against the same execution twice. The dedup check is `(execution_id, claimant_did, claim_type)`. A claimant can file a `data_misuse` and a `service_failure` claim against the same execution — these are distinct theories.

**INSERT:**
```python
INSERT INTO liability_claims (
    execution_id, snapshot_id, claimant_did, claim_type,
    description, harm_value_usd, status, filed_at
)
VALUES (..., 'filed', NOW())
RETURNING id
```

---

## The Rate Limit Design

```python
# api/services/liability_claims.py:60–81
async def enforce_claim_filing_rate_limit(redis, claimant_did):
    key = claim_rate_limit_key(claimant_did)   # sha256 of DID
    current = await _redis_call(redis, "incr", key)
    if current == 1:
        await _redis_call(redis, "expire", key, 3600)   # 1-hour window
    if current > 10:
        raise HTTPException(429, "claim filing rate limit exceeded")
```

**Why 10 claims per hour?** Claim filing triggers evidence gathering (8 DB queries). At more than 10 per hour, a single malicious claimant could generate significant DB load by filing claims against thousands of executions. The limit absorbs legitimate dispute volume (a compliance team filing 10 claims per hour is unusual) while blocking automated abuse.

**Why hash the claimant DID?** Agent DIDs are long strings that could be enumerated via Redis `SCAN`. Hashing prevents the rate limit keyspace from becoming a directory of all agent DIDs that have filed claims.

---

## Evidence Gathering — Eight Sources

After filing, evidence gathering runs as a background task (or synchronously in tests via `WORKFLOW_VERIFY_SYNC=true`):

```python
if _sync_gather_enabled(verify_sync):
    evidence = await gather_evidence(claim_id, db, redis)
else:
    background_tasks.add_task(gather_evidence, claim_id, db, redis)
```

`gather_evidence()` calls eight `_gather_*` functions. Each attaches one or more evidence records to the claim:

| Source | Evidence type | Layer | What it captures |
|--------|--------------|-------|-----------------|
| 1 | `workflow_execution` | 5 | Outcome, steps completed, failure step, verified flag |
| 2 | `validation_record` | 5 | Validator DID, checklist decisions |
| 3 | `liability_snapshot` | 6 | Frozen trust state (quality score, step trust states, context summary) |
| 4 | `context_disclosure` | 4 | Fields disclosed, withheld, committed per disclosure; erased flag |
| 5 | `context_mismatch` | 4 | Over-request events, severity, field requested |
| 6 | `trust_attestation` | 3 | On-chain attestations for services in the execution |
| 7 | `trust_revocation` | 3 | On-chain revocation events, timing relative to execution |
| 8 | `manifest_version` | 1 | Service manifest at approximately execution time (capability tags) |

---

## `_insert_evidence_if_missing()` — Idempotent Evidence Records

```python
# api/services/liability_claims.py:355–423
async def _insert_evidence_if_missing(db, *, claim_id, evidence_type, source_table,
                                       source_id, source_layer, summary, raw_data):
    existing = await db.execute(
        text("SELECT id FROM liability_evidence WHERE claim_id = :claim_id
              AND source_table = :source_table AND source_id = :source_id"),
        ...
    )
    if existing.mappings().first() is not None:
        return  # already attached

    await db.execute(
        text("""
            INSERT INTO liability_evidence (...) VALUES (...)
            ON CONFLICT (claim_id, source_table, source_id) DO NOTHING
        """),
        ...
    )
```

Two layers of deduplication: a SELECT check before the INSERT, and an `ON CONFLICT DO NOTHING` for the case where two concurrent evidence-gathering tasks try to insert the same record simultaneously. Evidence records are never deleted — re-gathering a claim appends new records without removing existing ones.

**`raw_data` is a JSONB copy, not a reference.** When evidence is gathered, the relevant fields from each source record are copied into `liability_evidence.raw_data` as `json.dumps(raw_data, default=str, sort_keys=True)`. If the source record is later GDPR-erased, modified, or deleted, the evidence record retains its captured state.

---

## GDPR-Erased Disclosure Records

```python
# In _gather_context_disclosures():
if row["erased"]:
    summary = "[ERASED - field data unavailable]"
    raw_data = {}
else:
    summary = f"Disclosed {disclosed} to service {row['service_id']}"
    raw_data = {
        "fields_disclosed": disclosed,
        ...
    }
await _insert_evidence_if_missing(db, ..., summary=summary, raw_data=raw_data)
```

A GDPR-erased disclosure (Layer 4 erasure, Lesson 38) still produces an evidence record with an empty `raw_data` and a `[ERASED]` summary. The record proves that a disclosure happened — its absence from the evidence package would be misleading to an attribution engine or a human reviewer. The tombstone pattern: the fact of the disclosure is preserved; the content is not.

---

## The Six-State Claim Lifecycle

```
filed
  ↓ (gather_evidence completes)
evidence_gathered
  ↓ (human reviewer assigned)
under_review
  ↓ (determine endpoint called)
determined
  ↓ (resolve endpoint called)
resolved
  ↑ (appeal endpoint called)
appealed → under_review (re-enters review cycle)
```

Each status transition is enforced by the service layer:

| Endpoint | From status | To status | Constraint |
|----------|------------|----------|-----------|
| `POST /claims/{id}/gather` | `filed` | `evidence_gathered` | Evidence gathering completes |
| Admin assignment | `evidence_gathered` | `under_review` | `reviewer_did` must be set |
| `POST /claims/{id}/determine` | `under_review` | `determined` | Attribution weights written |
| `POST /claims/{id}/resolve` | `determined` | `resolved` | Resolution note required |
| `POST /claims/{id}/appeal` | `resolved` | `appealed` → `under_review` | Loops back to review |

A claim can be appealed at most once before requiring a new determination. Appeals increment `determination_version` on the next determination record.

---

## The Redis Claim Status Cache

```python
# Cache key: liability:claim_status:{claim_id}
# TTL: 60 seconds
```

After each status transition, the service calls `refresh_claim_status_cache(redis, claim_id, new_status)`. Callers that poll claim status frequently (e.g., a compliance dashboard) hit the Redis cache rather than the database. The `GET /liability/claims/{claim_id}` endpoint reads the full claim detail from DB (no cache) — the status cache is only used for lightweight polling paths.

---

## Exercise 1 — File a Claim

After creating and reporting a workflow execution (Lesson 48):

```bash
EXECUTION_ID="<execution-uuid>"

curl -s -X POST "http://localhost:8000/v1/liability/claims" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"execution_id\": \"$EXECUTION_ID\",
    \"claimant_did\": \"did:key:z6MkTestContextAgent\",
    \"claim_type\": \"service_failure\",
    \"description\": \"Step 1 service returned empty result for travel.air.search\",
    \"harm_value_usd\": 150.00
  }" | python -m json.tool
```

**Expected:** 201 with `claim_id`, `status="filed"`, and `snapshot_id`.

---

## Exercise 2 — Observe Duplicate Rejection

Submit the same claim twice:

```bash
CLAIM_ID="<first-claim-uuid>"

curl -s -X POST "http://localhost:8000/v1/liability/claims" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"execution_id\": \"$EXECUTION_ID\",
    \"claimant_did\": \"did:key:z6MkTestContextAgent\",
    \"claim_type\": \"service_failure\",
    \"description\": \"Retry of same claim\"
  }" | python -m json.tool
```

**Expected:** 409 Conflict — same `(execution_id, claimant_did, claim_type)` already exists.

---

## Exercise 3 — Trigger Evidence Gathering

```bash
CLAIM_ID="<claim-uuid>"

curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/gather" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

Then inspect the gathered evidence:

```bash
curl -s "http://localhost:8000/v1/liability/claims/$CLAIM_ID" \
  -H "X-API-Key: dev-local-only" | python -c "
import sys, json
d = json.load(sys.stdin)
evidence = d.get('evidence', [])
print(f'Evidence records: {len(evidence)}')
for e in evidence:
    print(f\"  Layer {e['source_layer']} | {e['evidence_type']} | {e['summary']}\")
"
```

**Expected:** 3–8 evidence records depending on how much Layer 4 context activity occurred during the execution window.

---

## Best Practices

**Evidence gathering is idempotent.** Calling `POST /claims/{id}/gather` multiple times does not create duplicate evidence records. The `_insert_evidence_if_missing()` pattern and the `ON CONFLICT DO NOTHING` constraint make re-gathering safe. This enables re-triggering evidence gathering if new data arrives (e.g., a revocation that was being confirmed when the claim was first filed).

**Recommended (not implemented here):** A scheduled re-gather for claims where evidence gathering completed but no revocation records were found — revocations can take up to 20 blocks (40 seconds on Polygon) to confirm after being submitted. A re-gather 5 minutes after initial filing would catch late-confirming revocations.

---

## Interview Q&A

**Q: Why is a snapshot required before a claim can be filed? What happens if the snapshot doesn't exist?**
A: The snapshot is the evidentiary anchor — it contains the frozen trust state at execution time that attribution depends on. Without a snapshot, attribution would have to query current trust scores, which may have changed since execution. If no snapshot exists (which should only happen if snapshot creation failed at execution time), `file_claim()` returns 422 Unprocessable Entity — the claim cannot proceed without the frozen evidence.

**Q: Why does evidence gathering store raw_data as a JSONB copy rather than a foreign key reference?**
A: Source records can be modified, GDPR-erased, or deleted after a claim is filed. A foreign key reference would point to a record that may no longer contain the same data — or may no longer exist. Copying the relevant fields at gather time ensures the evidence package is stable and forensically complete regardless of what happens to the source records afterward.

**Q: Why are there two deduplication mechanisms in `_insert_evidence_if_missing()` — a SELECT check and an `ON CONFLICT DO NOTHING`?**
A: The SELECT check prevents most duplicates under normal operation (single gather task). The `ON CONFLICT DO NOTHING` handles the concurrent case where two tasks (e.g., a retry and the original) both check, both find no existing record, and both attempt to INSERT simultaneously. The database constraint guarantees exactly-once insertion in all cases.

---

## Key Takeaways

- Five claim types: service_failure, data_misuse, wrong_outcome, unauthorized_action, workflow_design_flaw — each signals a primary suspect
- `file_claim()` runs four checks: rate limit, execution exists, snapshot exists, no duplicate
- Rate limit: 10 claims/hour per claimant DID (hashed), fail-open
- Eight evidence sources spanning Layers 1–6; `_insert_evidence_if_missing()` is idempotent
- GDPR-erased disclosures produce tombstone evidence records — the fact of disclosure is preserved, not the content
- Six-state lifecycle: filed → evidence_gathered → under_review → determined → resolved / appealed

---

## Next Lesson

**Lesson 54 — The Attribution Engine: Computing Responsibility Weights** traces the 11-factor algorithm, explains how each factor shifts weight between the four actors, and shows how the normalization step guarantees weights always sum to 1.0.
