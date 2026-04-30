# Lesson 47: The One-Stop Approval — Context Bundle Integration

> **Beginner frame:** A context bundle is one approval package for a workflow's context needs. It prevents every step from becoming a separate privacy negotiation.

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_context.py`, `api/models/workflow.py`
**Prerequisites:** Lessons 34, 46
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

When you start a bank account, the teller doesn't ask for your ID separately for every form. They verify your identity once, staple everything together, and you sign one package. Layer 5's context bundle works the same way: instead of the agent approving a separate context disclosure for each step of a six-step workflow, they review one aggregated view of all the fields that will flow across all steps â€” and approve it once.

This lesson traces `create_context_bundle()` through its field aggregation, scoped profile override injection, and Layer 4 profile evaluation â€” and the `approve_context_bundle()` flow that makes the bundle ready for workflow execution.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain why a workflow context bundle exists (versus six separate Layer 4 match calls)
- Trace `create_context_bundle()` through its seven steps
- Explain `_apply_scoped_overrides()` and how it injects a priority-0 rule
- Describe what `_classify_step_fields()` returns and how it reuses Layer 4's `evaluate_profile()`
- Explain the `_dedupe()` function and why it preserves first-seen order
- Trace `approve_context_bundle()` and identify the three pre-approval checks
- Describe the 30-minute TTL and the consumed-bundle anti-reuse pattern

---

## Why Bundles Exist

Without bundles, a six-step workflow requires six separate agent interactions:

```
Step 1: agent calls POST /context/match â†’ reviews classification â†’ calls POST /context/disclose
Step 2: agent calls POST /context/match â†’ reviews classification â†’ calls POST /context/disclose
...
Step 6: agent calls POST /context/match â†’ reviews classification â†’ calls POST /context/disclose
```

For a human agent using a UI, this is six separate consent screens. For an automated agent, it is twelve Layer 4 round-trips with no ability to preview what will be shared across the whole workflow before committing to the first step.

The bundle solves this: **one approval authorizes context disclosure for all steps**. The agent reviews the aggregated field breakdown before the workflow starts, approves once, and the `bundle_id` is passed to each step's Layer 4 match call as pre-authorization.

---

## `create_context_bundle()` â€” Seven Steps

```python
# api/services/workflow_context.py:327â€“438
async def create_context_bundle(*, workflow_id, agent_did,
                                 scoped_profile_overrides, db, redis) -> BundleResponse:
```

**Step 1 â€” Load workflow and steps:**
```python
workflow, steps = await _load_workflow_steps(db, workflow_id)
```
One SQL query loads the published workflow and all its steps in execution order.

**Step 2 â€” Load the agent's base profile:**
```python
base_profile = await _load_profile_or_default(db, agent_did, redis=redis)
```
Calls `context_profiles.get_active_profile()` â€” which checks the 60s Redis cache first. If no profile exists for this agent, returns a default-deny profile (no rules, `default_policy="deny"`).

**Step 3 â€” Apply scoped overrides:**
```python
profile = _apply_scoped_overrides(base_profile, scoped_profile_overrides)
```
If the request included field-level overrides (e.g., `"user.frequent_flyer_id": "permit"`), injects a priority-0 rule that overrides the base profile for those fields. See `_apply_scoped_overrides()` below.

**Step 4 â€” Persist scoped profile (if overrides provided):**
```python
if scoped_profile_overrides:
    scoped_profile_id = await _create_scoped_profile(...)
```
Creates or updates a `workflow_scoped_profiles` row with `ON CONFLICT (workflow_id, agent_did) DO UPDATE`. This upsert pattern means the agent can re-create a bundle for the same workflow with different overrides â€” the scoped profile is refreshed rather than duplicated.

**Step 5 â€” Classify fields per step:**
```python
for step in steps:
    requested_fields = _dedupe(required_fields + optional_fields)
    service = await _service_context_for_step(db, workflow, step, requested_fields)
    breakdown = _classify_step_fields(profile=profile, service=service,
                                       requested_fields=requested_fields)
    by_step[f"step_{step['step_number']}"] = breakdown
