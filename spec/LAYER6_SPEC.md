# AgentLedger - Layer 6 Implementation Spec
## Liability: Attribution, Dispute Resolution, and Regulatory Compliance

**Version:** 0.1
**Status:** Ready for Implementation
**Author:** Michael Williams
**Last Updated:** April 2026
**Depends on:** Layer 1 (complete), Layer 2 (complete), Layer 3 (complete),
               Layer 4 (complete), Layer 5 (complete)

---

## Purpose of This Document

This is the implementation specification for Layer 6 of AgentLedger - Liability.
It is written for Claude Code or any developer building the system from scratch.
Every design decision is documented. Nothing should require guessing.

Do not build anything not described here without updating this spec first.

---

## What Layer 6 Builds

Layer 6 closes the accountability loop. Layers 1-5 collectively produce a rich audit
trail of every agent action: who discovered what service (L1), which agent identity
made the call (L2), what the trust state was at execution time (L3), what context was
disclosed (L4), and which workflow was followed and how it performed (L5).

Layer 6 does four things with that audit trail:

1. **Liability Snapshots** - point-in-time captures of trust state at the moment of
   each workflow execution, preserved independently of how trust scores change afterward
2. **Dispute Protocol** - a structured claims process: file, gather evidence, determine
   attribution, resolve
3. **Liability Attribution Engine** - given a dispute and its evidence, compute a
   principled attribution of responsibility across the four actors in every agent
   transaction: the agent, the service, the workflow author, and the validator
4. **Regulatory Compliance Export** - EU AI Act, HIPAA, and SEC-ready audit packages
   combining evidence from all five prior layers into jurisdiction-specific formats

Layer 6 does NOT include:
- Actual insurance underwriting or premium collection - requires licensed insurers
- Smart contract escrow execution - requires Layer 3 blockchain to be deployed
- Financial payment processing for claim settlements - out of scope for v0.1
- Governance org structure for AgentLedger itself - not a code problem
- Legal determinations - Layer 6 produces attribution evidence, not legal rulings

---

## The Critical Design Principle: Evidence, Not Judgment

Layer 6 produces attribution weights and evidence packages. It does not make legal
determinations. The output of the Liability Attribution Engine is a structured record
stating "based on available evidence, the agent bears 45% of attribution, the service
bears 40%, the workflow author bears 10%, and the validator bears 5%." This is evidence
for human decision-makers - insurers, lawyers, regulators - not a final ruling.

This distinction is architectural and legal. AgentLedger is infrastructure, not an
adjudicator. The moment Layer 6 starts issuing binding determinations, it becomes a
regulated financial or legal entity. Evidence packages keep it infrastructure.

---

## The Liability Snapshot Problem

Trust scores change. A service with trust_score=91.2 today may have had trust_score=62.0
when the disputed transaction occurred three weeks ago. Liability attribution must
reference the trust state AT EXECUTION TIME, not the current state.

Layer 3 stores attestation events on-chain - those are immutable. But the computed
trust_score in the `services` table is a rolling aggregate that gets overwritten.
Layer 6 solves this by capturing a liability snapshot at the moment each workflow
execution is reported. The snapshot is append-only and never modified. It becomes the
evidentiary anchor for any future dispute involving that execution.

This is why liability snapshots are created in Phase 1 even though disputes may never
be filed. The window to capture accurate trust state closes when the next crawl cycle
runs and overwrites the services table. Snapshots must be captured immediately.

---

## Technology Stack

All stack decisions are final for v0.1. Do not substitute without updating this spec.
Layer 6 adds to the existing stack - no replacements.

| Component | Technology | Reason |
|---|---|---|
| API Framework | FastAPI (Python 3.11+) | Existing - no change |
| Database | PostgreSQL 15+ | Existing - new tables added |
| Cache | Redis 7+ | Existing - claim status cache |
| PDF Export | reportlab | Existing from Layer 4 - reused for compliance exports |
| Testing | pytest + httpx | Existing |

No new dependencies required for v0.1.

---

## Repository Structure - New Files Only

Add these files to the existing AgentLedger structure:

