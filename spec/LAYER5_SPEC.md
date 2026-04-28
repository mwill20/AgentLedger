# AgentLedger - Layer 5 Implementation Spec
## Orchestration & Taste: Workflow Registry and Quality Signals

**Version:** 0.1
**Status:** Ready for Implementation
**Author:** Michael Williams
**Last Updated:** April 2026
**Depends on:** Layer 1 (complete), Layer 2 (complete), Layer 3 (complete), Layer 4 (complete)

---

## Purpose of This Document

This is the implementation specification for Layer 5 of AgentLedger - Orchestration &
Taste. It is written for Claude Code or any developer building the system from scratch.
Every design decision is documented. Nothing should require guessing.

Do not build anything not described here without updating this spec first.

---

## What Layer 5 Builds

Layer 5 adds the workflow registry and quality signal layer that sits above individual
service interactions and below liability. It provides three capabilities:

1. **Workflow Registry** - a machine-readable catalog of validated multi-step
   orchestration patterns that chain Layer 1 services together
2. **Human Validation Queue** - a review workflow where domain experts approve
   workflow definitions before they reach published status
3. **Outcome Quality Feedback Loop** - execution outcome reporting that feeds
   aggregate quality scores back into workflow ranking

Layer 5 does NOT include:
- Workflow execution - agent platforms execute; AgentLedger validates and serves specs
- Payment processing within workflows - Layer 6
- Insurance underwriting on workflow outcomes - Layer 6
- Cross-registry workflow federation - future

---

## The Core Problem Layer 5 Solves

Layers 1-4 solve trust and context at the individual service interaction level. An agent
can find a service (Layer 1), verify its identity (Layer 2), check its trust score (Layer
3), and disclose only the required context (Layer 4). But each of these is a single-hop
interaction.

Real agent tasks are multi-step. Booking business travel requires: search flights ->
book flight -> search hotels -> book hotel -> arrange ground transport -> add to calendar.
Each step involves a different service. Each requires its own trust check. Each requires
its own context disclosure.

Without Layer 5, every agent platform must hardcode these orchestration patterns
independently, with no shared quality signals, no human validation, and no accountability
chain spanning the full workflow. The result is NxN orchestration fragmentation - the
same problem that Layer 1 solves for service discovery.

Layer 5 solves it at the workflow level: a published, human-validated workflow spec that
any agent platform can execute, with a defined accountability chain from first step to
last.

---

## Critical Design Principle: Layer 5 Does Not Execute

Layer 5 is a registry and validation layer, not a runtime. It publishes workflow specs.
Agent platforms execute them.

This distinction is architectural and strategic:
- **Architectural**: execution requires real-time state management, retry logic, and
  rollback handling that belong in orchestration frameworks, not trust infrastructure
- **Strategic**: becoming an execution layer makes AgentLedger a competitor to agent
  platforms rather than infrastructure they depend on

The analogy is exact: DNS publishes records. It does not route packets. Layer 5 publishes
workflow specs. It does not execute them.

---

## Technology Stack

All stack decisions are final for v0.1. Do not substitute without updating this spec.
Layer 5 adds to the existing stack - no replacements.

| Component | Technology | Reason |
|---|---|---|
| API Framework | FastAPI (Python 3.11+) | Existing - no change |
| Database | PostgreSQL 15+ | Existing - new tables added |
| Cache | Redis 7+ | Existing - workflow cache + execution count cache |
| Testing | pytest + httpx | Existing |

No new dependencies required for v0.1.

---

## Repository Structure - New Files Only

Add these files to the existing AgentLedger structure:

