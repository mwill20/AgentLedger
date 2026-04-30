# Lesson 41: The Registry That Does Not Execute — Layer 5 Overview & Workflow Architecture

> **Beginner frame:** The workflow registry is a recipe book, not a kitchen. AgentLedger validates, ranks, and records workflow specifications, while agent platforms remain responsible for executing them.

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `spec/LAYER5_SPEC.md`, `spec/LAYER5_COMPLETION.md`, `api/routers/workflows.py`, `db/migrations/versions/006_layer5_workflows.py`
**Prerequisites:** Lessons 01â€“40 â€” this lesson opens the Layer 5 series
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

Layers 1â€“4 handle single-hop interactions perfectly. An agent finds a service (Layer 1), proves its identity (Layer 2), checks the service's attestation (Layer 3), and discloses only the right context (Layer 4). One service. One interaction. Done.

Now consider booking business travel:

> **Search flights â†’ Book flight â†’ Search hotels â†’ Book hotel â†’ Arrange ground transport â†’ Add to calendar.**

Six services. Six trust checks. Six context disclosures. Without coordination, every agent platform that needs to book business travel must re-invent this orchestration from scratch â€” with no shared quality signals, no validated sequence, and no accountability chain spanning all six steps.

Layer 5 solves this with the **workflow registry**: a catalog of validated, machine-readable orchestration specs that any agent platform can consume.

> **"Layer 5 is to workflow orchestration what DNS is to IP routing: it publishes records. It does not route packets."**

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain the DNS analogy and why Layer 5 does not execute workflows
- Describe all six Layer 5 database tables and their purpose
- Map the 11-endpoint API surface to the five service modules
- Trace the four-phase data flow: submit â†’ validate â†’ rank â†’ execute
- Explain the three capabilities Layer 5 adds that Layers 1â€“4 do not have
- Identify what Layer 5 deliberately excludes (and why each exclusion matters)

---

## Where Layer 5 Fits

```
Layer 1: Registry       â€” Does this service exist? What can it do?
Layer 2: Identity       â€” Is this agent's DID verified?
Layer 3: Trust          â€” Has this service earned attestation?
Layer 4: Context        â€” What data can this service see about this agent?
                           â†“
Layer 5: Workflow        â€” What multi-step patterns are validated and quality-scored?
                           â†“
              [ Layer 6: Liability â€” who is accountable when a workflow fails? ]
```

Layer 5 is the first layer that thinks about **sequences** rather than individual interactions. It answers: "Is there a validated orchestration pattern for this task, and which services should I use for each step?"

---

## What Layer 5 Adds

Layer 5 provides three capabilities absent from Layers 1â€“4:

| Capability | Description |
|-----------|-------------|
| **Workflow Registry** | Machine-readable catalog of validated multi-step orchestration specs |
| **Human Validation Queue** | Domain experts review and approve workflow definitions before publication |
| **Quality Feedback Loop** | Execution outcomes feed composite quality scores back into ranking |

These are layered on top of the existing stack. Layer 5 adds no new infrastructure â€” no new runtime, no new chain, no new auth system.

---

## The DNS Analogy â€” A Critical Design Principle

DNS publishes that `api.example.com â†’ 203.0.113.5`. It does not route the HTTP request. Layer 5 publishes that "business travel booking = steps 1â€“3 in this order with these trust thresholds." It does not execute the booking.

This boundary is both architectural and strategic:

- **Architectural:** Execution requires real-time state management, retry logic, rollback handling â€” concerns that belong in orchestration frameworks, not trust infrastructure.
- **Strategic:** If AgentLedger executed workflows, it would compete with agent platforms instead of being infrastructure they depend on. The registry model makes every agent platform a consumer of Layer 5, not a competitor.

---

## The Six Database Tables

Migration `006_layer5_workflows.py` adds six new tables to the existing schema:

```
workflows                   â€” Workflow definitions with status, quality_score, and execution counters
workflow_steps              â€” Ordered step records per workflow (ontology_tag, trust thresholds, context fields)
workflow_validations        â€” Validator assignment and decision records
workflow_executions         â€” Per-run outcome reports from agent platforms
workflow_context_bundles    â€” Multi-step context aggregation with single user approval
workflow_scoped_profiles    â€” Per-workflow profile overrides extending agent base profile
```

### Relationship diagram

