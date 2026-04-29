# Lesson 60: The Final Debrief — Full Layer 6 Flow & Interview Readiness

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** All Layer 6 files + `spec/LAYER6_COMPLETION.md`
**Prerequisites:** Lessons 51–59
**Estimated time:** 90 minutes

---

## Welcome Back, Agent Architect!

The final report to the board. After months of audit fieldwork — reviewing documents, interviewing witnesses, running confirmatory tests — the auditor presents a consolidated picture: here is what happened, here is the evidence, here is the responsibility breakdown, and here is the regulatory package for the regulator. Layer 6 is that final report.

This lesson assembles everything from Lessons 51–59 into a single end-to-end narrative, states the Layer 6 invariant, and closes the 60-lesson curriculum with five canonical interview questions that span the full AgentLedger stack.

---

## Learning Objectives

By the end of this lesson you will be able to:

- State the Layer 6 invariant in one falsifiable sentence
- Trace a liability event from workflow execution to resolved claim in one narrative
- Explain the five build phases and their acceptance criteria
- Answer five canonical interview questions about Layer 6 and the full AgentLedger stack
- Articulate how each layer answers a different question about agent accountability

---

## The Layer 6 Invariant

> **Every workflow execution produces a frozen point-in-time trust record before the 201 response returns; no attribution determination is possible without that record.**

This invariant has two parts:

**Part 1: Synchronized snapshot creation.** The snapshot is created synchronously, before the execution report's 201 response returns to the caller. This closes the window in which trust scores could change (e.g., crawl cycle overwrites `services.trust_score`) between execution and evidence capture. If snapshot creation fails, the execution endpoint returns 500 — not 201 — because a missing snapshot means attribution is impossible.

**Part 2: Snapshot as prerequisite for claims.** `file_claim()` returns 422 if no snapshot exists for the execution. No snapshot = no claim. This structural enforcement means every claim in `liability_claims` is guaranteed to have an associated frozen evidence anchor in `liability_snapshots`.

---

## The Full End-to-End Flow

### Phase 1 — Execution & Snapshot Creation

A workflow execution is reported via `POST /workflows/{id}/executions` (Layer 5). After the execution record is committed, the Layer 5 executor calls:

```python
await liability_snapshot.create_snapshot(db=db, execution_id=execution_id)
await db.commit()
```

`create_snapshot()` runs seven SQL queries to capture:
- Workflow state: `quality_score`, `author_did`, `validator_did`, validation checklist
- Per-step service trust state: resolved via pinned `service_id` or lateral join against `context_disclosures`
- Context summary: all fields disclosed, withheld, committed in the 35+5 minute window
- Agent profile default policy

The resulting `liability_snapshots` row is frozen — never updated after creation.

### Phase 2 — Claim Filing

A claimant (agent platform, compliance officer, the agent itself) files a claim via `POST /liability/claims`:

```python
ClaimCreateRequest(
    execution_id=...,
    claimant_did="did:key:...",
    claim_type="service_failure",
    description="Step 1 service returned empty result",
    harm_value_usd=150.0
)
```

Four checks: rate limit (10/hour per claimant DID), execution exists, snapshot exists, no duplicate. On success: `liability_claims` row with `status="filed"`.

### Phase 3 — Evidence Gathering

Evidence gathering runs async (or sync in tests). Eight source queries pull records from Layers 1–6 and attach them as `liability_evidence` rows with copied `raw_data`. Each attach is idempotent (`ON CONFLICT DO NOTHING`). After gathering: `status → "evidence_gathered"`.

### Phase 4 — Review & Determination

A human reviewer is assigned (`reviewer_did` set, `status → "under_review"`). The determination endpoint is called:

```python
POST /liability/claims/{id}/determine
{"determined_by": "did:key:z6MkReviewer"}
```