```
AgentLedger/
|-- api/
|   |-- routers/
|   |   `-- workflows.py              # All Layer 5 endpoints
|   |-- models/
|   |   `-- workflow.py               # Pydantic models for Layer 5
|   `-- services/
|       |-- workflow_registry.py      # Workflow CRUD + publication logic
|       |-- workflow_validator.py     # Human validation queue management
|       |-- workflow_ranker.py        # Composite quality score computation
|       |-- workflow_executor.py      # Outcome feedback write path
|       `-- workflow_context.py       # Workflow-scoped profile + bundle logic
|-- db/
|   `-- migrations/
|       `-- versions/
|           `-- 006_layer5_workflows.py
`-- tests/
    `-- test_api/
        |-- test_workflow_registry.py
        |-- test_workflow_validator.py
        |-- test_workflow_ranker.py
        `-- test_workflow_executor.py
```

Do not add any other files.

---

## Database Schema - New Tables Only

Add to the existing schema. Do not modify any Layer 1-4 tables.

```sql
-- Workflow definitions
-- A workflow is a validated, ordered sequence of ontology-tagged service steps
CREATE TABLE workflows (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,           -- e.g. "business-travel-booking"
    description TEXT NOT NULL,
    ontology_domain TEXT NOT NULL,       -- primary domain: TRAVEL, FINANCE, HEALTH, etc.
    tags TEXT[] NOT NULL DEFAULT '{}',   -- ontology tags touched by this workflow
    spec JSONB NOT NULL,                 -- full workflow spec (see Workflow Spec Format)
    spec_version TEXT NOT NULL DEFAULT '1.0',
    spec_hash TEXT,                      -- set at validation/publication time for immutability
    author_did TEXT NOT NULL REFERENCES agent_identities(did),
    status TEXT NOT NULL DEFAULT 'draft',
    -- 'draft'      = submitted, not yet reviewed
    -- 'in_review'  = assigned to validator
    -- 'published'  = validated and active
    -- 'deprecated' = replaced by newer version
    -- 'rejected'   = failed validation
    quality_score FLOAT NOT NULL DEFAULT 0.0,   -- 0.0-100.0, composite
    execution_count BIGINT NOT NULL DEFAULT 0,
    success_count BIGINT NOT NULL DEFAULT 0,
    failure_count BIGINT NOT NULL DEFAULT 0,
    parent_workflow_id UUID REFERENCES workflows(id),  -- for versioned updates
    published_at TIMESTAMPTZ,
    deprecated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Individual steps within a workflow (ordered)
CREATE TABLE workflow_steps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id UUID NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,         -- 1-based, defines execution order
    name TEXT NOT NULL,                   -- human-readable step name
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    service_id UUID REFERENCES services(id),
    -- NULL = any service capable of this ontology_tag
    -- non-NULL = pinned to a specific verified service
    is_required BOOLEAN NOT NULL DEFAULT true,
    -- false = optional step; workflow can succeed without it
    fallback_step_number INTEGER,
    -- if this step fails, jump to this step number instead of aborting
    context_fields_required TEXT[] NOT NULL DEFAULT '{}',
    context_fields_optional TEXT[] NOT NULL DEFAULT '{}',
    min_trust_tier INTEGER NOT NULL DEFAULT 2,
    min_trust_score FLOAT NOT NULL DEFAULT 50.0,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workflow_id, step_number)
);

-- Human validation records
CREATE TABLE workflow_validations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id UUID NOT NULL REFERENCES workflows(id),
    validator_did TEXT NOT NULL,          -- domain expert's agent DID
    validator_domain TEXT NOT NULL,       -- e.g. "HEALTH", "FINANCE"
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision TEXT,
    -- NULL = pending
    -- 'approved' = workflow passes quality and safety bar
    -- 'rejected' = workflow fails (see rejection_reason)
    -- 'revision_requested' = returned to author with notes
    decision_at TIMESTAMPTZ,
    rejection_reason TEXT,
    revision_notes TEXT,
    checklist JSONB NOT NULL DEFAULT '{}',
    -- structured validation checklist results (see Validation Checklist)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Execution outcome reports
-- Written by agent platforms after running a workflow
CREATE TABLE workflow_executions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id UUID NOT NULL REFERENCES workflows(id),
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    context_bundle_id UUID,              -- references workflow_context_bundles if used
    outcome TEXT NOT NULL,
    -- 'success'  = all required steps completed successfully
    -- 'partial'  = some required steps failed, workflow completed with degraded result
    -- 'failure'  = workflow aborted before completion
    steps_completed INTEGER NOT NULL DEFAULT 0,
    steps_total INTEGER NOT NULL,
    failure_step_number INTEGER,         -- which step caused abort, if any
    failure_reason TEXT,
    duration_ms INTEGER,
    reported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified BOOLEAN NOT NULL DEFAULT false,
    -- true = cross-checked against Layer 4 context_disclosures audit trail
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Workflow-level context bundles
-- Groups multiple single-service context disclosures under one user approval
CREATE TABLE workflow_context_bundles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id UUID NOT NULL REFERENCES workflows(id),
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    scoped_profile_id UUID REFERENCES context_profiles(id),
    -- workflow-scoped profile that overrides agent's default for this execution
    status TEXT NOT NULL DEFAULT 'pending',
    -- 'pending'   = awaiting user approval
    -- 'approved'  = user approved all disclosed fields
    -- 'rejected'  = user rejected; workflow cannot proceed
    -- 'consumed'  = bundle used in a completed execution
    approved_fields JSONB NOT NULL DEFAULT '{}',
    -- { "step_1": ["user.name", "user.email"], "step_2": ["user.name"] }
    user_approved_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,     -- bundle TTL: 30 minutes
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Workflow-scoped context profiles
-- Overrides agent defaults for the duration of a specific workflow execution
CREATE TABLE workflow_scoped_profiles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_id UUID NOT NULL REFERENCES workflows(id),
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    base_profile_id UUID REFERENCES context_profiles(id),
    -- the agent's default profile this scope extends
    overrides JSONB NOT NULL DEFAULT '{}',
    -- field-level overrides: { "user.dob": "permit", "user.ssn": "deny" }
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(workflow_id, agent_did)
);

-- Indexes
CREATE INDEX workflows_status ON workflows(status) WHERE status = 'published';
CREATE INDEX workflows_domain ON workflows(ontology_domain);
CREATE INDEX workflows_quality ON workflows(quality_score DESC) WHERE status = 'published';
CREATE INDEX workflows_tags ON workflows USING GIN(tags);
CREATE INDEX workflow_steps_workflow ON workflow_steps(workflow_id, step_number);
CREATE INDEX workflow_validations_pending ON workflow_validations(workflow_id)
    WHERE decision IS NULL;
CREATE INDEX workflow_executions_workflow ON workflow_executions(workflow_id, reported_at DESC);
CREATE INDEX workflow_context_bundles_agent ON workflow_context_bundles(agent_did, status);
```

