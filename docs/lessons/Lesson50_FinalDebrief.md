# Lesson 50: The Final Debrief — Full Layer 5 Flow & Interview Readiness

> **Beginner frame:** Layer 5 answers "which workflow should an agent trust enough to run?" This debrief ties workflow specs, human validation, service ranking, context bundles, and execution feedback into one quality loop.

**Layer:** 5 — Workflow Registry & Quality Signals
**Source:** All Layer 5 files — `workflow_registry.py`, `workflow_validator.py`, `workflow_ranker.py`, `workflow_context.py`, `workflow_executor.py`, `spec/LAYER5_COMPLETION.md`
**Prerequisites:** Lessons 41–49
**Estimated time:** 90 minutes

---

## Welcome Back, Agent Architect!

A flight's black box exists to answer one question after an incident: what happened, in what order, and why? This lesson is the black box for Layer 5. You have already traced every individual component — the CRUD layer, the validation queue, the quality score formula, the ranking engine, the context bundle, the execution feedback loop, and the four threat mitigations. This lesson connects them into a single coherent end-to-end story, defines the Layer 5 invariant, and closes with five canonical interview questions that compress the entire layer into interview-ready answers.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Trace a workflow from author submission to agent execution in a single narrative
- State the Layer 5 invariant in one falsifiable sentence
- Recite the six build phases and what each phase delivered
- Answer five canonical interview questions without notes
- Name all five Layer 6 handoff points and what Layer 6 builds on each

---

## The Layer 5 Invariant

> **A workflow's quality score above 70.0 requires cryptographic evidence that context was actually disclosed for each step, in the right time window, by the claiming agent.**

Everything in Layer 5 is a consequence of this invariant:

- `spec_hash` exists because a workflow whose spec can change after approval cannot be trusted as the thing being scored
- The human validation queue exists because a quality signal built on unreviewed specs is meaningless
- `volume_factor` exists because a single execution proves nothing
- The `verified=True` flag exists to link a reported outcome to Layer 4 audit evidence
- The 70.0 cap exists because unlinked outcomes cannot cross the line that separates "visible" from "top-ranked"

If you remove any of these, the invariant breaks.

---

## The Full End-to-End Flow

### Phase 1 — Submission (Lessons 42–43)

A workflow author submits `POST /v1/workflows` with a machine-readable JSONB spec. The spec is validated by ten rules:

- Seven by Pydantic at request time: spec version format, step count (1–20), sequential step numbering, fallback ordering (`fallback_step_number < step_number`), trust tier range (0–4), trust score range (0–100), no duplicate `ontology_tag`
- Three by DB lookup: all `ontology_tag` values exist in the ontology table; any pinned `service_id` has the declared tag as a capability; any step touching `sensitivity_tier >= 3` fields triggers domain-specific validator assignment

A valid submission creates:
- One `workflows` row (status=`draft`, `quality_score=35.0`)
- N `workflow_steps` rows (one per step, bulk-inserted)
- One `workflow_validations` sentinel row (pending)

The response returns `{workflow_id, slug, status: "draft", validation_id, estimated_review_hours: 48}`.

### Phase 2 — Validation Queue (Lesson 44)

An admin calls `POST /v1/workflows/{id}/validate` to assign a validator. The endpoint checks the workflow is `draft`, upserts the validation sentinel with `status=in_review` and a `validator_did`.

The validator calls `PUT /v1/workflows/{id}/validation` with a five-item boolean checklist:
- `steps_achievable` — every step has at least one qualifying service in the registry
- `context_minimal` — each step requests only fields needed for its ontology tag
- `trust_thresholds_appropriate` — the declared tiers match the data sensitivity
- `no_sensitive_tag_without_domain_review` — high-sensitivity steps have domain expert sign-off
- `fallback_logic_sound` — fallback chains are acyclic and terminal