```
AgentLedger/
|-- api/
|   |-- routers/
|   |   `-- liability.py              # All Layer 6 endpoints
|   |-- models/
|   |   `-- liability.py              # Pydantic models for Layer 6
|   `-- services/
|       |-- liability_snapshot.py     # Trust state capture at execution time
|       |-- liability_claims.py       # Dispute filing and evidence gathering
|       |-- liability_attribution.py  # Attribution weight computation
|       `-- liability_compliance.py  # Regulatory export generation
|-- db/
|   `-- migrations/
|       `-- versions/
|           `-- 007_layer6_liability.py
|-- tests/
|   `-- test_api/
|       |-- test_liability_snapshot.py
|       |-- test_liability_claims.py
|       |-- test_liability_attribution.py
|       `-- test_liability_compliance.py
```

Do not add any other files.

---

## Database Schema - New Tables Only

Add to the existing schema. Do not modify any Layer 1-5 tables.

```sql
-- Point-in-time trust state snapshot captured at workflow execution time.
-- Created automatically when a workflow execution is reported (POST /workflows/{id}/executions).
-- Append-only. Never modified after creation.
CREATE TABLE liability_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    execution_id UUID NOT NULL UNIQUE REFERENCES workflow_executions(id),
    workflow_id UUID NOT NULL REFERENCES workflows(id),
    agent_did TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Workflow state at execution time
    workflow_quality_score FLOAT NOT NULL,
    workflow_author_did TEXT NOT NULL,
    workflow_validator_did TEXT,          -- NULL if workflow was never validated
    workflow_validation_checklist JSONB,  -- copy of checklist from workflow_validations

    -- Per-step service trust state at execution time
    -- One entry per workflow step: { step_number, service_id, service_name,
    --   trust_score, trust_tier, trust_score_source }
    step_trust_states JSONB NOT NULL DEFAULT '[]',

    -- Context disclosure summary at execution time
    -- { fields_disclosed, fields_withheld, fields_committed, mismatch_count }
    context_summary JSONB NOT NULL DEFAULT '{}',

    -- Whether any critical context mismatches occurred during this execution
    critical_mismatch_count INTEGER NOT NULL DEFAULT 0,

    -- Agent context profile in effect at execution time
    agent_profile_default_policy TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Dispute claims