---

## Workflow Spec Format

Every workflow is stored as a JSONB `spec` field in the `workflows` table and returned
via the API. This is the machine-readable format agent platforms consume.

```json
{
  "spec_version": "1.0",
  "workflow_id": "uuid",
  "name": "Business Travel Booking",
  "slug": "business-travel-booking",
  "description": "Book a complete business trip: flights, hotel, and ground transport",
  "ontology_domain": "TRAVEL",
  "tags": ["travel.air.book", "travel.lodging.book", "travel.ground.rideshare"],
  "steps": [
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
    },
    {
      "step_number": 2,
      "name": "Book hotel near destination",
      "ontology_tag": "travel.lodging.book",
      "service_id": null,
      "is_required": true,
      "fallback_step_number": null,
      "context_fields_required": ["user.name", "user.email"],
      "context_fields_optional": [],
      "min_trust_tier": 2,
      "min_trust_score": 60.0,
      "timeout_seconds": 30
    },
    {
      "step_number": 3,
      "name": "Arrange airport transfer",
      "ontology_tag": "travel.ground.rideshare",
      "service_id": null,
      "is_required": false,
      "fallback_step_number": null,
      "context_fields_required": ["user.name"],
      "context_fields_optional": [],
      "min_trust_tier": 2,
      "min_trust_score": 50.0,
      "timeout_seconds": 15
    }
  ],
  "context_bundle": {
    "all_required_fields": ["user.name", "user.email"],
    "all_optional_fields": ["user.frequent_flyer_id"],
    "single_approval": true
  },
  "quality": {
    "quality_score": 87.4,
    "execution_count": 1240,
    "success_rate": 0.94,
    "validation_status": "published",
    "validated_by_domain": "TRAVEL"
  },
  "accountability": {
    "author_did": "did:key:z6Mk...",
    "published_at": "2026-04-28T00:00:00Z",
    "spec_hash": "sha256:..."
  }
}
```