```
For each step, classify every field (required + optional) as `permitted`, `committed`, or `withheld`. The classification uses the agent's scoped profile and a `ServiceContext` built from the step's trust thresholds and ontology tag.

**Step 6 â€” Deduplicate union across all steps:**
```python
all_permitted = _dedupe(permitted_union)
all_committed = _dedupe(committed_union)
all_withheld  = _dedupe(withheld_union)
```
A field that appears in three steps is listed once in the union. The agent sees "you will share `user.name` with this workflow" rather than "you will share `user.name` three times."

**Step 7 â€” Insert bundle row:**
```python
INSERT INTO workflow_context_bundles (
    workflow_id, agent_did, scoped_profile_id, status,
    approved_fields, expires_at
) VALUES (..., 'pending', ..., NOW() + INTERVAL '30 minutes')
RETURNING id, expires_at
```
The bundle is created in `pending` status with a 30-minute TTL. The full field breakdown is stored as JSONB in `approved_fields`.

---

## `_apply_scoped_overrides()` â€” Priority-0 Rule Injection

```python
# api/services/workflow_context.py:124â€“157
def _override_rule(scoped_profile_overrides: dict[str, str]) -> Any | None:
    if not scoped_profile_overrides:
        return None
    permitted = sorted(f for f, a in scoped_profile_overrides.items() if a == "permit")
    denied    = sorted(f for f, a in scoped_profile_overrides.items() if a in {"deny","withhold"})
    return SimpleNamespace(
        priority=0,           # lower number = evaluated first
        scope_type="sensitivity",
        scope_value="1",
        permitted_fields=permitted,
        denied_fields=denied,
        action="permit",
    )

def _apply_scoped_overrides(profile, scoped_profile_overrides) -> Any:
    rule = _override_rule(scoped_profile_overrides)
    if rule is None:
        return profile
    return SimpleNamespace(
        profile_id=getattr(profile, "profile_id", None),
        default_policy=profile.default_policy,
        rules=[rule, *list(profile.rules)],   # override rule first
    )
```

**How it works:** Layer 4's `evaluate_profile()` iterates rules in priority order (ascending). Inserting the override rule at priority 0 â€” before all existing rules â€” means it is always evaluated first. If the override permits a field, evaluation stops immediately and returns `"permit"`, regardless of what lower-priority rules say.

**Scope type `"sensitivity"` with scope_value `"1"`:** This makes the rule match on `sensitivity_tier >= 1`, which covers every field. The rule's effect comes from its `permitted_fields` and `denied_fields` lists, not from scope filtering.

This design means scoped overrides are **additive, not replacing**: the base profile's rules still apply for fields not mentioned in the overrides. An override that permits `user.frequent_flyer_id` does not affect `user.ssn` â€” that field is still evaluated against the base profile rules.

---

## `_classify_step_fields()` â€” Layer 4 Reuse

```python
# api/services/workflow_context.py:278â€“305
def _classify_step_fields(*, profile, service, requested_fields) -> BundleFieldBreakdown:
    permitted, withheld, committed = [], [], []
    for field in requested_fields:
        decision = evaluate_profile(
            profile.rules,
            field,
            service,
            profile.default_policy,
        )
        if decision == "permit":
            permitted.append(field)
        elif decision == "commit":
            committed.append(field)
        else:
            withheld.append(field)
    return BundleFieldBreakdown(permitted=permitted, withheld=withheld, committed=committed)
