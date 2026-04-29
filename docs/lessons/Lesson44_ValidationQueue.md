# Lesson 44: The Inspection Panel â€” Human Validation Queue

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_validator.py`, `api/models/workflow.py` (checklist validators)
**Prerequisites:** Lesson 43
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A building inspector signs off on a blueprint only after physically walking through the building. Layer 5's validation queue is that inspection: a domain expert reviews the workflow, checks all five items on the validation checklist, and records a decision that transitions the workflow's status â€” permanently, in the case of publication.

This lesson traces the full state machine and the two functions that drive it: `assign_workflow_to_validator()` and `record_validator_decision()`.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Recite the five validation checklist items and explain what each protects against
- Trace the full state machine: draft â†’ in_review â†’ published/rejected/revision_requested
- Explain `compute_spec_hash()` and why the hash is set at approval time, not submission time
- Trace `record_validator_decision()` through all three decision branches
- Explain `compute_initial_quality_score()` and what a newly published workflow's score will be
- Describe what happens when a validator tries to approve a workflow they were not assigned to

---

## The State Machine

```
submitted
    â†“ POST /workflows
status = 'draft'
    â†“ POST /workflows/{id}/validate (admin assigns validator_did)
status = 'in_review'
    â†“ PUT /workflows/{id}/validation
    â”œâ”€â”€ decision = 'approved'           â†’ status = 'published'   (terminal positive)
    â”œâ”€â”€ decision = 'rejected'           â†’ status = 'rejected'    (terminal negative)
    â””â”€â”€ decision = 'revision_requested' â†’ status = 'draft'       (back to author)
```

`rejected` and `published` are both terminal â€” a rejected workflow cannot be re-submitted with the same UUID. An author who received a rejection creates a new `POST /workflows` submission (optionally with `parent_workflow_id` pointing to the rejected UUID for lineage tracking).

`revision_requested` returns the workflow to `draft` status. The author calls `PUT /workflows/{id}` to update the spec, which re-validates against the ten rules. A new `POST /workflows/{id}/validate` assignment is then required before the validator can review again.

---

## The Five-Item Validation Checklist

Every validator decision must include this structured checklist. All five items must be `true` for `decision='approved'`.

| # | Key | What it checks |
|---|-----|---------------|
| 1 | `steps_achievable` | Each step references a real, published ontology tag with at least one capable service in the registry |
| 2 | `context_minimal` | No step requests context fields beyond what is reasonably necessary |
| 3 | `trust_thresholds_appropriate` | `min_trust_tier` and `min_trust_score` are proportional to the action's sensitivity |
| 4 | `no_sensitive_tag_without_domain_review` | High-sensitivity steps were reviewed by a domain-appropriate validator |
| 5 | `fallback_logic_sound` | Any `fallback_step_number` references lead to safe degraded outcomes, not service escalations |

These are enforced by a Pydantic validator on `ValidatorDecisionRequest`:

```python
# api/models/workflow.py
class ValidationChecklist(BaseModel):
    steps_achievable: bool
    context_minimal: bool
    trust_thresholds_appropriate: bool
    no_sensitive_tag_without_domain_review: bool
    fallback_logic_sound: bool

    @model_validator(mode="after")
    def _all_must_pass(self) -> "ValidationChecklist":
        failed = [k for k, v in self.model_dump().items() if not v]
        if failed:
            raise ValueError(f"all checklist items must be true for approval; failed: {failed}")
        return self
```

A validator cannot submit `decision='approved'` with any checklist item set to `false` â€” the request is rejected at the Pydantic layer before any database call.

---

## `assign_workflow_to_validator()` â€” The Gate Check

```python
# api/services/workflow_validator.py:157â€“269
async def assign_workflow_to_validator(db, workflow_id, request) -> ValidationResponse:
    workflow = await _load_workflow_for_validation(db, workflow_id)
    if workflow["status"] != "draft":
        raise HTTPException(409, "workflow must be draft to assign validation")

    pending = await _load_pending_validation(db, workflow_id)
    if pending is not None and pending["validator_did"] != VALIDATION_QUEUE_DID:
        raise HTTPException(409, "active validation already assigned")