Five queries load the claim, snapshot, evidence, workflow, workflow steps, and execution. `compute_attribution()` evaluates all 11 factors as pure functions over the loaded data. The resulting weights are normalized to sum exactly to 1.0 and inserted as `liability_determinations` (version 1). `status → "determined"`.

### Phase 5 — Resolution (or Appeal)

The claim is resolved:

```python
POST /liability/claims/{id}/resolve
{"resolution_note": "Service operator acknowledged...", "resolved_by": "..."}
```

`status → "resolved"`. If the claimant contests the determination:

```python
POST /liability/claims/{id}/appeal
{"appeal_reason": "New revocation evidence...", "appellant_did": "..."}
```

`status → "under_review"`. The cycle repeats; the next determination creates version 2.

### Phase 6 — Regulatory Export

At any point, a compliance officer can request an export:

```bash
GET /liability/compliance/export?export_type=eu_ai_act&execution_id=...
```

The export assembles evidence from all six layers into a jurisdiction-specific PDF — EU AI Act (5 sections), HIPAA (health.* filtered), or SEC (finance-domain). The export is logged in `compliance_exports`.

---

## The Five Build Phases

| Phase | Scope | Acceptance gate |
|-------|-------|----------------|
| 1 — Snapshots | Migration 007, snapshot service, sync wiring into Layer 5 | Auto-snapshot on execution; step trust states captured |
| 2 — Dispute Protocol | Claim filing, 8-source evidence gathering, resolve/appeal | Filing returns 201; duplicate returns 409; all 8 evidence sources attached |
| 3 — Attribution Engine | 11-factor algorithm, normalization, confidence | Weights sum to 1.0; revoked-service factor and critical-mismatch factor verified |
| 4 — Compliance Export | EU AI Act, HIPAA, SEC PDF generation | Valid PDF for EU AI Act; 400 for HIPAA without health.* scope |
| 5 — Hardening | Rate limits, Redis cache, load test | p95 < 200ms @ 100 concurrent snapshot reads |

---

## Five Canonical Interview Questions

### Q1: Why are liability snapshots created synchronously rather than in a background task?

Trust scores in the `services` table are rolling aggregates, recomputed by crawl cycles that run every few minutes. A background task for snapshot creation could execute after a crawl overwrites `services.trust_score` for one or more services involved in the execution. The resulting snapshot would reflect a trust state that never existed at the moment of the execution report.

Synchronous creation — before the 201 response returns — guarantees that the snapshot contains the exact trust state the agent experienced when the workflow ran. This is the "evidence window" principle: the window is open only until the next crawl. Layer 6 captures evidence before the window closes.

### Q2: How does the attribution engine prevent gaming — specifically, how does using an undertrusted service affect the attribution?

`service_trust_below_step_minimum` fires when `trust_score < min_trust_score` for any step in the snapshot. It shifts +0.15 weight to the **agent**, not the service. Similarly, `service_trust_tier_below_step_minimum` shifts +0.20 to the agent.

This is the anti-gaming property: if an agent deliberately uses an undertrusted service (to reduce cost, bypass restrictions, or access a permissive service), the attribution engine shifts responsibility toward the agent. The service accurately represented its trust tier; the agent chose to use a service that didn't meet the step's requirements. The decision to proceed belonged to the agent.

### Q3: What is the relationship between the Layer 4 HMAC commitment and Layer 6 evidence?

When Layer 4 processes a high-sensitivity field as a committed field, it stores a `commitment_hash` in `context_disclosures` and doesn't transmit the field value to the service. The service receives only the hash — it can verify the field was committed but cannot see its value.

In Layer 6 evidence gathering, `_gather_context_disclosures()` captures `fields_committed` from each `context_disclosures` row. For GDPR-erased disclosures (`erased=True`), even this list is removed — only the tombstone summary remains. The HMAC commitment protects PII during the workflow; Layer 6 preserves the evidence that a commitment occurred without re-exposing the committed value.

### Q4: How does Layer 6 know which service handled each workflow step if the workflow step has no pinned service?