```

This is `evaluate_profile()` imported directly from `context_matcher.py` â€” the same function used in the Layer 4 8-step matching engine. The bundle classification uses exactly the same logic as a real Layer 4 match call, so the bundle preview is an accurate prediction of what will happen at match time.

**`committed` verdict:** A field classified as `"commit"` means it would require an HMAC commitment at match time â€” it is a high-sensitivity field that the agent's profile permits but that Layer 4 would not transmit as plaintext. The bundle shows this to the user before they approve.

---

## `_dedupe()` â€” Ordered Deduplication

```python
# api/services/workflow_context.py:41â€“50
def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
```

**Why preserve order?** The field names in the bundle response appear in the order they are first encountered during step iteration (step 1 first, then step 2, etc.). The `_dedupe()` function preserves this "first seen" ordering rather than sorting alphabetically, which makes the response intuitive â€” fields are listed in the order they are first needed.

---

## `approve_context_bundle()` â€” Three Pre-Approval Checks

```python
# api/services/workflow_context.py:457â€“515
async def approve_context_bundle(*, bundle_id, request, db) -> BundleApproveResponse:
    row = await db.execute("SELECT ... WHERE id = :bundle_id AND agent_did = :agent_did", ...)
    if row is None:
        raise HTTPException(404, "bundle not found for this agent_did")

    if _utc_now() > _ensure_aware(row["expires_at"]):
        raise HTTPException(410, "bundle expired")

    if row["status"] != "pending":
        raise HTTPException(409, "bundle already approved/rejected/consumed")

    UPDATE workflow_context_bundles SET status='approved', user_approved_at=NOW()
```

**Check 1 â€” Bundle + agent_did ownership:** The query filters on both `bundle_id` and `agent_did`. A service or admin cannot approve another agent's bundle. The 404 response does not distinguish "bundle doesn't exist" from "bundle belongs to a different agent" â€” leaking which bundles exist for other agents would be a privacy concern.

**Check 2 â€” Expiry:** The 30-minute TTL is enforced at approval time. An agent that creates a bundle but doesn't approve it within 30 minutes gets a 410 Gone â€” they must create a new bundle.

**Check 3 â€” Status idempotency:** Only `pending` bundles can be approved. An already-approved bundle cannot be re-approved (409 Conflict). A consumed bundle cannot be re-used.

---

## The Bundle Status State Machine

```
pending     (created, awaiting user approval)
  â†“ POST /workflows/context/bundle/{id}/approve
approved    (user approved; agent may proceed with workflow)
  â†“ POST /workflows/{id}/executions (bundle_id provided)
consumed    (bundle used in a completed execution â€” cannot be reused)

rejected    (user declined; workflow cannot proceed)
```

**`consumed`** is set when `workflow_registry.py:report_execution()` references the bundle. Once consumed, the bundle cannot authorize another execution â€” the agent must create a new bundle for the next run of the same workflow.

---

## Exercise 1 â€” Create and Approve a Bundle

```bash
# Step 1: Create bundle
BUNDLE=$(curl -s -X POST http://localhost:8000/v1/workflows/context/bundle \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "<published-workflow-uuid>",
    "agent_did": "did:key:z6MkTestContextAgent",
    "scoped_profile_overrides": {}
  }')
echo "$BUNDLE" | python -m json.tool

BUNDLE_ID=$(echo "$BUNDLE" | python -c "import sys,json; print(json.load(sys.stdin)['bundle_id'])")

# Step 2: Approve
curl -s -X POST "http://localhost:8000/v1/workflows/context/bundle/$BUNDLE_ID/approve" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"agent_did": "did:key:z6MkTestContextAgent"}' | python -m json.tool
```

**Expected after approval:** `status="approved"`, `approved_at` timestamp set.

---

## Exercise 2 â€” Scoped Override Changes Classification

First, create a bundle with empty overrides and note which fields are withheld. Then create a new bundle with `scoped_profile_overrides` that permits one withheld field. Compare the two `by_step` breakdowns.

```bash
# Bundle with override permitting a withheld field
curl -s -X POST http://localhost:8000/v1/workflows/context/bundle \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "<published-workflow-uuid>",
    "agent_did": "did:key:z6MkTestContextAgent",
    "scoped_profile_overrides": {"user.frequent_flyer_id": "permit"}
  }' | python -m json.tool