### Validation Rules for Submitted Specs

1. `spec_version` must equal `"1.0"`
2. `steps` must have at least 1 entry and no more than 20
3. `step_number` values must be sequential starting at 1, no gaps
4. All `ontology_tag` values must exist in the `ontology_tags` table
5. `fallback_step_number` must reference a step with a higher number (no backward
   jumps that create loops)
6. `context_fields_required` in any step must be declared in the target service's
   manifest context block (if `service_id` is pinned) - otherwise advisory only
7. `min_trust_tier` must be between 1 and 4
8. `min_trust_score` must be between 0.0 and 100.0
9. No step may reference the same `ontology_tag` twice unless `service_id` values differ
10. If any step touches an ontology tag with `sensitivity_tier >= 3`, the workflow
    requires HEALTH, FINANCE, or the relevant domain validator to approve - not a
    general validator

---

## API Specification - New Endpoints Only

Base URL: `https://api.agentledger.io/v1` (local: `http://localhost:8000/v1`)
All endpoints require `X-API-Key` or Bearer VC token. Same auth as Layers 1-4.

---

### POST /workflows
Submit a workflow for review.

**Request body:** Full workflow spec (see Workflow Spec Format above)

**Processing:**
1. Validate spec structure per validation rules 1-10
2. Create `workflows` record with `status='draft'`
3. Create `workflow_steps` records from spec
4. Auto-assign to validation queue (creates `workflow_validations` record)
5. Return draft workflow with validation assignment

**Response 201:**
```json
{
  "workflow_id": "uuid",
  "slug": "business-travel-booking",
  "status": "draft",
  "validation_id": "uuid",
  "estimated_review_hours": 48
}
```
**Response 422:** Spec validation error with field-level detail.

---

### GET /workflows
List published workflows with optional filters.

**Query params:**
- `domain` - filter by ontology_domain (TRAVEL, FINANCE, HEALTH, COMMERCE, PRODUCTIVITY)
- `tags` - comma-separated ontology tags; returns workflows touching all listed tags
- `status` - defaults to `published`; admin can pass `draft` or `in_review`
- `quality_min` - minimum quality_score (0.0-100.0)
- `limit`, `offset`

**Response 200:** Paginated list of workflow summaries with quality scores and step counts.

---

### GET /workflows/{workflow_id}
Full workflow detail including complete spec, all steps, and quality metrics.

**Response 200:** Full workflow object as defined in Workflow Spec Format.
**Response 404:** Workflow not found or not published (non-admin).

---

### GET /workflows/slug/{slug}
Same as `GET /workflows/{workflow_id}` but addressed by slug.

---

### POST /workflows/{workflow_id}/validate
Admin endpoint. Assign a workflow to a validator.

**Request body:**
```json
{
  "validator_did": "did:key:z6Mk...",
  "validator_domain": "TRAVEL"
}
```
**Response 200:** Validation assignment record.
**Response 409:** Workflow already assigned to an active validation.

---

### PUT /workflows/{workflow_id}/validation
Validator decision endpoint.

