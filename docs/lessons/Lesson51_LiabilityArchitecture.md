# Lesson 51: The Accountability Engine — Layer 6 Architecture & Design Principles

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `spec/LAYER6_SPEC.md`, `api/services/liability_snapshot.py`, `api/services/liability_claims.py`, `api/services/liability_attribution.py`, `api/services/liability_compliance.py`, `db/migrations/versions/007_layer6_liability.py`
**Prerequisites:** Lessons 41–50 (especially Lesson 48 — execution reporting, and Lesson 50 — Layer 5 integration points)
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

When a building contractor finishes a project, there is a paper trail: permits, inspection records, material certificates, and a chain of signed approvals. If something fails, this trail answers "who knew what, when, and what did they approve?" Layer 6 is that paper trail — but built into the AgentLedger infrastructure itself, assembled automatically from the evidence that Layers 1–5 produce.

Layer 6 does four things: it captures trust state at the moment each workflow executes (before that state can change), provides a structured process for filing and investigating disputes, computes a principled attribution of responsibility across the four actors in every agent transaction, and packages all of it into jurisdiction-specific regulatory export formats.

This lesson maps the architecture — the five tables, four services, nine endpoints, and the single design principle that governs all of them.

---

## Learning Objectives

By the end of this lesson you will be able to:

- State the Layer 6 design principle in one sentence and explain why it has legal significance
- Name the four capabilities Layer 6 provides and the four things it explicitly does not provide
- Explain the liability snapshot problem and why synchronous creation is the solution
- Map each Layer 5 integration point to the Layer 6 component that consumes it
- Name all five Layer 6 tables and their purpose
- Describe the five build phases and what each phase delivered

---

## The Design Principle: Evidence, Not Judgment

> **Layer 6 produces attribution weights and evidence packages. It does not make legal determinations.**

This distinction is both architectural and legal. If Layer 6 issued binding determinations, it would become a regulated financial or legal adjudicator — requiring licensure, oversight, and a governance structure that is out of scope for infrastructure software. By producing structured evidence ("based on available data, the agent bears 45% of attribution, the service bears 40%, the author bears 10%, the validator bears 5%"), Layer 6 remains infrastructure. Human decision-makers — insurers, lawyers, regulators — use this evidence to make the actual determinations.

Every design decision in Layer 6 flows from this principle. The attribution engine computes weights, not verdicts. The compliance exports are evidence packages, not regulatory filings. The dispute protocol is a structured evidence-gathering process, not an arbitration panel.

---

## The Four Capabilities

| Capability | What it does | What it does NOT do |
|-----------|-------------|---------------------|
| Liability Snapshots | Captures point-in-time trust state at execution time, before trust scores change | Does not compute attribution or determine responsibility |
| Dispute Protocol | Structured claims lifecycle: file → gather evidence → determine → resolve/appeal | Does not adjudicate; does not involve payment settlement |
| Attribution Engine | Computes responsibility weights across four actors from structured evidence | Does not issue binding rulings; does not process insurance claims |
| Regulatory Compliance Export | Generates EU AI Act, HIPAA, and SEC-ready PDF evidence packages | Does not submit reports to regulators; does not guarantee compliance |

---

## The Liability Snapshot Problem

Trust scores in the `services` table are rolling aggregates, overwritten whenever the Layer 3 crawler runs a new trust recomputation. A service with `trust_score=91.2` today may have had `trust_score=62.0` at the time of a disputed execution three weeks ago. Liability attribution must reference the trust state *at execution time*, not the current state.

Layer 3 attestation events are immutable on-chain — but the computed `trust_score` in the `services` table is not. Layer 6 solves this by capturing a snapshot of every relevant actor's state at the moment each workflow execution is reported.

**Why synchronous, not async?** The Layer 5 execution endpoint (`POST /workflows/{id}/executions`) wires `create_snapshot()` synchronously, before the 201 response returns. If snapshot creation ran as a background task, a crawl cycle could overwrite the `services` table before the snapshot executed — the evidence window closes in minutes. Synchronous creation guarantees the snapshot is committed before the response goes back to the caller.

---

## The Five Tables

Migration `007_layer6_liability.py` adds five tables to the existing schema:

| Table | Records | Key constraint |
|-------|---------|---------------|
| `liability_snapshots` | One per workflow execution | `UNIQUE (execution_id)` — one snapshot per execution, append-only |
| `liability_claims` | One per dispute | Status lifecycle (filed → evidence_gathered → under_review → determined → resolved/appealed) |
| `liability_evidence` | Many per claim | 8 source types; raw data copied at gather time; never deleted |
| `liability_determinations` | One or more per claim | Versioned for appeals (`determination_version` increments); must sum to 1.0 |
| `compliance_exports` | One per export request | Audit log of all regulatory exports generated |

