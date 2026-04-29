# Lesson 42: The Blueprint Department â€” Workflow Spec Format & Validation Rules

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/models/workflow.py`, `api/services/workflow_registry.py` (lines 200â€“422)
**Prerequisites:** Lesson 41
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

A building permit office doesn't accept blueprints that show walls going through each other, rooms with no exits, or electrical diagrams that reference breakers that don't exist. Layer 5's submission endpoint is that permit office: it validates the workflow spec against ten rules before creating any database record.

This lesson covers the spec format that agent platforms consume and the validation logic that ensures every spec in the registry is structurally sound before it ever reaches a human validator.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Read and write a complete `WorkflowCreateRequest` spec in JSON
- Recite all ten spec validation rules from memory
- Trace `_validate_workflow_spec()` through its two DB queries
- Explain `_validation_domain()` and why sensitivity_tier â‰¥ 3 changes the validator assignment
- Identify which validation rules are enforced by Pydantic and which require DB access
- Describe `_spec_payload()` and the initial quality/accountability block inserted with a new workflow

---

## The Workflow Spec Format

A workflow spec is a JSONB object stored in `workflows.spec`. It is also the primary request body for `POST /workflows`. The spec contains four top-level sections:

```json
{
  "spec_version": "1.0",
  "name": "Business Travel Booking",
  "slug": "business-travel-booking",
  "description": "Book a complete business trip: flights, hotel, and ground transport",
  "ontology_domain": "TRAVEL",
  "tags": ["travel.air.book", "travel.lodging.book", "travel.ground.rideshare"],
  "steps": [...],
  "accountability": {
    "author_did": "did:key:z6Mk..."
  }
}
```

### The `steps` array

Each step is an ordered service interaction:

```json
{
  "step_number": 1,
  "name": "Search and book flight",
  "ontology_tag": "travel.air.book",
  "service_id": null,
  "is_required": true,
  "fallback_step_number": null,
  "context_fields_required": ["user.name", "user.email"],
  "context_fields_optional": ["user.frequent_flyer_id"],
  "min_trust_tier": 3,
  "min_trust_score": 75.0,
  "timeout_seconds": 30
}
```

**`service_id: null`** means any active service capable of `travel.air.book` can fill this step. **`service_id: "<uuid>"`** pins the step to a specific service â€” validation then cross-checks that service's Layer 1 manifest.

**`fallback_step_number`** â€” if this step fails, jump to this step instead of aborting. Must reference a *higher* step number to prevent loops.

---

## The Ten Validation Rules

Layer 5 enforces ten spec validation rules at submission time. Rules 1â€“9 are implemented in Pydantic validators on `WorkflowCreateRequest`; Rule 10 requires DB access.

### Pydantic-enforced rules (no DB needed)

| # | Rule | Validation location |
|---|------|-------------------|
| 1 | `spec_version` must equal `"1.0"` | Pydantic `Literal["1.0"]` field |
| 2 | `steps` must have 1â€“20 entries | Pydantic `min_length=1, max_length=20` |
| 3 | `step_number` values must be sequential starting at 1, no gaps | `@model_validator` |
| 5 | `fallback_step_number` must reference a higher step number (no backward jumps) | `@model_validator` |
| 7 | `min_trust_tier` must be between 1 and 4 | Pydantic `ge=1, le=4` |
| 8 | `min_trust_score` must be between 0.0 and 100.0 | Pydantic `ge=0.0, le=100.0` |
| 9 | No step may reference the same ontology_tag twice unless service_id values differ | `@model_validator` |

### DB-enforced rules (require registry queries)

| # | Rule | Code |
|---|------|------|
| 4 | All `ontology_tag` values must exist in `ontology_tags` | `_load_ontology_rows()` |
| 6 | `context_fields_required` must be declared in the service's manifest (if `service_id` pinned) | `_validate_pinned_service_step()` |
| 10 | Sensitivity_tier â‰¥ 3 steps require domain-appropriate validator | `_validation_domain()` |

---

## `_validate_workflow_spec()` â€” The Two DB Queries

```python
# api/services/workflow_registry.py:306â€“322
async def _validate_workflow_spec(
    db: AsyncSession,
    request: WorkflowCreateRequest,
) -> dict[str, Mapping[str, Any]]:
    step_tags = [step.ontology_tag for step in request.steps]
    missing_from_tags = [tag for tag in sorted(set(step_tags)) if tag not in request.tags]
    if missing_from_tags:
        raise HTTPException(
            status_code=422,
            detail=f"workflow tags must include step tags: {', '.join(missing_from_tags)}",
        )

    ontology_rows = await _load_ontology_rows(db, request.tags + step_tags)
    for step in request.steps:
        await _validate_pinned_service_step(db, step)
    return ontology_rows