```

**Expected:** The override-permitted field should appear in `permitted` rather than `withheld`, even if the base profile would withhold it.

---

## Exercise 3 â€” Expired Bundle Rejection

Set the bundle TTL very short in a test environment and observe the 410 response. Alternatively, directly update an existing bundle's `expires_at` in psql:

```bash
BUNDLE_ID="<pending-bundle-uuid>"
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "UPDATE workflow_context_bundles SET expires_at = NOW() - INTERVAL '1 second' WHERE id = '$BUNDLE_ID';"

curl -s -X POST "http://localhost:8000/v1/workflows/context/bundle/$BUNDLE_ID/approve" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"agent_did": "did:key:z6MkTestContextAgent"}' | python -m json.tool
```

**Expected:** 410 Gone with `"bundle expired"` detail.

---

## Best Practices

**Bundles are a user-facing feature, not just a technical one.** The `by_step` breakdown in the bundle response is designed to be rendered in a UI: "In Step 1 (flight booking), your name and email will be shared. In Step 2 (hotel), your name and email will be shared again. Your frequent flyer ID will be committed (not sent as plaintext)." The data structure maps directly to UI components.

**Recommended (not implemented here):** A "re-create bundle" endpoint that creates a new bundle from an existing one's settings â€” useful when an agent's profile changes between the time they planned a workflow and when they're ready to execute it.

---

## Interview Q&A

**Q: Why does the scoped override use `priority=0` rather than always being evaluated last?**
A: Lower priority number = evaluated first in Layer 4's rule evaluation order. Priority 0 means the override fires before any of the agent's base profile rules â€” it takes precedence. This allows the override to grant permissions that the base profile would deny. If it were lowest priority (evaluated last), the base profile's deny rules would prevent the override from having any effect.

**Q: What is the relationship between a `workflow_context_bundles` row and the Layer 4 `context_disclosures` rows created during execution?**
A: The bundle row records pre-approval of context disclosure. As the workflow executes step by step, each Layer 4 match + disclose sequence writes a `context_disclosures` row (as covered in Lesson 37). After execution completes, `workflow_executor.py:_verify_execution_by_bundle()` cross-checks whether `context_disclosures` rows exist for each step within the bundle's time window. If they do, the execution is marked `verified=true`.

**Q: Why is the bundle scoped to `(workflow_id, agent_did)` rather than just `bundle_id`?**
A: A bundle represents an agent's pre-approval for a specific workflow. Two bundles for the same `(workflow_id, agent_did)` would represent conflicting approvals. The scoped profile upsert (`ON CONFLICT (workflow_id, agent_did) DO UPDATE`) enforces that only one active scoped profile exists per agent per workflow. The bundle itself is identified by its UUID but is always validated against its owning agent.

---

## Key Takeaways

- Bundle purpose: single user approval for context disclosure across all workflow steps
- `_apply_scoped_overrides()` injects a priority-0 rule that overrides the base profile for specified fields
- `_classify_step_fields()` reuses Layer 4's `evaluate_profile()` â€” the bundle preview is accurate
- `_dedupe()` preserves first-seen field order across steps
- Bundle approval: three checks (ownership, expiry, status idempotency)
- Bundle status: pending â†’ approved â†’ consumed (one-way, consumed bundles cannot be reused)
- 30-minute TTL; expired bundles return 410 Gone

---

## Next Lesson

**Lesson 48 â€” The Feedback Machine: Execution Outcome Reporting** covers `workflow_executor.py` â€” how execution reports increment counters, how `_verify_execution_by_bundle()` cross-checks against the Layer 4 audit trail, and how the `verified=true` flag lifts the 70.0 quality cap.