The `_load_step_trust_states()` function in `liability_snapshot.py` uses a `LATERAL` subquery to resolve unpinned steps. For each step where `ws.service_id IS NULL`, it queries `context_disclosures` for the most recent disclosure in the 35+5 minute window that matches `agent_did` and `ontology_tag`. The `service_id` from that disclosure row is used as the step's resolved service.

This means the snapshot reflects which service *actually* received context for each step — not which service the workflow author recommended. If the agent used a different service than the step's preferred candidate (e.g., because the preferred service was unavailable), the snapshot captures the actual service used, making attribution accurate.

### Q5: What happens if a service is revoked after a workflow execution but before a claim is filed — who bears responsibility?

`service_revoked_after_execution_for_related_reason` fires when a revocation event exists for a service in the execution, where `revoked_at > execution.reported_at`, AND the revocation reason code contains keywords matching the claim type (e.g., a `service_failure` claim + a `capability`-related revocation reason).

This factor shifts +0.15 to the **service** — not the agent. The agent ran the workflow in good faith when the service was still in good standing. The revocation happening afterward (for a related reason) is evidence that the service had a systematic problem that eventually led to its revocation. The agent can't be responsible for a problem that wasn't visible until after the fact.

The timing distinction is architecturally significant: `service_revoked_before_execution` (+0.25 → agent) vs. `service_revoked_after_execution_for_related_reason` (+0.15 → service). The direction of weight shift is opposite depending on whether the revocation was knowable at execution time.

---

## The Complete Six-Layer Picture

Each layer in AgentLedger answers a different accountability question:

| Layer | Name | Question answered |
|-------|------|-----------------|
| 1 | Manifest Registry | "What can this service do?" |
| 2 | Identity & Credentials | "Who is this agent?" |
| 3 | Trust & Verification | "Can this service be trusted?" |
| 4 | Context Matching & Disclosure | "What context am I sharing, and with whom?" |
| 5 | Workflow Registry & Quality Signals | "Does this workflow produce reliable outcomes?" |
| 6 | Liability & Attribution | "When something goes wrong, who is responsible?" |

Each layer builds on the ones before it:

- Layer 6 attribution uses Layer 5 workflow quality scores to identify low-quality workflows
- Layer 6 snapshots capture Layer 3 trust scores at execution time before they change
- Layer 6 compliance exports include Layer 4 context disclosures and Layer 2 session assertions
- Layer 6 evidence gathering pulls Layer 1 manifest records to establish what capabilities a service claimed at execution time

---

## The Six-Layer Invariant Set

| Layer | Invariant |
|-------|----------|
| 1 | A manifest is only discoverable if it passed DNS verification |
| 2 | An agent can only use an API key if its DID is active and not revoked |
| 3 | A service reaches trust tier 4 only with ≥2 confirmed attestations from ≥2 independent organizations |
| 4 | A context field is only disclosed if the agent's profile permits it for the requesting service |
| 5 | A workflow quality score above 70.0 requires verified executions with Layer 4 audit trail evidence |
| 6 | Every workflow execution produces a frozen trust snapshot before the 201 response returns; no claim is possible without it |

These six invariants form the trust contract of the AgentLedger infrastructure stack.

---

## Exercise 1 — Trace the Evidence Chain

For a single workflow execution, identify which records in each of the six layers contribute to a potential Layer 6 attribution determination:

| Layer | Relevant records |
|-------|----------------|
| L1 | `manifests` — service capability declarations at execution time |
| L2 | `session_assertions` — agent identity authorization records |
| L3 | `chain_events` (attestation, revocation) — trust state audit trail |
| L4 | `context_disclosures`, `context_mismatch_events` — what was shared and flagged |
| L5 | `workflow_executions`, `workflow_validations`, `workflow_context_bundles` |
| L6 | `liability_snapshots`, `liability_evidence`, `liability_determinations` |