```

**Pre-check:** Before touching the DB, the function verifies that every step's `ontology_tag` is included in the workflow's `tags` array. Tags are the workflow-level index (searchable via `GIN` index on `workflows.tags`); if a step uses a tag that isn't in the top-level tags array, the workflow would be unsearchable by that tag.

**Query 1 â€” `_load_ontology_rows()`:** Loads all tags from `ontology_tags` in one query:

```sql
SELECT tag, domain, sensitivity_tier
FROM ontology_tags
WHERE tag = ANY(CAST(:tags AS TEXT[]))
```

Any tag not found in the result set is an unknown tag â€” raises 422.

**Query 2 â€” `_validate_pinned_service_step()`:** For each step with a non-null `service_id`, two sub-queries verify:
1. The service is active and capable of the step's `ontology_tag` (via `service_capabilities`)
2. The `context_fields_required` are declared in the service's manifest (via `service_context_requirements`)

```sql
-- Sub-query 1: capability check
SELECT sc.service_id
FROM service_capabilities sc
JOIN services s ON s.id = sc.service_id
WHERE sc.service_id = :service_id
  AND sc.ontology_tag = :ontology_tag
  AND s.is_active = true
  AND s.is_banned = false
LIMIT 1

-- Sub-query 2: context field declaration check
SELECT field_name
FROM service_context_requirements
WHERE service_id = :service_id
  AND field_name = ANY(CAST(:fields AS TEXT[]))
```

If any required field is missing from the service's declared context, the submission is rejected with a specific error message naming the missing fields.

---

## `_validation_domain()` â€” Rule 10

```python
# api/services/workflow_registry.py:325â€“334
def _validation_domain(
    request: WorkflowCreateRequest,
    ontology_rows: dict[str, Mapping[str, Any]],
) -> str:
    for step in request.steps:
        row = ontology_rows[step.ontology_tag]
        if int(row["sensitivity_tier"]) >= 3:
            return str(row["domain"])
    return request.ontology_domain
```

This function determines which domain validator is required for the workflow. The logic is:

1. Walk every step
2. If any step's ontology tag has `sensitivity_tier >= 3` (health records, financial data, SSN etc.), return that tag's domain â€” requiring a domain-specific validator (e.g., a HEALTH validator for a health workflow)
3. If no high-sensitivity step exists, return the workflow's declared `ontology_domain` (general TRAVEL validator for a travel workflow)

**Why it matters:** A general-purpose validator can approve a hotel booking workflow. But a workflow that includes a step touching health records (`sensitivity_tier >= 3`) must be reviewed by someone with domain expertise in health compliance â€” not a general validator. The assignment is made automatically at submission time, not left to the admin.

---

## `_spec_payload()` â€” The Stored Spec

When a workflow is created, `_spec_payload()` builds the JSONB value stored in `workflows.spec`:

```python
# api/services/workflow_registry.py:337â€“350
def _spec_payload(request: WorkflowCreateRequest, workflow_id: UUID) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    payload["workflow_id"] = str(workflow_id)
    payload["quality"] = {
        "quality_score": 0.0,
        "execution_count": 0,
        "success_rate": 0.0,
        "validation_status": "draft",
        "validated_by_domain": None,
    }
    payload["accountability"]["published_at"] = None
    payload["accountability"]["spec_hash"] = None
    return payload
```

The stored spec includes two sections absent from the submission:

- **`quality`**: all zeros at creation time; updated by `compute_workflow_quality_score()` after each execution
- **`accountability.published_at`** and **`accountability.spec_hash`**: both null at creation; set at publication time when the validator approves

This means the JSONB spec returned from `GET /workflows/{id}` always contains the current quality metrics and the immutability hash â€” it is a complete machine-readable document that agent platforms can consume without making additional API calls.

---

## The Pydantic Model Structure

```python
# api/models/workflow.py
class WorkflowStepInput(BaseModel):
    step_number: int
    name: str
    ontology_tag: str
    service_id: UUID | None = None
    is_required: bool = True
    fallback_step_number: int | None = None
    context_fields_required: list[str] = []
    context_fields_optional: list[str] = []
    min_trust_tier: int = Field(ge=1, le=4)
    min_trust_score: float = Field(ge=0.0, le=100.0)
    timeout_seconds: int = 30

class WorkflowCreateRequest(BaseModel):
    spec_version: Literal["1.0"]
    name: str
    slug: str
    description: str
    ontology_domain: str
    tags: list[str]
    steps: list[WorkflowStepInput] = Field(min_length=1, max_length=20)
    accountability: AccountabilityBlock

    @model_validator(mode="after")
    def _validate_steps(self) -> "WorkflowCreateRequest":
        # Rule 3: sequential step_numbers starting at 1
        # Rule 5: no backward fallback jumps
        # Rule 9: no duplicate ontology_tag (unless service_id differs)
        ...