All five must be `true` to approve. On approval:
- `workflow.status` → `published`
- `workflows.spec_hash` = `sha256(json.dumps(spec, sort_keys=True))` is computed and stored
- `workflows.quality_score` is initialized to `35.0` via `compute_initial_quality_score()`
- All workflow caches (detail, slug, list) are invalidated

Published spec is immutable — `PUT /workflows/{id}` on a published workflow returns 409.

### Phase 3 — Discovery & Ranking (Lessons 43, 46)

Agent platforms discover workflows via `GET /v1/workflows` with domain, tag, quality, and status filters. The list endpoint:
- Uses a SHA-256 hash of canonical filter params as the Redis cache key
- Returns results sorted `quality_score DESC`
- Aggressive cache invalidation: all `workflow:list:*` keys are cleared on any quality change

For any published workflow, `GET /v1/workflows/{id}/rank` returns per-step service candidates:
- SQL filters: `ontology_tag` match, `trust_tier >= step.min_trust_tier`, `trust_score >= step.min_trust_score`, `is_active=true`, `is_banned=false`
- Optional geo and pricing model filters
- Layer 4 `evaluate_profile()` applied to compute `can_disclose` per candidate
- Results sorted `trust_score DESC`, limit 10 per step
- Cache key: `workflow:rank:{workflow_id}:{geo}:{pricing_model}:{agent_did}` — 60s TTL

At 100 concurrent users sharing the same cache key: **p95 = 24ms** (8× better than the 200ms target).

### Phase 4 — Context Bundle (Lesson 47)

Before executing, the agent creates a pre-authorization bundle via `POST /v1/workflows/context/bundle`:

1. Load the published workflow and all its steps (1 query)
2. Load the agent's base context profile (Redis-cached, 60s TTL)
3. Apply `scoped_profile_overrides` — inject a priority-0 rule that overrides base profile for specific fields
4. For each step, classify every field as `permitted`, `committed`, or `withheld` using Layer 4's `evaluate_profile()`
5. Deduplicate the field union across all steps (first-seen order preserved)
6. Insert a `workflow_context_bundles` row: status=`pending`, 30-minute TTL

The agent reviews the `by_step` breakdown (which step shares which fields) and approves once via `POST /v1/workflows/context/bundle/{id}/approve`. Approval checks: ownership, non-expired, status=`pending`. On approval: status → `approved`.

### Phase 5 — Execution Reporting (Lesson 48)

After running the workflow, the agent platform calls `POST /v1/workflows/{id}/executions`:

1. Validate workflow is `published`
2. Validate agent exists and is not revoked
3. Validate bundle ownership (if provided): bundle must belong to this `(workflow_id, agent_did)`
4. Insert `workflow_executions` row (`verified=false`)
5. Atomic counter update: `execution_count + 1`, `success_count + 1` (if outcome=`success`), `failure_count + 1` (if outcome=`failure`); partial outcomes increment only `execution_count`
6. Commit, then recompute `quality_score`; invalidate rank and detail/list caches

Verification runs async via `BackgroundTasks`:
- Queries `context_disclosures` for the agent, in the window `(reported_at - 35min)` to `(reported_at + 5min)`
- Checks that every required step's `ontology_tag` appears in the disclosed tags
- Sets `verified=True` if all required tags are covered
- Triggers a second quality recompute after verification settles

### Phase 6 — Quality Signal (Lesson 45)

```
quality_score = round(raw * 100, 2)

raw = validation_score * 0.35
    + success_rate * 0.30 * volume_factor
    + verification_rate * 0.20
    + avg_step_trust * 0.15

if verification_rate < 0.5:
    raw = min(raw, 0.70)
```

The score grows along a natural evidence accumulation curve:

| Stage | Condition | Typical score |
|-------|-----------|--------------|
| Newly published | 0 executions, no pinned services | 35.0 |
| Early signal | 50 unverified successes | ~62.0 |
| Quality gaming attempt | 10,000 unverified successes | ≤ 70.0 (capped) |
| Trusted workflow | 200 executions, 95% success, 80% verified | 93.0 |