```
workflows
  â”œâ”€â”€ workflow_steps (ON DELETE CASCADE â€” steps deleted with workflow)
  â”œâ”€â”€ workflow_validations (FK to workflows)
  â”œâ”€â”€ workflow_executions (FK to workflows + agent_identities)
  â”œâ”€â”€ workflow_context_bundles (FK to workflows + agent_identities + context_profiles)
  â””â”€â”€ workflow_scoped_profiles (FK to workflows + agent_identities + context_profiles)
                                UNIQUE(workflow_id, agent_did)
```

Layer 5 tables reference Layer 1 (`services`, `ontology_tags`), Layer 2 (`agent_identities`), and Layer 4 (`context_profiles`) but do not modify any of those tables.

---

## The Five Service Modules

```
api/services/workflow_registry.py   â€” CRUD, spec validation, execution reporting, quality recompute
api/services/workflow_validator.py  â€” Validation queue management, decision recording, spec hashing
api/services/workflow_ranker.py     â€” Quality score formula, per-step candidate ranking, Redis cache
api/services/workflow_context.py    â€” Bundle creation, field aggregation, scoped profile, approval
api/services/workflow_executor.py   â€” Execution outcome write path + verification logic
```

Each module has a clear boundary:

- `workflow_registry.py` owns the `workflows` and `workflow_steps` tables
- `workflow_validator.py` owns the `workflow_validations` table
- `workflow_ranker.py` owns quality score computation (reads from all Layer 5 tables; writes only to `workflows.quality_score`)
- `workflow_context.py` owns the `workflow_context_bundles` and `workflow_scoped_profiles` tables
- `workflow_executor.py` owns the `workflow_executions` table

---

## The 11-Endpoint API Surface

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/v1/workflows` | Submit workflow spec for validation |
| `PUT` | `/v1/workflows/{id}` | Replace a draft workflow spec |
| `GET` | `/v1/workflows` | List workflows with domain/tag/quality filters |
| `GET` | `/v1/workflows/{id}` | Retrieve workflow by UUID |
| `GET` | `/v1/workflows/slug/{slug}` | Retrieve workflow by slug |
| `POST` | `/v1/workflows/{id}/validate` | Assign draft workflow to a validator (admin) |
| `PUT` | `/v1/workflows/{id}/validation` | Record validator approval/rejection/revision |
| `POST` | `/v1/workflows/{id}/executions` | Report execution outcome, trigger quality recompute |
| `GET` | `/v1/workflows/{id}/rank` | Return per-step ranked service candidates |
| `POST` | `/v1/workflows/context/bundle` | Create workflow-level context bundle |
| `POST` | `/v1/workflows/context/bundle/{id}/approve` | User approves context bundle |

---

## The Four-Phase Data Flow

```
Phase 1: Submit
  POST /workflows â†’ validate spec (10 rules) â†’ INSERT workflows + workflow_steps
                  â†’ auto-assign to validation queue

Phase 2: Validate (human-in-the-loop)
  POST /workflows/{id}/validate â†’ assign validator_did
  PUT /workflows/{id}/validation â†’ decision (approved/rejected/revision_requested)
  On approval: status='published', spec_hash locked, initial quality_score computed

Phase 3: Discover & Rank
  GET /workflows (list with filters, sorted by quality_score)
  GET /workflows/{id}/rank â†’ per-step service candidates from Layer 1 + Layer 3
  Layer 4 can_disclose gating applies when agent_did provided

Phase 4: Execute & Report
  POST /workflows/context/bundle â†’ aggregate context for all steps
  POST /workflows/context/bundle/{id}/approve â†’ user approves
  (Agent platform executes the workflow)
  POST /workflows/{id}/executions â†’ outcome report â†’ quality_score recomputed
```

---

## What Layer 5 Does NOT Include

| Excluded | Reason |
|----------|--------|
| Workflow execution runtime | Agent platforms execute; AgentLedger validates and serves specs â€” executing makes us a runtime, not infrastructure |
| Payment processing | Layer 6 |
| Insurance evidence inputs | Layer 6; underwriting is out of scope |
| Cross-registry federation | Future |
| Workflow marketplace monetization | Layer 6 |
| Full ZKP for context bundles | v0.2 (inherits Layer 4 deferral) |

---

## The Workflow Status State Machine

```
draft          (submitted, not yet reviewed)
  â†“ POST /workflows/{id}/validate (admin assigns)
in_review      (assigned to validator)
  â†“ PUT /workflows/{id}/validation
  â”œâ”€â”€ approved    â†’ published
  â”œâ”€â”€ rejected    â†’ rejected (terminal)
  â””â”€â”€ revision_requested â†’ draft (back to author)