All tables are append-only from a business logic standpoint: evidence is never deleted, snapshots are never modified, and determination versions are never overwritten (appeals create new version records).

---

## The Four Services

| Service | File | Primary responsibility |
|---------|------|----------------------|
| `liability_snapshot.py` | `api/services/liability_snapshot.py` | Synchronous trust state capture; read path for snapshot retrieval |
| `liability_claims.py` | `api/services/liability_claims.py` | Claim filing, 8-source evidence gathering, status transitions |
| `liability_attribution.py` | `api/services/liability_attribution.py` | 11-factor attribution weight computation, normalization |
| `liability_compliance.py` | `api/services/liability_compliance.py` | EU AI Act, HIPAA, SEC PDF generation via ReportLab |

The four services have a strict dependency hierarchy. Attribution reads from claims; compliance reads from both claims and determinations. The snapshot service is independent and wired directly into the Layer 5 executor.

---

## The Nine Endpoints

All Layer 6 endpoints are mounted at `/v1/liability`:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/liability/snapshots/{execution_id}` | Retrieve the trust state snapshot for a specific execution |
| `GET` | `/liability/snapshots` | Admin: list all snapshots with filters |
| `POST` | `/liability/claims` | File a liability claim against an execution |
| `GET` | `/liability/claims/{claim_id}` | Retrieve claim with all evidence and determination |
| `POST` | `/liability/claims/{claim_id}/gather` | Manually trigger evidence gathering |
| `POST` | `/liability/claims/{claim_id}/determine` | Compute attribution weights from gathered evidence |
| `POST` | `/liability/claims/{claim_id}/resolve` | Close a claim with a resolution note |
| `POST` | `/liability/claims/{claim_id}/appeal` | Contest a determination, returning to `under_review` |
| `GET` | `/liability/compliance/export` | Generate EU AI Act / HIPAA / SEC compliance PDF |

The five claim lifecycle endpoints mirror the status state machine — each endpoint corresponds to a specific status transition.

---

## Layer 5 Integration Points

Layer 6 consumes all five Layer 5 handoff points identified in Lesson 50:

| Layer 5 Surface | What Layer 6 builds on it |
|----------------|---------------------------|
| `workflow_executions.workflow_id + agent_did + outcome + failure_step_number` | Liability snapshot captures all four as the evidentiary anchor |
| `workflow_context_bundles.id` + Layer 4 `context_disclosures` | Combined into evidence records during evidence gathering |
| `workflows.quality_score` | Captured in snapshot; drives `workflow_quality_score_low_at_execution` attribution factor |
| `workflow_validations.validator_did` | Named as a distinct actor in attribution determination |
| `workflow_steps.service_id` + Layer 3 `attestation_records` | Revocation timing relative to execution drives two attribution factors |

---

## The Five Build Phases

| Phase | Scope | Key deliverable |
|-------|-------|----------------|
| 1 — Snapshots | Migration 007, snapshot service, synchronous wiring into Layer 5 executor | AC 1–2: auto-snapshot on execution, step trust states captured |
| 2 — Dispute Protocol | Claim filing, 8-source evidence gathering, resolve/appeal | AC 3–4: filing, deduplication, all 8 evidence sources |
| 3 — Attribution Engine | 11-factor algorithm, normalization, confidence | AC 5–7: weights sum to 1.0, revoked service and mismatch factors verified |
| 4 — Compliance Export | EU AI Act, HIPAA, SEC PDF generation | AC 8–9: valid PDF for EU AI Act; 400 for HIPAA without health.* scope |
| 5 — Hardening | Rate limits, Redis claim status cache, load test | AC 10: p95 < 200ms @ 100 concurrent snapshot reads |

---

## Cross-Layer Evidence Sources

Layer 6 evidence gathering queries eight distinct source tables from Layers 1–5:

| Evidence type | Source table | Source layer |
|--------------|-------------|-------------|
| `workflow_execution` | `workflow_executions` | Layer 5 |
| `context_disclosure` | `context_disclosures` | Layer 4 |
| `context_mismatch` | `context_mismatch_events` | Layer 4 |
| `trust_attestation` | Layer 3 chain events (attestations) | Layer 3 |
| `trust_revocation` | Layer 3 chain events (revocations) | Layer 3 |
| `manifest_version` | `manifests` | Layer 1 |
| `validation_record` | `workflow_validations` | Layer 5 |
| `crawl_event` | `crawl_events` | Layer 1 |

This cross-layer evidence aggregation is what makes Layer 6 possible: without Layers 1–5 generating structured, queryable evidence records, there would be nothing to gather.

---

## What Layer 6 Does Not Include

These are architectural exclusions, not deferrals:

| Excluded capability | Why it's excluded |
|--------------------|------------------|
| Insurance underwriting | Requires licensed insurers; regulatory, not code |
| Binding legal determinations | Would make Layer 6 a regulated adjudicator |
| Payment settlement | Requires payment rails out of scope for v0.1 |
| Smart contract escrow | Requires Layer 3 blockchain deployment (currently Amoy testnet) |
| AgentLedger governance structure | Organizational, not code |

---

## Exercise 1 — Map the Dependency Chain

Trace the data that flows into a single attribution determination. Start from `workflow_executions` (Layer 5) and identify all tables that must be queried before `liability_determinations` can be written.

**Expected path:**
```
workflow_executions (L5)
  → liability_snapshots (L6) — captures trust state at execution time
  → liability_claims (L6) — dispute filed against execution_id
  → liability_evidence (L6) — 8 sources gathered from L1-L5
  → liability_determinations (L6) — 11 factors applied to evidence