```

The `@model_validator` fires before any DB access. It catches structural errors (duplicate steps, bad step numbering, circular fallbacks) without requiring a database round-trip.

---

## Exercise 1 â€” Submit a Valid Workflow

```bash
curl -s -X POST http://localhost:8000/v1/workflows \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "spec_version": "1.0",
    "name": "Travel Search and Book",
    "slug": "travel-search-book",
    "description": "Search and book a flight and hotel",
    "ontology_domain": "TRAVEL",
    "tags": ["travel.air.search", "travel.lodging.search"],
    "steps": [
      {
        "step_number": 1,
        "name": "Search flights",
        "ontology_tag": "travel.air.search",
        "is_required": true,
        "context_fields_required": ["user.name"],
        "min_trust_tier": 1,
        "min_trust_score": 30.0,
        "timeout_seconds": 15
      },
      {
        "step_number": 2,
        "name": "Search hotels",
        "ontology_tag": "travel.lodging.search",
        "is_required": false,
        "context_fields_required": [],
        "min_trust_tier": 1,
        "min_trust_score": 30.0,
        "timeout_seconds": 15
      }
    ],
    "accountability": {
      "author_did": "did:key:z6MkTestContextAgent"
    }
  }' | python -m json.tool
```

**Expected:** 201 with `status="draft"`, `validation_id`, `estimated_review_hours: 48`.

---

## Exercise 2 â€” Trigger a Validation Rule Failure

Test Rule 3 (non-sequential step numbers):

```bash
curl -s -X POST http://localhost:8000/v1/workflows \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "spec_version": "1.0",
    "name": "Bad Steps",
    "slug": "bad-steps",
    "description": "Non-sequential steps",
    "ontology_domain": "TRAVEL",
    "tags": ["travel.air.search"],
    "steps": [
      {"step_number": 1, "name": "Step 1", "ontology_tag": "travel.air.search",
       "context_fields_required": [], "min_trust_tier": 1, "min_trust_score": 0.0},
      {"step_number": 3, "name": "Step 3", "ontology_tag": "travel.air.search",
       "service_id": "00000000-0000-0000-0000-000000000001",
       "context_fields_required": [], "min_trust_tier": 1, "min_trust_score": 0.0}
    ],
    "accountability": {"author_did": "did:key:z6MkTestContextAgent"}
  }' | python -m json.tool
```

**Expected:** 422 with a Pydantic validation error noting non-sequential step numbers.

---

## Exercise 3 â€” Inspect the Stored Spec

After a successful submission, retrieve the workflow and inspect the `spec` field:

```bash
WORKFLOW_ID="<uuid-from-post>"
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID" \
  -H "X-API-Key: dev-local-only" | python -c "
import sys, json
data = json.load(sys.stdin)
print(json.dumps(data.get('spec', {}), indent=2))
"
```

**Expected:** Full spec including `quality` block (all zeros) and `accountability.spec_hash: null`.

---

## Best Practices

**Keep validation as early as possible.** Pydantic catches structural errors before any function call. `_validate_workflow_spec()` catches registry-data errors before any row is inserted. This ordering prevents partial state: a workflow is either fully valid or fully rejected â€” never half-inserted.

**Recommended (not implemented here):** A dry-run endpoint (`POST /workflows/validate-only`) that runs all ten rules and returns detailed errors without creating any database records. Authors could validate their spec during development without polluting the draft queue.

---

## Interview Q&A

**Q: Why does Layer 5 validate that step tags are included in the workflow's top-level tags array?**
A: The top-level `tags` array is indexed with a PostgreSQL GIN index (`workflows USING GIN(tags)`). List queries filter by tag using `'travel.air.book' = ANY(tags)`. If a step uses a tag not in the top-level array, the workflow is invisible to tag-filtered queries â€” agents searching for workflows that touch `travel.air.book` would not find it. The pre-check enforces index consistency.

**Q: Why does the validation domain change when any step has sensitivity_tier >= 3?**
A: High-sensitivity steps (health records, financial data, SSN) require a validator who understands the compliance requirements in that domain â€” not a general-purpose reviewer. The automatic domain assignment ensures the workflow lands in the right queue without requiring admins to manually inspect every submission for sensitivity markers.

**Q: Why is `spec_hash` null at submission time and only set at publication?**
A: The spec hash is the immutability proof. It is meaningful only after the human validator has reviewed and approved the specific spec. Setting it at submission would mean hashing a spec that might be revised before publication â€” the hash would be misleading. Locking the hash at approval time ensures the hash corresponds exactly to the reviewed and approved spec.

---

## Key Takeaways

- Workflow spec is JSONB stored in `workflows.spec` â€” consumed as-is by agent platforms
- Ten validation rules: seven enforced by Pydantic, three requiring DB access
- `_validate_workflow_spec()` runs two DB queries: ontology tag existence + pinned service capability check
- `_validation_domain()` auto-assigns domain-specific validators for high-sensitivity steps
- `_spec_payload()` adds `quality` and `accountability` blocks to the stored spec at creation time
- `spec_hash` is null until a human validator approves the workflow

---

## Next Lesson

**Lesson 43 â€” The Submissions Window: Workflow CRUD** traces the full `create_workflow()`, `list_workflows()`, and `get_workflow()` functions â€” including the three Redis caches (detail, slug, list), the cache invalidation pattern, and the rate limit that protects the workflow query surface.