---

## Exercise 2 — Full Lifecycle from the CLI

Run the complete Layer 6 lifecycle from execution report to resolved claim:

```bash
# 1. Report an execution (creates snapshot automatically)
EXEC=$(curl -s -X POST "http://localhost:8000/v1/workflows/$WF_ID/executions" \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{"agent_did":"did:key:z6MkTestContextAgent","outcome":"failure","steps_completed":1,"steps_total":2}')
EXECUTION_ID=$(echo $EXEC | python -c "import sys,json; print(json.load(sys.stdin)['execution_id'])")

# 2. Retrieve snapshot
curl -s "http://localhost:8000/v1/liability/snapshots/$EXECUTION_ID" -H "X-API-Key: dev-local-only" | \
  python -c "import sys,json; d=json.load(sys.stdin); print('quality_score:', d['workflow_quality_score'])"

# 3. File claim
CLAIM=$(curl -s -X POST "http://localhost:8000/v1/liability/claims" \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d "{\"execution_id\":\"$EXECUTION_ID\",\"claimant_did\":\"did:key:z6MkTestContextAgent\",\"claim_type\":\"wrong_outcome\",\"description\":\"Workflow failed at step 1 with no fallback triggered\"}")
CLAIM_ID=$(echo $CLAIM | python -c "import sys,json; print(json.load(sys.stdin)['claim_id'])")

# 4. Gather evidence
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/gather" -H "X-API-Key: dev-local-only" | \
  python -c "import sys,json; d=json.load(sys.stdin); print('evidence_count:', d.get('evidence_count', '?'))"

# 5. Determine
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/determine" \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{"determined_by":"did:key:z6MkTestReviewer"}' | \
  python -c "import sys,json; det=json.load(sys.stdin).get('determination',{}); print(f'agent={det.get(\"agent_weight\",0):.2f} service={det.get(\"service_weight\",0):.2f} author={det.get(\"workflow_author_weight\",0):.2f} validator={det.get(\"validator_weight\",0):.2f}')"

# 6. Resolve
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/resolve" \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{"resolution_note":"Attribution determined. Workflow author notified of missing fallback logic.","resolved_by":"did:key:z6MkTestReviewer"}' | \
  python -c "import sys,json; print('final status:', json.load(sys.stdin)['status'])"
```

---

## Curriculum Completion: The Full AgentLedger Stack

With Lesson 60, the 60-lesson curriculum for AgentLedger is complete. The stack spans six layers, 60 lessons, and approximately 50–70 hours of study time.

**What you can now do:**

- Explain, trace, debug, and extend all six infrastructure layers
- Answer the design questions behind each layer's architecture (why blockchain for trust, why sync snapshot creation, why the unverifiable cap at 70.0)
- Run and interpret end-to-end workflows from manifest registration through liability determination
- Describe the complete accountability chain: who registered the service (L1), who ran the workflow as a verified agent (L2), what was the trust state at execution (L3), what context was shared (L4), did the workflow perform reliably (L5), and who was responsible when something went wrong (L6)

---

## Key Takeaways

- Layer 6 invariant: every execution produces a frozen snapshot before the 201 response; no claim is possible without it
- Full lifecycle: execution → snapshot (sync) → claim filing → evidence gathering → determination → resolution / appeal
- Five build phases, 10 acceptance criteria, 346 tests (all passing at completion)
- Attribution gaming is self-defeating: using an undertrusted service shifts weight to the agent
- The six layers form a complete accountability stack — each answers a different question about who did what, whether it was trustworthy, and who bears responsibility

---

## Congratulations

You have completed all 60 lessons of the AgentLedger curriculum. You now have a comprehensive understanding of how trust infrastructure for autonomous agents is built — from manifest discovery through cryptographic identity, blockchain-anchored trust verification, privacy-preserving context matching, workflow quality signals, and accountability attribution. This is the foundation for the autonomous agent web.