CREATE TABLE liability_claims (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    execution_id UUID NOT NULL REFERENCES workflow_executions(id),
    snapshot_id UUID NOT NULL REFERENCES liability_snapshots(id),
    claimant_did TEXT NOT NULL,           -- agent DID of the party filing the claim
    claim_type TEXT NOT NULL,
    -- 'service_failure'      = service did not fulfill its declared capability
    -- 'data_misuse'          = service mishandled disclosed context
    -- 'wrong_outcome'        = agent completed workflow but result was incorrect
    -- 'unauthorized_action'  = agent acted outside its declared scope
    -- 'workflow_design_flaw' = workflow spec caused the harm
    description TEXT NOT NULL,            -- human-readable description of the harm
    harm_value_usd FLOAT,                 -- estimated harm in USD, optional
    status TEXT NOT NULL DEFAULT 'filed',
    -- 'filed'            = claim submitted, evidence gathering not started
    -- 'evidence_gathered' = all evidence sources queried and attached
    -- 'under_review'     = human reviewer assigned
    -- 'determined'       = attribution weights computed and recorded
    -- 'resolved'         = claim closed with resolution note
    -- 'appealed'         = determination contested, returned to under_review
    reviewer_did TEXT,                    -- assigned human reviewer DID
    resolution_note TEXT,
    filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evidence_gathered_at TIMESTAMPTZ,
    determined_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Evidence items attached to a claim
-- Each evidence item references a specific record from Layers 1-5.
-- Append-only per claim. Evidence is never deleted.
CREATE TABLE liability_evidence (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id UUID NOT NULL REFERENCES liability_claims(id),
    evidence_type TEXT NOT NULL,
    -- 'workflow_execution'    = record from workflow_executions
    -- 'context_disclosure'    = record from context_disclosures
    -- 'context_mismatch'      = record from context_mismatch_events
    -- 'trust_attestation'     = record from Layer 3 attestation events
    -- 'trust_revocation'      = record from Layer 3 revocation events
    -- 'manifest_version'      = record from manifests (service claimed X at time T)
    -- 'validation_record'     = record from workflow_validations
    -- 'crawl_event'           = record from crawl_events
    source_table TEXT NOT NULL,           -- which table the evidence came from
    source_id UUID NOT NULL,              -- PK of the referenced record
    source_layer INTEGER NOT NULL,        -- 1-5, which layer produced this evidence
    summary TEXT NOT NULL,                -- human-readable summary of this evidence item
    raw_data JSONB NOT NULL DEFAULT '{}', -- copy of the relevant fields at gather time
    gathered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Attribution determinations
-- One record per claim, created when status transitions to 'determined'.
-- Append-only. If a claim is appealed and re-determined, a new record is created.
CREATE TABLE liability_determinations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id UUID NOT NULL REFERENCES liability_claims(id),
    determination_version INTEGER NOT NULL DEFAULT 1,  -- increments on appeal re-determination

    -- Attribution weights (must sum to 1.0)
    agent_weight FLOAT NOT NULL DEFAULT 0.0,
    service_weight FLOAT NOT NULL DEFAULT 0.0,
    workflow_author_weight FLOAT NOT NULL DEFAULT 0.0,
    validator_weight FLOAT NOT NULL DEFAULT 0.0,

    -- The actor DIDs/IDs at determination time
    agent_did TEXT NOT NULL,
    service_id UUID REFERENCES services(id),
    workflow_author_did TEXT,
    validator_did TEXT,

    -- Factors that drove the attribution (structured reasoning)
    attribution_factors JSONB NOT NULL DEFAULT '[]',
    -- [ { factor, actor, weight_contribution, evidence_id }, ... ]

    -- Confidence in the determination (0.0-1.0)
    -- Low confidence when evidence is sparse or contradictory
    confidence FLOAT NOT NULL DEFAULT 0.5,

    determined_by TEXT NOT NULL DEFAULT 'system',
    -- 'system'  = computed by attribution engine
    -- 'reviewer' = overridden by human reviewer (reviewer_did on the claim)

    determined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Log of all regulatory compliance exports generated
CREATE TABLE compliance_exports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    export_type TEXT NOT NULL,
    -- 'eu_ai_act'  = EU AI Act transparency and auditability package
    -- 'hipaa'      = HIPAA PHI access audit trail (health.* tags only)
    -- 'sec'        = SEC trade execution audit (finance.investment.* tags only)
    -- 'full'       = Combined all-layers export (same as Layer 4 compliance export
    --               but extended with Layer 5-6 records)
    agent_did TEXT,                       -- NULL for service-scoped exports
    service_id UUID REFERENCES services(id), -- NULL for agent-scoped exports
    execution_id UUID REFERENCES workflow_executions(id), -- NULL for full account exports
    claim_id UUID REFERENCES liability_claims(id), -- non-NULL for dispute-scoped exports
    from_date TIMESTAMPTZ,
    to_date TIMESTAMPTZ,
    record_count INTEGER NOT NULL DEFAULT 0,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX liability_snapshots_execution ON liability_snapshots(execution_id);
CREATE INDEX liability_claims_execution ON liability_claims(execution_id);
CREATE INDEX liability_claims_status ON liability_claims(status);
CREATE INDEX liability_claims_claimant ON liability_claims(claimant_did);
CREATE INDEX liability_evidence_claim ON liability_evidence(claim_id, source_layer);
CREATE INDEX liability_determinations_claim ON liability_determinations(claim_id,
    determination_version DESC);
CREATE INDEX compliance_exports_agent ON compliance_exports(agent_did, generated_at DESC)
    WHERE agent_did IS NOT NULL;
CREATE INDEX compliance_exports_type ON compliance_exports(export_type, generated_at DESC);
```

---

## The Liability Attribution Engine

This is the intellectual core of Layer 6. Given a filed claim and its gathered evidence,
the engine computes attribution weights across four actors. Weights must sum to 1.0.

### The Four Actors

| Actor | Identified By | Bears Responsibility For |
|---|---|---|
| Agent | `agent_did` | Choosing services, respecting context restrictions, acting within declared scope |
| Service | `service_id` | Fulfilling declared capabilities, handling context correctly, not over-requesting data |
| Workflow Author | `workflow_author_did` | Designing a sound workflow spec with appropriate trust thresholds |
| Validator | `validator_did` | Approving a workflow that met the quality and safety bar |

### Attribution Factor Catalog

Each factor shifts weight toward one actor by a defined amount. Factors are evaluated
from the evidence gathered. Not all factors apply to every claim type.

```python
ATTRIBUTION_FACTORS = {

    # --- SERVICE FACTORS ---

    "service_trust_below_step_minimum": {
        # The service's trust_score at execution time was below the workflow step's
        # min_trust_score. Agent chose an undertrusted service.
        "shifts_weight_to": "agent",
        "base_contribution": 0.15,
        "evidence_source": "liability_snapshots.step_trust_states"
    },

    "service_trust_tier_below_step_minimum": {
        # The service's trust_tier at execution time was below the step's min_trust_tier.
        # Stronger signal than score alone - tier is a categorical gate.
        "shifts_weight_to": "agent",
        "base_contribution": 0.20,
        "evidence_source": "liability_snapshots.step_trust_states"
    },

    "service_revoked_before_execution": {
        # A revocation event exists for the service with a timestamp before the
        # execution's reported_at. Agent used a service whose trust had been withdrawn.
        "shifts_weight_to": "agent",
        "base_contribution": 0.25,
        "evidence_source": "revocation_events + workflow_executions.reported_at"
    },

    "critical_context_mismatch_ignored": {
        # context_mismatch_events with severity='critical' exist for this execution's
        # agent_did + service combination. Agent proceeded despite a critical mismatch.
        "shifts_weight_to": "agent",
        "base_contribution": 0.20,
        "evidence_source": "context_mismatch_events"
    },

    "service_capability_not_verified": {
        # The service's claimed capability (ontology_tag for the failing step) has
        # is_verified=false in service_capabilities. Service was not capability-probed.
        "shifts_weight_to": "service",
        "base_contribution": 0.15,
        "evidence_source": "service_capabilities.is_verified"
    },

    "service_context_over_request": {
        # context_mismatch_events exist where the service requested context beyond
        # its manifest declaration. Service exceeded its declared scope.
        "shifts_weight_to": "service",
        "base_contribution": 0.20,
        "evidence_source": "context_mismatch_events"
    },

    "service_revoked_after_execution_for_related_reason": {
        # A revocation event exists after execution with a reason code related to
        # the claim type (e.g. claim_type='data_misuse', revocation reason includes
        # 'data_handling'). Evidence the harm was part of a pattern.
        "shifts_weight_to": "service",
        "base_contribution": 0.15,
        "evidence_source": "revocation_events"
    },

    # --- WORKFLOW AUTHOR FACTORS ---

    "workflow_quality_score_low_at_execution": {
        # The workflow's quality_score captured in the snapshot was below 60.0.
        # A low-quality workflow design contributed to the harm.
        "shifts_weight_to": "workflow_author",
        "base_contribution": 0.10,
        "threshold": 60.0,
        "evidence_source": "liability_snapshots.workflow_quality_score"
    },

    "workflow_trust_threshold_inadequate": {
        # The failing step's min_trust_tier was set below 3 for an ontology tag
        # with sensitivity_tier >= 3. Author set an insufficient trust floor
        # for a sensitive capability.
        "shifts_weight_to": "workflow_author",
        "base_contribution": 0.15,
        "evidence_source": "workflow_steps + ontology_tags.sensitivity_tier"
    },

    "workflow_no_fallback_for_critical_step": {
        # The failing step is is_required=true with no fallback_step_number, and
        # the ontology_tag has sensitivity_tier >= 3. Author provided no
        # degraded-mode path for a high-stakes step.
        "shifts_weight_to": "workflow_author",
        "base_contribution": 0.10,
        "evidence_source": "workflow_steps"
    },

    # --- VALIDATOR FACTORS ---

    "validator_approved_inadequate_trust_threshold": {
        # Same condition as workflow_trust_threshold_inadequate, but evaluated
        # against the validator's checklist. If trust_thresholds_appropriate=true
        # was checked but the threshold was actually inadequate, the validator
        # bears weight for approving incorrectly.
        "shifts_weight_to": "validator",
        "base_contribution": 0.10,
        "evidence_source": "workflow_validations.checklist"
    },

    "validator_approved_non_minimal_context": {
        # context_minimal=true was checked on the validation checklist, but
        # a critical context mismatch occurred on a step the validator approved.
        # Validator failed to catch context over-request during review.
        "shifts_weight_to": "validator",
        "base_contribution": 0.10,
        "evidence_source": "workflow_validations.checklist + context_mismatch_events"
    },
}
```

### Attribution Algorithm

```python
def compute_attribution(
    claim: LiabilityClaim,
    snapshot: LiabilitySnapshot,
    evidence: list[LiabilityEvidence],
    workflow: Workflow,
    workflow_steps: list[WorkflowStep],
    failing_step: WorkflowStep | None
) -> AttributionResult:

    weights = {
        "agent": 0.25,           # base weight: equal distribution
        "service": 0.25,
        "workflow_author": 0.25,
        "validator": 0.25
    }
    applied_factors = []

    for factor_name, factor_def in ATTRIBUTION_FACTORS.items():
        if factor_applies(factor_name, claim, snapshot, evidence, workflow_steps):
            actor = factor_def["shifts_weight_to"]
            contribution = factor_def["base_contribution"]

            # Shift weight from all other actors proportionally to the target actor
            other_actors = [a for a in weights if a != actor]
            per_other_reduction = contribution / len(other_actors)

            weights[actor] += contribution
            for other in other_actors:
                weights[other] -= per_other_reduction
                weights[other] = max(0.0, weights[other])  # floor at zero

            applied_factors.append({
                "factor": factor_name,
                "actor": actor,
                "weight_contribution": contribution,
                "evidence_ids": find_supporting_evidence(factor_name, evidence)
            })

    # Normalize to sum to exactly 1.0 (floating point correction)
    total = sum(weights.values())
    weights = {k: round(v / total, 4) for k, v in weights.items()}

    # Confidence: high when many factors apply, low when claim has sparse evidence
    confidence = min(1.0, 0.3 + (len(applied_factors) * 0.1))

    return AttributionResult(
        weights=weights,
        applied_factors=applied_factors,
        confidence=confidence
    )
```

### Claim Type to Likely Lead Actor

This is guidance for the attribution engine's initial weight distribution, not a
deterministic rule. Evidence overrides these priors.

| Claim Type | Initial Lead Actor | Rationale |
|---|---|---|
| `service_failure` | Service | Service did not fulfill its declared capability |
| `data_misuse` | Service | Service mishandled context it was trusted to receive |
| `wrong_outcome` | Shared | Could be agent, service, or workflow design |
| `unauthorized_action` | Agent | Agent acted outside its declared scope |
| `workflow_design_flaw` | Workflow Author | Spec was flawed; validator secondarily |

---

## Regulatory Compliance Export Formats

### EU AI Act Export
Required when any step in the workflow touches a high-risk AI system category
(health.*, finance.investment.*, or any ontology tag with sensitivity_tier >= 3).

Sections:
1. **System Identification** - agent DID, workflow ID, service IDs involved
2. **Human Oversight Records** - HITL interrupt events from Layer 2 (sensitivity_tier
   >= 3 capability authorizations), context bundle user approvals from Layer 5
3. **Transparency Records** - what the agent was authorized to do, what it did,
   what context was disclosed (field names only)
4. **Auditability Chain** - chronological event log linking L1 manifest version ->
   L2 session assertion -> L3 trust state -> L4 context disclosure -> L5 execution
5. **Incident Records** - any context mismatches, trust threshold failures, or
   disputes filed for this execution

### HIPAA Export
Applicable only when `health.*` ontology tags are present in the workflow.

Sections:
1. **PHI Access Log** - which health.* capabilities were invoked, which agent DID
   invoked them, which service handled them
2. **Minimum Necessary Standard** - evidence that context disclosure was limited
   to declared required fields (from context_disclosures)
3. **Business Associate Evidence** - service trust_tier and attestation records at
   execution time (evidence of due diligence in service selection)
4. **Breach Indicators** - any critical context mismatches involving health.* tags

### SEC Export
Applicable only when `finance.investment.*` ontology tags are present in the workflow.

Sections:
1. **Trade Execution Record** - agent DID, service ID, ontology tag invoked,
   timestamp, execution_id
2. **Authorization Chain** - session assertion (L2), trust verification (L3),
   context disclosure authorization (L4)
3. **Audit Trail** - complete event sequence with timestamps
4. **Agent Identity Verification** - L2 identity credentials for the executing agent

---

## API Specification - New Endpoints Only

Base URL: `https://api.agentledger.io/v1` (local: `http://localhost:8000/v1`)
All endpoints require `X-API-Key` or Bearer VC token.

---

### GET /liability/snapshots/{execution_id}
Retrieve the liability snapshot for a workflow execution.

**Response 200:** Full snapshot including step_trust_states and context_summary.
**Response 404:** No snapshot found for this execution_id.

Note: Snapshots are created automatically by the Layer 5 execution reporting path.
There is no POST endpoint - they are system-generated.

---

### POST /liability/claims
File a liability claim against a workflow execution.

**Request body:**
```json
{
  "execution_id": "uuid",
  "claimant_did": "did:key:z6Mk...",
  "claim_type": "service_failure",
  "description": "The flight booking service confirmed a booking but no ticket was issued.",
  "harm_value_usd": 450.00
}
```

**Processing:**
1. Verify execution_id exists
2. Verify snapshot exists for this execution (required for attribution)
3. Verify claimant_did is the agent_did on the execution (only the executing agent
   can file a claim in v0.1)
4. Create liability_claims record with status='filed'
5. Trigger async evidence gathering (see gather_evidence below)
6. Return claim record with claim_id

**Response 201:**
```json
{
  "claim_id": "uuid",
  "execution_id": "uuid",
  "status": "filed",
  "claim_type": "service_failure",
  "filed_at": "ISO 8601"
}
```
**Response 404:** Execution not found.
**Response 409:** Claim already filed for this execution_id + claimant_did.

---

### GET /liability/claims/{claim_id}
Retrieve a claim with all evidence and current determination (if any).

**Response 200:** Full claim object including evidence list and latest determination.
**Response 404:** Claim not found.

---

### POST /liability/claims/{claim_id}/gather
Trigger evidence gathering for a claim. Can be called manually if async gathering
failed. In normal flow, this is triggered automatically after filing.

**Processing - gather_evidence(claim_id):**
Query all of the following and create liability_evidence records:

1. **workflow_executions** (L5) - execution record with outcome, steps, duration
2. **workflow_validations** (L5) - validation checklist and validator_did
3. **liability_snapshots** (L6) - trust state at execution time
4. **context_disclosures** (L4) - all disclosures for this agent_did within execution window
5. **context_mismatch_events** (L4) - any mismatches for this agent_did + services
6. **manifests** (L1) - manifest version at time of execution (match by crawled_at <= executed_at)
7. **service_capabilities** (L1) - is_verified flag for the failing step's ontology_tag
8. **revocation_events** (L3) - any revocations for involved services (before AND after execution)

After all evidence records are created:
- Set claim status = 'evidence_gathered'
- Set claim evidence_gathered_at = NOW()

**Response 200:** Updated claim with evidence_count.

---

### POST /liability/claims/{claim_id}/determine
Trigger attribution computation. Admin/reviewer endpoint.

**Processing:**
1. Verify claim status = 'evidence_gathered' or 'under_review'
2. Load all evidence for this claim
3. Call compute_attribution() from liability_attribution.py
4. Create liability_determinations record
5. Set claim status = 'determined', determined_at = NOW()

**Request body:** `{ "reviewer_did": "did:key:..." }` (optional override)
If reviewer_did provided: set determined_by='reviewer' on determination.

**Response 200:**
```json
{
  "determination_id": "uuid",
  "claim_id": "uuid",
  "attribution": {
    "agent": 0.45,
    "service": 0.40,
    "workflow_author": 0.10,
    "validator": 0.05
  },
  "applied_factors": [...],
  "confidence": 0.80,
  "determined_at": "ISO 8601"
}
```

---

### POST /liability/claims/{claim_id}/resolve
Close a claim with a resolution note.

**Request body:**
```json
{
  "resolution_note": "Service provider agreed to refund. Claim closed.",
  "reviewer_did": "did:key:..."
}
```
**Response 200:** Updated claim with status='resolved'.
**Response 409:** Claim is not in 'determined' state.

---

### POST /liability/claims/{claim_id}/appeal
Contest a determination and return the claim to review.

**Processing:**
1. Verify claim status = 'determined'
2. Set claim status = 'under_review'
3. Increment determination_version counter (next determination will be version N+1)

**Request body:** `{ "appeal_reason": "string", "claimant_did": "did:key:..." }`
**Response 200:** Updated claim with status='under_review'.

---

### GET /liability/compliance/export
Generate a regulatory compliance export.

**Query params:**
- `export_type` (required) - `eu_ai_act | hipaa | sec | full`
- `agent_did` - scope to a specific agent
- `execution_id` - scope to a specific execution
- `claim_id` - scope to a dispute record
- `from_date`, `to_date` - date range for full exports

**Validation:**
- `hipaa` exports: reject if no `health.*` ontology tags found in scope
- `sec` exports: reject if no `finance.investment.*` ontology tags found in scope

**Response 200:** `application/pdf`
**Response 400:** No records found in scope, or wrong export_type for the ontology
tags present in the scoped records.

---

### GET /liability/snapshots
Admin endpoint. List all snapshots with filters.

**Query params:** `workflow_id`, `agent_did`, `from_date`, `to_date`, `limit`, `offset`
**Response 200:** Paginated snapshot summaries.

---

## Layer 5 Integration Points Activated by Layer 6

| # | Integration Point | Layer 5 State | Layer 6 Change |
|---|---|---|---|
| 1 | Workflow execution records | `workflow_executions` | Layer 6 auto-creates a liability_snapshot immediately after each execution is reported; snapshot captured before trust scores can change |
| 2 | Context bundle audit trail | `workflow_context_bundles` + L4 `context_disclosures` | Layer 6 evidence gathering pulls both tables into a single evidence package per claim |
| 3 | Quality score as risk signal | `workflows.quality_score` | Captured in snapshot at execution time; feeds `workflow_quality_score_low_at_execution` attribution factor |
| 4 | Validator accountability | `workflow_validations.validator_did` | Captured in snapshot; validator_did appears in attribution output and is a named actor in the determination |
| 5 | Pinned service revocation | `workflow_steps.service_id` + L3 revocation | Evidence gathering queries revocation_events for all pinned services; pre-execution revocations trigger the `service_revoked_before_execution` attribution factor |

---

## Threat Model - Layer 6 Additions

Layers 1-5 defined 18 threats. Layer 6 adds 4 more:

| # | Threat | Attack | Severity | Mitigation |
|---|---|---|---|---|
| 19 | Claim Flooding | Agent files mass spurious claims to degrade services' reputations by association | High High | One claim per execution_id per claimant_did (409 on duplicate); claim filing rate limit per agent_did |
| 20 | Attribution Gaming | Agent deliberately uses low-trust services in order to shift blame to service actor in attribution | Medium Medium | `service_trust_below_step_minimum` factor shifts weight BACK to agent; using an undertrusted service increases agent attribution, not decreases it |
| 21 | Evidence Tampering | Actor attempts to modify Layer 1-5 records after harm occurs to reduce their attribution weight | Critical Critical | All Layer 1-5 audit tables are append-only; snapshots capture state at execution time independently; on-chain attestation events are immutable |
| 22 | Snapshot Timing Attack | Actor triggers execution reporting at a moment when their trust score is artificially high, then lets it decay before a dispute is filed | Medium Medium | Snapshot is created synchronously in the execution reporting write path - not async - so it captures real-time state before the response is returned |

---

## Build Order

### Phase 1 - Snapshots
- Migration 007 with all five Layer 6 tables
- Pydantic models in `api/models/liability.py`
- `api/services/liability_snapshot.py` - snapshot creation logic
- Wire snapshot creation into Layer 5's `POST /workflows/{id}/executions` write path:
  after counters are incremented, before returning 201, call `create_snapshot(execution_id)`
- `GET /liability/snapshots/{execution_id}`
- `GET /liability/snapshots` (admin list)

**Done when:** POST /workflows/{id}/executions automatically creates a liability snapshot
retrievable via GET /liability/snapshots/{execution_id} with accurate step_trust_states
and context_summary populated.

---

### Phase 2 - Dispute Protocol
- `api/services/liability_claims.py` - claim CRUD and evidence gathering
- `POST /liability/claims` - file a claim
- `GET /liability/claims/{claim_id}` - retrieve claim with evidence
- `POST /liability/claims/{claim_id}/gather` - manual evidence gather trigger
- `POST /liability/claims/{claim_id}/resolve`
- `POST /liability/claims/{claim_id}/appeal`
- Async evidence gathering (sync in test mode, same WORKFLOW_VERIFY_SYNC pattern as L5)

**Done when:** Filing a claim against a failed execution triggers evidence gathering
across all 8 evidence sources, and GET /liability/claims/{id} returns all evidence
records with correct source_layer tags.

---

### Phase 3 - Attribution Engine
- `api/services/liability_attribution.py` - full attribution algorithm
- `POST /liability/claims/{claim_id}/determine`
- All attribution factors from the factor catalog implemented and tested individually

**Done when:** POST /liability/claims/{id}/determine returns an attribution with
weights summing to 1.0, applied_factors listing which factors fired, and confidence
score. Test with at least three distinct claim scenarios producing different lead actors.

---

### Phase 4 - Regulatory Compliance Export
- `api/services/liability_compliance.py` - EU AI Act, HIPAA, SEC PDF generation
- `GET /liability/compliance/export` - parameterized export endpoint
- Reuse reportlab pattern from Layer 4 compliance export
- compliance_exports log record created for each generated export

**Done when:** All three export types (eu_ai_act, hipaa, sec) return valid PDFs.
HIPAA export correctly rejects when no health.* tags are in scope. SEC export correctly
rejects when no finance.investment.* tags are in scope.

---

### Phase 5 - Hardening
- Rate limiting: 10 claim filings per agent_did per hour (abuse prevention)
- Redis claim status cache (TTL: 60s, invalidated on status transition)
- 80%+ test coverage for all new modules
- Load test: GET /liability/snapshots/{execution_id} at 100 concurrent, p95 < 200ms

---

## Acceptance Criteria (10 gates)

```
[ ] POST /workflows/{id}/executions auto-creates liability snapshot
[ ] Snapshot step_trust_states captures trust_score/trust_tier for each step's service
[ ] POST /liability/claims returns 201; duplicate claim returns 409
[ ] Evidence gathering populates records from all 8 sources (8 evidence_type values)
[ ] POST /liability/claims/{id}/determine returns attribution weights summing to 1.0
[ ] Attribution factors fire correctly: pre-execution revoked service increases agent_weight
[ ] Attribution factors fire correctly: critical mismatch increases agent_weight
[ ] GET /liability/compliance/export?export_type=eu_ai_act returns valid PDF
[ ] GET /liability/compliance/export?export_type=hipaa returns 400 when no health.* tags in scope
[ ] GET /liability/snapshots/{execution_id} p95 < 200ms @ 100 concurrent requests
```

---

## What Layer 6 Does NOT Include

- Actual insurance underwriting or premium pricing - requires licensed insurers
- Financial settlement processing - payment rails out of scope for v0.1
- Smart contract escrow - requires Layer 3 blockchain deployment to be live
- Legal binding determinations - Layer 6 produces evidence, not rulings
- AgentLedger governance framework - organizational structure, not code
- Cross-registry liability federation - future

---

*This spec is the source of truth for Layer 6. Update it before changing any behavior
described here.*