```

A published workflow is immutable. Any spec change requires a new `POST /workflows` submission, creating a new workflow UUID with `parent_workflow_id` pointing to the previous version.

---

## Layer 3 and Layer 4 Integration Points

Layer 5 activates five integration points with lower layers:

**Layer 3 (Trust):**
- `GET /workflows/{id}/rank` uses `trust_tier` and `trust_score` from Layer 3 to filter and rank service candidates per step
- A revocation of a pinned service (`workflow_steps.service_id IS NOT NULL`) triggers a workflow re-validation check

**Layer 4 (Context):**
- `workflow_context_bundles` groups multiple single-service context disclosures under one user approval
- `GET /workflows/{id}/rank` uses Layer 4's `evaluate_profile()` to compute `can_disclose` per candidate
- `workflow_scoped_profiles` extends an agent's base Layer 4 profile with workflow-specific field overrides

---

## Exercise 1 â€” Read the Schema

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "\d+ workflows"
```

Note: `status`, `quality_score`, `execution_count`, `success_count`, `failure_count` are all on the `workflows` table. The step detail lives in `workflow_steps`. This design keeps list queries fast: listing 100 workflows requires no JOIN.

---

## Exercise 2 â€” Inspect a Published Workflow

If a published workflow exists in your environment:

```bash
curl -s "http://localhost:8000/v1/workflows?status=published" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

If no published workflows exist yet, you will complete the full submission â†’ validation â†’ publication flow in Lesson 43â€“44.

---

## Exercise 3 â€” Map Module to Table

For each Layer 5 service module, identify which table(s) it owns:

| Module | Tables owned |
|--------|-------------|
| `workflow_registry.py` | ? |
| `workflow_validator.py` | ? |
| `workflow_ranker.py` | ? (reads all, writes one column) |
| `workflow_context.py` | ? |
| `workflow_executor.py` | ? |

**Expected answers:** registry â†’ workflows + workflow_steps; validator â†’ workflow_validations; ranker â†’ workflows.quality_score; context â†’ workflow_context_bundles + workflow_scoped_profiles; executor â†’ workflow_executions.

---

## Best Practices

**Never let Layer 5 execute anything.** Any time a feature request suggests "and then AgentLedger should call the service," the answer is no. The execution stays with the agent platform. Layer 5's value is the validated spec and quality signals â€” not the execution.

**Recommended (not implemented here):** A webhook notification system for workflow validators â€” when a new workflow is assigned to them, they receive a push notification rather than having to poll.

---

## Interview Q&A

**Q: Why does Layer 5 not execute workflows if it knows their step order?**
A: Execution requires real-time state management, retry logic, and rollback handling â€” responsibilities that belong in orchestration frameworks. More importantly, if AgentLedger executed workflows, it would become a competitor to agent platforms rather than infrastructure they depend on. The DNS analogy is precise: DNS publishes records; it does not route packets.

**Q: Why are workflow steps stored in a separate table from workflows?**
A: Separation of read access patterns. Listing 100 workflows requires only the `workflows` table â€” no JOIN. Retrieving one workflow's full spec requires the JOIN. This avoids loading step arrays for every list result. The `ON DELETE CASCADE` ensures steps are cleaned up atomically with their workflow.

**Q: What does `parent_workflow_id` track?**
A: When an author submits an updated version of a workflow, the new submission includes `parent_workflow_id` pointing to the previous workflow UUID. This allows discovery tooling to trace the lineage of a workflow across versions and compare quality scores over time â€” without mutating the published spec.

---

## Key Takeaways

- Layer 5 is a registry and validation layer, not a runtime â€” the DNS analogy
- Six new tables: workflows, workflow_steps, workflow_validations, workflow_executions, workflow_context_bundles, workflow_scoped_profiles
- Five service modules with clear ownership boundaries
- 11 endpoints across four phases: submit â†’ validate â†’ rank â†’ report
- Layer 5 integrates with Layer 3 (trust filtering per step) and Layer 4 (context bundle, can_disclose gating)
- Published workflow specs are immutable â€” any change creates a new workflow

---

## Next Lesson

**Lesson 42 â€” The Blueprint Department: Workflow Spec Format & Validation Rules** covers the machine-readable JSONB spec, the ten validation rules enforced at submission time, and how `_validate_workflow_spec()` uses the ontology registry and Layer 1 service data to reject invalid submissions before they ever reach the validation queue.