---

## The Six Build Phases

| Phase | What shipped | Acceptance gate |
|-------|-------------|----------------|
| 1 | Registry CRUD — migration 006, Pydantic models, create/list/get/update | `POST /workflows` returns 201 for valid spec; 422 for invalid |
| 2 | Human validation queue — assign, decide, state machine | `draft → in_review → published` verified end-to-end |
| 3 | Ranking engine — quality score formula, Redis cache, Layer 3 trust filter | p95 < 200ms @ 100 concurrent |
| 4 | Context bundle integration — field aggregation, scoped overrides, approve flow | Bundle aggregates fields across all steps correctly |
| 5 | Outcome feedback loop — execution reporting, bundle verification, quality recompute | Unverified executions cannot push `quality_score` above 70.0 |
| 6 | Hardening + load test — rate limit, Redis warming, threat mitigations | p95 = 24ms; all four threat mitigations verified |

---

## The Four Threat Mitigations (Summary)

| Threat | Severity | Mitigation | Breaks without it |
|--------|----------|-----------|------------------|
| 19 — Workflow Laundering | Critical | `spec_hash` at publication + 409 on PUT for published | Adversary modifies stored spec after validation; all executions run the modified version undetected |
| 20 — Step Poisoning | Critical | `context_minimal` checklist item + auto-domain-validator for `sensitivity_tier >= 3` | Malicious step requesting SSN in a travel workflow approved by a general validator |
| 21 — Quality Gaming | High | `verification_rate < 0.5` → cap at 70.0 | 10,000 fake success reports push workflow to rank 1 in every search result |
| 22 — Context Bundle Abuse | High | Bundle scoped to `(workflow_id, agent_did)` + 30-min TTL + `consumed` status | Bundle from workflow A authorizes context disclosure for workflow B |

---

## Caching Architecture Summary

| Cache | Key pattern | TTL | Invalidation trigger |
|-------|------------|-----|---------------------|
| Workflow detail | `workflow:detail:{id}` | 60s | Status change, quality change |
| Workflow slug | `workflow:slug:{slug}` | 60s | Same as detail |
| Workflow list | `workflow:list:{sha256(filter_params)}` | 60s | Any quality or status change (all `workflow:list:*` cleared) |
| Workflow rank | `workflow:rank:{id}:{geo}:{pricing}:{agent_did}` | 60s | Execution report that changes quality_score |
| Context profile | `context:profile:{agent_did}` | 60s | Profile write (Layer 4) |

---

## Rate Limit Summary

- **Target:** Per API key (not per IP) — multi-agent platforms behind one gateway share one IP
- **Threshold:** 200 list/rank queries per 60-second window
- **Key:** `sha256(api_key.encode()).hexdigest()` — API key never appears in Redis keyspace
- **Fail-open:** Rate limit bypassed if Redis is unavailable (workflow discovery is read-only; excess DB load is worse-case, not a security breach)

---

## Five Canonical Interview Questions

### Q1: Why doesn't Layer 5 execute workflows?

Layer 5 publishes workflow specs — it does not run them. This is the DNS analogy: DNS publishes records (which IP maps to which hostname), but it does not route packets. Execution requires a runtime with access to the agent's current context, session state, real-time service endpoints, and error-handling logic that is application-specific. Layer 5 provides the validated spec (what should happen in what order) and the per-step service shortlist (who is capable and trusted). The agent platform provides the runtime that does it.

Separating publication from execution means the Layer 5 registry can be a shared, cached, read-heavy infrastructure layer — not a per-request hotpath.

### Q2: Why is the quality score anchored at 35.0 at publication, not 0 or 100?

The 35.0 floor reflects human validation: `validation_score = 1.0` × weight `0.35` = `0.35` → `35.0`. Human review is the foundational quality signal — a domain expert verified the spec is achievable, context-minimal, and trust-appropriate. That base signal is granted unconditionally at approval time, before a single execution.