**Request body:**
```json
{
  "validator_did": "did:key:z6Mk...",
  "decision": "approved",
  "checklist": {
    "steps_achievable": true,
    "context_minimal": true,
    "trust_thresholds_appropriate": true,
    "no_sensitive_tag_without_domain_review": true,
    "fallback_logic_sound": true
  },
  "rejection_reason": null,
  "revision_notes": null
}
```

**Processing for `decision='approved'`:**
1. Set `workflow_validations.decision = 'approved'`
2. Set `workflows.status = 'published'`, `workflows.published_at = NOW()`
3. Compute initial `quality_score` (see Quality Score Computation)
4. Invalidate workflow list cache

**Processing for `decision='rejected'`:**
1. Set `workflow_validations.decision = 'rejected'`
2. Set `workflows.status = 'rejected'`

**Processing for `decision='revision_requested'`:**
1. Set `workflow_validations.decision = 'revision_requested'`
2. Set `workflows.status = 'draft'` (returns to author for edits)

**Response 200:** Updated workflow with new status.
**Response 403:** Requesting DID does not match assigned validator.

---

### POST /workflows/{workflow_id}/executions
Report an execution outcome. Called by agent platforms after completing (or failing)
a workflow run.

**Request body:**
```json
{
  "agent_did": "did:key:z6Mk...",
  "context_bundle_id": "uuid",
  "outcome": "success",
  "steps_completed": 3,
  "steps_total": 3,
  "failure_step_number": null,
  "failure_reason": null,
  "duration_ms": 4200
}
```

**Processing:**
1. Create `workflow_executions` record with `verified=false`
2. Increment `workflows.execution_count`
3. If `outcome='success'`: increment `workflows.success_count`
4. If `outcome='failure'`: increment `workflows.failure_count`
5. Trigger async background task: cross-check against Layer 4 `context_disclosures`
   audit trail - if context_bundle_id is present, verify that disclosures exist
   for each step in the reported timeframe. Set `verified=true` if confirmed.
6. Recompute `quality_score` (see Quality Score Computation)

**Response 201:** `{ "execution_id": "uuid", "verified": false, "quality_score": 87.4 }`

**Anti-gaming note:** `verified=false` executions contribute at 30% weight to quality
score. `verified=true` executions contribute at full weight. Unverifiable outcomes
cannot move the quality score above 70.0 regardless of reported outcome.

---

### POST /workflows/context/bundle
Create a workflow-level context bundle. Groups all context disclosures for a workflow
execution under a single user approval interaction.

**Request body:**
```json
{
  "workflow_id": "uuid",
  "agent_did": "did:key:z6Mk...",
  "scoped_profile_overrides": {
    "user.frequent_flyer_id": "permit"
  }
}
```

**Processing:**
1. Load workflow steps and their context requirements
2. Aggregate all required and optional fields across steps
3. Apply agent's context profile + any scoped overrides
4. Classify each field: permitted / withheld / committed (reuses Layer 4 matcher logic)
5. Create `workflow_context_bundles` record with `status='pending'`
6. Return bundle with full field breakdown for user approval UI

**Response 201:**
```json
{
  "bundle_id": "uuid",
  "workflow_id": "uuid",
  "status": "pending",
  "by_step": {
    "step_1": {
      "permitted": ["user.name", "user.email"],
      "withheld": [],
      "committed": ["user.frequent_flyer_id"]
    },
    "step_2": {
      "permitted": ["user.name", "user.email"],
      "withheld": [],
      "committed": []
    }
  },
  "all_permitted": ["user.name", "user.email"],
  "all_committed": ["user.frequent_flyer_id"],
  "all_withheld": [],
  "expires_at": "ISO 8601"
}
```

---

### POST /workflows/context/bundle/{bundle_id}/approve
User approves the context bundle. After approval, the agent may proceed with workflow
execution and use the bundle_id when reporting outcomes.