```

Two gate checks before any write:

1. **Status check:** Only `draft` workflows can be assigned. An `in_review` workflow that already has a human validator cannot be re-assigned without the current validator first making a decision.

2. **Pending validation check:** Reads the existing `workflow_validations` row. If the `validator_did` is not the sentinel (`VALIDATION_QUEUE_DID`), a real validator is already assigned â†’ 409. If the row is the sentinel, the admin is assigning for the first time â€” the sentinel row is UPDATEd with the real validator's DID rather than creating a new row.

**Why update the sentinel row rather than inserting a new one?** There is one validation record per workflow per review cycle. Inserting a new row each time a validator is assigned would create multiple active validation records, complicating the "load pending validation" query. Updating the sentinel preserves the single-row invariant.

---

## `record_validator_decision()` â€” The Three Branches

```python
# api/services/workflow_validator.py:272â€“394
async def record_validator_decision(db, workflow_id, request, redis=None) -> WorkflowRecord:
    validation = await _load_pending_validation(db, workflow_id)
    if validation is None:
        raise HTTPException(404, "no active validation for this workflow")
    if validation["validator_did"] != request.validator_did:
        raise HTTPException(403, "validator_did does not match assigned validator")
```

**DID check:** The validator who was assigned must be the one submitting the decision. A different DID â€” even an admin â€” cannot record a decision for someone else's assignment. This preserves the accountability chain: the validation record always identifies exactly who reviewed and approved the workflow.

### Branch 1: `decision='approved'`

```python
if request.decision == "approved":
    spec_hash = compute_spec_hash(spec)         # SHA-256(sorted JSON)
    quality_score = compute_initial_quality_score(
        await _avg_step_trust(db, workflow_id)  # mean trust of pinned services
    )
    # UPDATE workflows SET status='published', spec_hash=..., quality_score=..., published_at=NOW()
    # After commit: invalidate_workflow_caches(redis, workflow_id=workflow_id)
```

At approval time three things happen atomically:

1. `spec_hash` is computed and stored â€” the immutability proof
2. `quality_score` is computed â€” the initial ranking signal
3. `published_at` is set â€” the public availability timestamp

After commit, all workflow caches are invalidated so the published status is immediately visible.

### Branch 2: `decision='rejected'`

```python
elif request.decision == "rejected":
    # UPDATE workflows SET status='rejected'
```

Status transitions to `rejected`. No score is set, no hash is locked. The workflow is effectively archived â€” it cannot be updated or resubmitted under the same UUID.

### Branch 3: `decision='revision_requested'`

```python
else:
    # UPDATE workflows SET status='draft'
```

Returns the workflow to `draft`. The validator's `revision_notes` are stored in `workflow_validations`. The author reads the notes via `GET /workflows/{id}/validation` and calls `PUT /workflows/{id}` to update the spec.

---

## `compute_spec_hash()` â€” The Immutability Proof

```python
# api/services/workflow_validator.py:25â€“27
def compute_spec_hash(spec: dict[str, Any]) -> str:
    return sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
```

**`sort_keys=True`** ensures the hash is canonical. Two spec dicts that are semantically identical but have different key ordering produce the same hash. Without `sort_keys`, JSON serialization order could vary between Python runs, making the hash non-deterministic.

**Hashing the stored spec, not the submission.** The `spec` parameter is the full JSONB document from `workflows.spec` â€” including the `quality` block and `accountability` section. This means the hash covers the complete stored representation, not just the step definitions. Any post-hoc modification of the stored spec (e.g., editing `quality.quality_score` directly in the DB) would invalidate the hash.

---

## `compute_initial_quality_score()` â€” What a New Workflow Scores

```python
# api/services/workflow_validator.py:30â€“44
def compute_initial_quality_score(avg_step_trust: float) -> float:
    validation_score = 1.0      # just published â†’ full validation credit
    success_rate = 0.0          # no executions yet
    verification_rate = 0.0     # no executions yet
    volume_factor = 0.0         # min(1.0, 0/100) = 0
    raw = (
        1.0 * 0.35              # 0.35
        + 0.0 * 0.30 * 0.0     # 0.00
        + 0.0 * 0.20            # 0.00
        + avg_step_trust * 0.15 # 0.075 if avg_step_trust = 0.5
    )                           # raw = 0.4250 (with default 0.5 trust)
    if verification_rate < 0.5:
        raw = min(raw, 0.70)    # 0.4250 < 0.70, no change
    return round(raw * 100, 2)  # = 35.0
```

**A newly published workflow starts at ~35.0** (assuming no pinned services and 0.5 default trust). This is enough to appear in search results but below "proven" workflows that have accumulated verified execution history.

**`avg_step_trust`**: For steps with no `service_id`, the function uses 0.5 (normalized trust = 50/100). For pinned services, it uses their actual `trust_score / 100`. A workflow where all steps are pinned to high-trust (e.g., 90-score) services starts at:
```
raw = 0.35 + 0 + 0 + 0.90*0.15 = 0.35 + 0.135 = 0.485
quality_score = 48.5
```

---

## Exercise 1 â€” Full State Machine Walk-Through

```bash
# Step 1: Submit a workflow (from Lesson 43 exercise)
WORKFLOW_ID="<uuid-from-post>"