Score above 35.0 requires execution evidence. Score above 70.0 requires verified execution evidence. The three thresholds (35.0, 70.0, theoretical 100.0) represent three epistemic states: reviewed, used, and proven.

### Q3: What does `verified=True` on an execution actually prove?

It proves that the claiming agent disclosed context to services matching the required ontology tags, within a 35-minute window before the execution report was submitted. It does not prove that the correct specific service was used — only that some service with the right capability tag received context from this agent at approximately the right time.

More concretely: `verified=True` means the Layer 4 `context_disclosures` audit trail contains rows for every step's `ontology_tag`, attributed to this agent, in the right time window. Without `verified=True`, you only have the agent platform's claim. With it, you have cryptographic audit trail evidence that something actually happened.

### Q4: How does the context bundle prevent the need for six separate consent screens?

Without bundles, a six-step workflow requires twelve Layer 4 round-trips: six `POST /context/match` calls (one per step) and six `POST /context/disclose` calls. Each call requires the agent to review a per-step classification and consent individually.

The bundle aggregates all six steps' field requirements into a single `by_step` breakdown. The agent reviews the complete picture — "Step 1 shares these fields, Step 2 shares these fields, Step 3 commits this sensitive field rather than sharing it as plaintext" — and approves once. The resulting `bundle_id` serves as pre-authorization for all six Layer 4 match calls. From six consent interactions to one.

### Q5: Why does Layer 5 use three separate Redis cache types rather than one?

Because the invalidation triggers are different:

- **Detail cache** invalidates on spec changes (publication, deprecation) or quality score changes — roughly per-event
- **List cache** invalidates on every quality score change from any execution report, because list results are sorted `quality_score DESC` — any execution that changes one workflow's score makes every cached list page potentially stale
- **Rank cache** invalidates on execution reports that change quality, and is scoped to four dimensions (workflow + geo + pricing + agent) because the `can_disclose` flags are agent-specific

A single unified cache would either under-invalidate (stale data leaks) or over-invalidate (thrashing on every execution report, eliminating the p95=24ms benefit).

---

## Layer 6 Handoff Points

Layer 5 exposes five integration surfaces that Layer 6 (Liability & Insurance) builds on:

| Handoff | Layer 5 Surface | What Layer 6 Builds |
|---------|----------------|---------------------|
| Liability attribution | `workflow_executions.workflow_id + agent_did + outcome + failure_step_number` | Who ran which workflow, which step failed, which service was responsible |
| Regulatory compliance package | `workflow_context_bundles.id` + Layer 4 `context_disclosures` | Combined per-execution compliance export linking bundle approval to actual disclosures |
| Insurance pricing | `workflows.quality_score` | Low quality score → higher coverage premium; verified workflows qualify for lower rates |
| Validator accountability chain | `workflow_validations.validator_did` | The human validator who approved a workflow that caused harm is part of the accountability record |
| Revocation-at-execution-time | `workflow_steps.service_id` + Layer 3 `attestation_records` | Trust state of each pinned service at the moment the workflow executed — relevant to liability when a service loses Tier 4 between approval and execution |

---

## Curriculum Completion: Layers 1–5

With Lesson 50, the Layers 1–5 curriculum is complete. Here is a summary of what each layer contributed:

| Layer | Name | Core contribution |
|-------|------|-----------------|
| 1 | Manifest Registry | Discovery — services publish what they can do and who they are |
| 2 | Identity & Credentials | Authentication — agents and services prove cryptographic identity before any interaction |
| 3 | Trust & Verification | Trust signals — independent auditors attest to service quality; blockchain makes attestations tamper-evident |
| 4 | Context Matching & Disclosure | Privacy enforcement — agents control what context flows to which services, with audit trail |
| 5 | Workflow Registry & Quality Signals | Orchestration — multi-step workflow specs are published, validated by humans, and quality-scored from verified execution evidence |