**Request body:** `{ "agent_did": "did:key:z6Mk..." }`
**Response 200:** `{ "bundle_id": "uuid", "status": "approved", "approved_at": "ISO 8601" }`
**Response 410:** Bundle expired.

---

### GET /workflows/{workflow_id}/rank
Returns a ranked list of specific services that can fulfill each step, using Layer 1
ranking signals filtered by the step's min_trust_tier and min_trust_score.

**Query params:** `geo`, `pricing_model`

**Response 200:**
```json
{
  "workflow_id": "uuid",
  "ranked_steps": [
    {
      "step_number": 1,
      "ontology_tag": "travel.air.book",
      "candidates": [
        {
          "service_id": "uuid",
          "name": "FlightBookerPro",
          "trust_score": 91.2,
          "trust_tier": 3,
          "rank_score": 0.88,
          "can_disclose": true
        }
      ]
    }
  ]
}
```

---

## Quality Score Computation

The workflow quality score is a composite of four signals. It is recomputed after every
execution outcome is reported and after validation status changes.

```python
def compute_workflow_quality_score(
    validation_score: float,    # 1.0 if published, 0.5 if draft, 0.0 if rejected
    success_rate: float,        # success_count / execution_count (0.0 if no executions)
    verification_rate: float,   # verified executions / total executions
    avg_step_trust: float       # mean trust_score of pinned services across steps
) -> float:

    # Execution volume scaling: low volume = higher uncertainty discount
    volume = execution_count
    volume_factor = min(1.0, volume / 100)  # scales 0->1 as executions 0->100

    raw = (
        validation_score  * 0.35 +
        success_rate      * 0.30 * volume_factor +
        verification_rate * 0.20 +
        avg_step_trust    * 0.15
    )

    # Unverifiable cap: if verification_rate < 0.5, cap at 70.0
    if verification_rate < 0.5:
        raw = min(raw, 0.70)

    return round(raw * 100, 2)  # 0.0-100.0
```

**Initial score at publication (zero executions):**
- `validation_score = 1.0`
- `success_rate = 0.0` (no executions yet)
- `verification_rate = 0.0`
- `avg_step_trust` = computed from Layer 3 trust scores of pinned services (or 0.5 if unpinned)
- `volume_factor = 0.0`

Newly published workflows start at a quality score derived entirely from validation
status and pinned service trust scores. Execution history builds the score over time.

---

## Validation Checklist

When a validator reviews a workflow, they complete this structured checklist. All items
must pass for `decision='approved'`.

| # | Check | Description |
|---|-------|-------------|
| 1 | `steps_achievable` | Each step references a real, currently-published ontology tag with at least one capable service in the registry |
| 2 | `context_minimal` | No step requests context fields beyond what is reasonably necessary for that ontology tag |
| 3 | `trust_thresholds_appropriate` | `min_trust_tier` and `min_trust_score` per step are proportional to the sensitivity of the action |
| 4 | `no_sensitive_tag_without_domain_review` | Any step with sensitivity_tier >= 3 was reviewed by a domain-appropriate validator |
| 5 | `fallback_logic_sound` | Any `fallback_step_number` references lead to safe degraded outcomes, not service escalations |

Validators may pass all five and still choose `revision_requested` if they have broader
concerns not captured by the checklist.

---

## Layer 4 Integration Points Activated by Layer 5

| # | Integration Point | Layer 4 State | Layer 5 Change |
|---|---|---|---|
| 1 | Workflow context bundles | `context_disclosures` (single-service) | Layer 5 groups multiple disclosures into `workflow_context_bundles` with single user approval; bundle_id is passed to Layer 4 match calls per step |
| 2 | Context fit ranking signal | `can_disclose: bool` returned by matcher | Layer 5 `/workflows/{id}/rank` uses `can_disclose` to filter step candidates - services the agent cannot disclose to are excluded from ranked results |
| 3 | Mismatch -> workflow abort | `context_mismatch_events` severity field | Layer 5 execution reporting checks for `severity='critical'` mismatches on any step; a critical mismatch in the audit trail causes the execution to be marked as `verified=false` and flags a review |
| 4 | Profile inheritance | `context_profiles` | `workflow_scoped_profiles` extends the agent's base profile with step-specific field overrides; the scoped profile is passed to Layer 4 match calls per step instead of the default profile |
| 5 | Compliance bundle | `GET /context/compliance/export` | Layer 6 will combine context compliance export with workflow execution records into a single regulatory package; `workflow_context_bundles` and `workflow_executions.context_bundle_id` provide the workflow join without modifying Layer 4 tables |