```

---

## Exercise 2 — Identify the Evidence Window

A workflow execution is reported at 14:00 UTC. A crawl cycle updates the `services` table at 14:05 UTC. A dispute is filed at 14:30 UTC and evidence is gathered at 14:35 UTC.

**Question:** At what point does the evidence window close, and how does Layer 6 prevent stale trust state from corrupting the attribution?

*(Answer: The evidence window closes at 14:05 UTC when the crawl cycle overwrites `services.trust_score`. The snapshot — created synchronously at 14:00 UTC before the 201 response returns — captures the exact trust state. By 14:35 UTC when evidence is gathered, the current `services.trust_score` may differ; but the `liability_snapshots.step_trust_states` JSONB column preserves the 14:00 UTC values regardless.)*

---

## Best Practices

**Snapshots are write-once, never updated.** The `_load_existing_snapshot()` check at the top of `create_snapshot()` returns early if a snapshot already exists for a given `execution_id`. This idempotency guard prevents duplicate snapshots from inconsistent retry behavior in the Layer 5 executor, while ensuring the first-captured state is authoritative.

**Evidence raw_data is a snapshot, not a reference.** When evidence is gathered, the relevant fields from each source record are copied into `liability_evidence.raw_data` as JSONB. This protects against GDPR erasure, record deletion, or field updates between gather time and determination time. The evidence record is a forensic copy.

---

## Interview Q&A

**Q: Why does Layer 6 create liability snapshots synchronously rather than in a background task?**
A: The `services` table is a mutable rolling aggregate updated by crawl cycles. A background task might execute after a crawl overwrites the trust scores for services involved in the execution — capturing state that never existed at execution time. Synchronous creation before the 201 response returns guarantees the snapshot reflects the exact trust state at the moment of the execution report.

**Q: Why is Layer 6 designed as "evidence infrastructure" rather than an adjudication system?**
A: If Layer 6 issued binding liability determinations, it would become a regulated financial or legal entity requiring licensure and governance structures beyond what infrastructure software can provide. By producing structured attribution weights as evidence for human decision-makers (insurers, lawyers, regulators), Layer 6 remains infrastructure that enables accountability without assuming the legal obligations of an adjudicator.

**Q: What happens if all five Layer 6 tables are empty for a given execution?**
A: A snapshot is created automatically when the execution is reported — the first table (`liability_snapshots`) is always populated. The remaining four tables (`liability_claims`, `liability_evidence`, `liability_determinations`, `compliance_exports`) are only created when a dispute is filed. Most executions never have a claim filed against them. The snapshot exists as a preserved evidentiary record regardless.

---

## Key Takeaways

- Design principle: evidence infrastructure, not adjudication — avoids regulated entity classification
- Four capabilities: snapshots, dispute protocol, attribution engine, compliance export
- Five tables: snapshots (append-only, one per execution), claims, evidence, determinations (versioned), compliance export audit log
- Snapshot creation is synchronous — closes the trust-score-mutation evidence window
- Nine endpoints mirror the five claim lifecycle status transitions
- 8 evidence source types span all five prior layers

---

## Next Lesson

**Lesson 52 — The Snapshot in Time: Liability Snapshot Creation & Read Path** traces `create_snapshot()` through its seven SQL queries, explains the `step_trust_states` JSONB structure, and shows how the snapshot is retrieved and used as the evidentiary anchor for attribution.