Each layer answers a different question:

- "What can this service do?" → Layer 1
- "Who is this agent?" → Layer 2
- "Can this service be trusted?" → Layer 3
- "What context am I sharing, and with whom?" → Layer 4
- "Does this workflow produce reliable outcomes?" → Layer 5

Layer 6 answers: "When something goes wrong, who is responsible and who bears the cost?"

---

## Exercise 1 — Trace the Full Flow in Isolation

Without running any code, write out the six phases in bullet form with the primary data mutation at each phase:

```
Phase 1 (Submission):    workflows row inserted, status='draft'
Phase 2 (Validation):    workflows.status → 'published', spec_hash computed
Phase 3 (Discovery):     Redis hit: workflow:detail:{id}, list cache served
Phase 4 (Bundle):        workflow_context_bundles row inserted, status='pending'
Phase 5 (Execution):     workflow_executions inserted, counters incremented
Phase 6 (Quality):       quality_score recomputed, caches invalidated
```

**Check:** After Phase 2, what data mutation would make the quality score above 70.0 possible?

*(Answer: `workflow_executions.verified = true` for more than 50% of executions. Specifically, `verified_count / execution_count >= 0.5`.)*

---

## Exercise 2 — Quality Score Gauntlet

Compute the quality score from scratch for each scenario. Show all intermediate values.

**Scenario A:** 0 executions, no pinned services, published.
**Scenario B:** 10 executions, all successful, none verified, `avg_step_trust=0.75`.
**Scenario C:** 150 executions, 130 successful, 90 verified, `avg_step_trust=0.9`.

*(Expected: A=35.0, B≤70.0, C compute manually — should be above 80.0)*

---

## Exercise 3 — Threat Walkthrough

For Threat 21 (Quality Gaming), trace what happens step-by-step when a malicious actor:

1. Creates a published workflow
2. Reports 500 fake `outcome='success'` executions (no `context_bundle_id`)
3. Queries the workflow's `quality_score`
4. Reports 1 more execution with a valid `context_bundle_id`

At each step, state the `verification_rate`, whether the cap applies, and the resulting `quality_score` range.

---

## Interview Quick Reference

| Question stem | One-sentence answer |
|--------------|---------------------|
| "Why doesn't Layer 5 execute?" | It publishes specs like DNS publishes records — execution is the platform's job |
| "Why start at 35.0?" | Human validation is worth 0.35 × 100 = 35 points — unconditional at approval |
| "What does verified=True prove?" | Layer 4 audit trail shows the agent disclosed context to the right capability tag in the right time window |
| "Why a bundle?" | One user approval covers all steps instead of N separate consent interactions |
| "Why three cache types?" | Different invalidation triggers: spec changes vs. quality changes vs. agent-specific can_disclose flags |
| "What's the invariant?" | quality_score > 70.0 requires cryptographic audit trail evidence linking reported outcomes to actual context disclosures |

---

## Key Takeaways

- Layer 5 invariant: quality above 70.0 requires `verification_rate >= 0.5` — cryptographic audit trail evidence for each execution
- Six phases: CRUD → validation queue → ranking → context bundle → execution feedback → hardening
- Four threats: spec laundering (19), step poisoning (20), quality gaming (21), bundle abuse (22) — each has a mitigation that breaks the threat model if removed
- Three cache types, three invalidation patterns — not interchangeable because invalidation triggers differ
- The Layer 6 handoff begins at `workflow_executions` — liability attribution needs the outcome + failure step + agent DID chain

---

## Congratulations

You have completed the Layer 1–5 curriculum for AgentLedger. You can now explain, trace, debug, and extend all five infrastructure layers from the manifest registry through quality-scored workflow orchestration. Layer 6 (Liability & Insurance) is where these signals become the evidentiary basis for accountability — the final layer before the system handles real-world consequences.