---

## Threat Model - Layer 5 Additions

Layers 1-4 defined 14 threats. Layer 5 adds 4 more:

| # | Threat | Attack | Severity | Mitigation |
|---|--------|--------|----------|------------|
| 15 | Workflow Laundering | Submit legitimate workflow, get validated, then update spec to route steps to malicious services | Critical | Spec hash stored at validation time; any spec change after publication creates a new workflow requiring fresh validation - published workflows are immutable |
| 16 | Step Poisoning | Publish a workflow with 9 legitimate steps + 1 malicious step that exfiltrates context | Critical | Validation checklist item 2 (context_minimal) + sensitivity_tier gating; domain validators review each step's context requirements independently |
| 17 | Quality Gaming | Agent platform reports fake success outcomes to inflate a workflow's quality score | High | Cross-verification against Layer 4 context_disclosures audit trail; unverified outcomes capped at 70.0 quality score; verification_rate < 0.5 triggers review |
| 18 | Context Bundle Abuse | Obtain bundle approval for a legitimate workflow, reuse bundle_id to authorize context disclosure for a different workflow | High | Bundle tied to specific workflow_id and agent_did; consumed bundles cannot be reused; 30-minute TTL; bundle status transitions are one-way |

---

## Layer 3 Integration Points Activated by Layer 5

| # | Integration Point | Layer 3 State | Layer 5 Change |
|---|---|---|---|
| 1 | Trust score per step | Stored in `services.trust_score` | Layer 5 `/workflows/{id}/rank` uses trust_score + trust_tier to filter and rank service candidates per step |
| 2 | Revocation propagation | Trust revocation marks service inactive | A revocation of a pinned service (`service_id` non-null in `workflow_steps`) triggers a workflow status check - if a required step's pinned service is revoked, the workflow is auto-flagged for re-validation |
| 3 | avg_step_trust | Used in quality score | Quality score pulls current Layer 3 trust scores for pinned services at computation time |

---

## Build Order

### Phase 1 - Workflow Registry CRUD
- Migration 006 with all six new tables
- Pydantic models in `api/models/workflow.py`
- `api/services/workflow_registry.py` - create, read, list workflows + step records
- `POST /workflows`, `GET /workflows`, `GET /workflows/{id}`, `GET /workflows/slug/{slug}`
- Spec validation rules 1-10

**Done when:** POST /workflows with a valid two-step TRAVEL workflow returns 201 with
`status='draft'`, and GET /workflows/slug/{slug} retrieves it with full step detail.

---

### Phase 2 - Human Validation Queue
- `api/services/workflow_validator.py` - validation assignment and decision logic
- `POST /workflows/{id}/validate` - assign to validator
- `PUT /workflows/{id}/validation` - record decision, transition workflow status
- Spec hash computed at publication time - immutability enforcement

**Done when:** A workflow transitions draft -> in_review -> published via the two
endpoints, and a second PUT attempt with a modified spec is rejected.

---

### Phase 3 - Workflow Ranking Engine
- `api/services/workflow_ranker.py` - quality score computation
- `GET /workflows/{id}/rank` - per-step service candidates ranked via Layer 1 + Layer 3
- Quality score recomputed on each execution outcome write
- Redis cache for ranked results (TTL: 60s)