# Step 2: Assign validator (admin operation)
curl -s -X POST "http://localhost:8000/v1/workflows/$WORKFLOW_ID/validate" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "validator_did": "did:key:z6MkValidatorAgent",
    "validator_domain": "FINANCE"
  }' | python -m json.tool

# Step 3: Record approval
curl -s -X PUT "http://localhost:8000/v1/workflows/$WORKFLOW_ID/validation" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "validator_did": "did:key:z6MkValidatorAgent",
    "decision": "approved",
    "checklist": {
      "steps_achievable": true,
      "context_minimal": true,
      "trust_thresholds_appropriate": true,
      "no_sensitive_tag_without_domain_review": true,
      "fallback_logic_sound": true
    }
  }' | python -m json.tool
```

**Expected after approval:** `status="published"`, `spec_hash` is a 64-character hex string, `quality_score â‰ˆ 35.0`.

---

## Exercise 2 â€” Verify Spec Immutability

After the workflow is published, attempt to update the spec:

```bash
curl -s -X PUT "http://localhost:8000/v1/workflows/$WORKFLOW_ID" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "spec_version": "1.0",
    "name": "Finance Report Pull v2",
    "slug": "finance-report-pull-v2",
    ...
  }' | python -m json.tool
```

**Expected:** 409 Conflict: "published workflow spec is immutable; submit a new workflow to create an updated version."

---

## Exercise 3 â€” Wrong Validator DID

Try to approve a workflow using a DID that was not assigned:

```bash
curl -s -X PUT "http://localhost:8000/v1/workflows/$WORKFLOW_ID/validation" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "validator_did": "did:key:z6MkDifferentValidator",
    "decision": "approved",
    "checklist": {...all true...}
  }' | python -m json.tool
```

**Expected:** 403 Forbidden: "validator_did does not match assigned validator."

---

## Best Practices

**The checklist is the accountability artifact.** When a workflow causes harm, the `workflow_validations` table shows who reviewed it, when, and which checklist items they confirmed. Design the checklist to capture judgment calls, not just mechanical checks â€” a validator who rubber-stamps all five items without review is creating a paper trail that shows they approved a harmful workflow.

**Recommended (not implemented here):** An appeal process for rejected workflows â€” a second validator can overturn a rejection without the author re-submitting. This would require a second `workflow_validations` row with a different `validator_did` and an `appeal` flag on the decision.

---

## Interview Q&A

**Q: Why can only the assigned validator submit a decision?**
A: The `validator_did` in `workflow_validations` is the accountability record â€” it names who reviewed the workflow. If any DID could submit a decision, the accountability chain would be meaningless. The DID check ensures the person who was assigned is the person who reviewed.

**Q: Why is `spec_hash` computed from the stored JSONB spec rather than from the submission request?**
A: The stored spec includes the `quality` and `accountability` blocks added by `_spec_payload()`. Hashing the stored document ensures the hash covers the complete published artifact â€” the same bytes an agent platform would retrieve via `GET /workflows/{id}`. This makes the hash verifiable: anyone can compute `sha256(json.dumps(retrieved_spec, sort_keys=True))` and compare to `spec_hash`.

**Q: What happens if a validator submits `revision_requested` but provides no `revision_notes`?**
A: `revision_notes` is optional in the Pydantic model â€” the field is `str | None`. A revision request with no notes is valid but unhelpful. The best practice (not enforced) would be to require `revision_notes` when `decision='revision_requested'`. Current implementation trusts the validator to provide useful notes.

---

## Key Takeaways

- State machine: draft â†’ in_review â†’ published/rejected (terminal) or draft (revision)
- Five-item checklist: all must be `true` for `decision='approved'`; enforced by Pydantic
- `compute_spec_hash()` uses `sha256(json.dumps(spec, sort_keys=True))` â€” canonical JSON
- Spec hash is set at approval time and never changed â€” published specs are immutable
- `compute_initial_quality_score()` produces ~35.0 for an unpinned, newly published workflow
- Validator DID check enforces accountability: only the assigned validator can decide

---

## Next Lesson

**Lesson 45 â€” The Quality Ledger: Composite Scoring Engine** dives into `compute_workflow_quality_score()` â€” the four-component formula, volume factor scaling, the unverifiable cap, and `_avg_step_trust()` â€” and explains how execution history moves a workflow's score from 35.0 toward 100.0 over time.