**Done when:** GET /workflows/{id}/rank returns service candidates for each step filtered
by min_trust_tier and sorted by rank_score, and quality_score updates after an execution
is reported.

---

### Phase 4 - Context Bundle Integration
- `api/services/workflow_context.py` - bundle creation, scoped profile logic
- `POST /workflows/context/bundle` - aggregate context across steps + classify fields
- `POST /workflows/context/bundle/{id}/approve` - user approval
- `workflow_scoped_profiles` CRUD wired to Layer 4 matcher

**Done when:** A bundle is created for a three-step workflow, classified fields show
correctly across steps, user approval transitions status to 'approved', and a Layer 4
match call using the scoped profile returns different results than the default profile.

---

### Phase 5 - Outcome Feedback Loop
- `api/services/workflow_executor.py` - execution outcome write path + verification logic
- `POST /workflows/{id}/executions` - outcome reporting endpoint
- Async background verification against Layer 4 `context_disclosures` audit trail
- Quality score recomputation after each execution

**Done when:** A reported success outcome increments execution_count and success_count,
the async verification task runs and sets verified=true when audit trail evidence exists,
and quality_score updates correctly after verification.

---

### Phase 6 - Hardening
- Redis cache for workflow list and individual workflow reads (TTL: 60s)
- Cache invalidation on publication and quality score update
- Rate limiting: 200 workflow queries per API key per minute
- Revocation-triggered workflow re-validation check (Layer 3 integration)
- 80%+ test coverage for all new modules
- Load test: `GET /workflows` and `GET /workflows/{id}/rank` at 100 concurrent,
  p95 < 200ms

---

## Acceptance Criteria (10 gates)

```
[ ] POST /workflows returns 201 for valid spec; invalid spec returns 422 with field errors
[ ] Workflow transitions draft -> in_review -> published via validation endpoints
[ ] Published workflow spec is immutable: PUT with modified spec creates new workflow
[ ] GET /workflows?domain=TRAVEL returns only published TRAVEL workflows sorted by quality_score
[ ] GET /workflows/{id}/rank returns per-step candidates filtered by min_trust_tier
[ ] POST /workflows/context/bundle aggregates context fields correctly across all steps
[ ] Scoped profile overrides apply to Layer 4 match calls for that workflow execution
[ ] POST /workflows/{id}/executions increments counters and triggers async verification
[ ] Unverified executions cannot push quality_score above 70.0
[ ] GET /workflows/{id}/rank p95 < 200ms @ 100 concurrent requests
```

---

## What Layer 5 Does NOT Include

- Workflow execution runtime - agent platforms execute, not AgentLedger
- Payment processing within workflows - Layer 6
- Insurance underwriting on workflow outcomes - Layer 6
- Cross-registry workflow federation - future
- Workflow marketplace monetization - Layer 6
- Full ZKP for context bundles - v0.2 (inherits Layer 4 deferral)

---

## Layer 6 Integration Points (for the next session)

| # | Integration Point | Where in Layer 5 | What Layer 6 Adds |
|---|---|---|---|
| 1 | Workflow execution records | `workflow_executions` | Layer 6 uses execution outcomes as evidence for liability attribution - who ran which workflow, which step failed, what service was responsible |
| 2 | Context bundle audit trail | `workflow_context_bundles` + Layer 4 `context_disclosures` | Layer 6 combines into single regulatory package per workflow execution |
| 3 | Quality score as risk signal | `workflows.quality_score` | Layer 6 insurance underwriting uses quality_score to price workflow coverage - low quality = higher premium |
| 4 | Validator accountability | `workflow_validations.validator_did` | Layer 6 liability chain includes the validator who approved a workflow that subsequently caused harm |
| 5 | Pinned service revocation | `workflow_steps.service_id` + Layer 3 revocation | Layer 6 dispute resolution references the trust state of pinned services at execution time, not current state |

---

*This spec is the source of truth for Layer 5. Update it before changing any behavior
described here.*

