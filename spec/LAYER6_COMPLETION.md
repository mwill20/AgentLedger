# AgentLedger - Layer 6 Completion Summary

**For:** Architect sign-off and v0.1.0 release
**Date:** April 2026
**Branch:** `main` (synced with `origin/main`)
**Final commit:** `d47138b` - "feat: complete Layer 6 liability infrastructure"

---

## 1. What Was Built

Layer 6 is the **Liability layer** - the accountability and evidence infrastructure
that closes the trust loop across all six layers of the AgentLedger stack.

| Capability | Description |
|---|---|
| **Liability Snapshots** | Point-in-time captures of trust state at workflow execution time, created synchronously before the 201 response returns - preventing trust score decay from corrupting the evidentiary record |
| **Dispute Protocol** | Structured claims lifecycle: file -> gather evidence -> determine -> resolve / appeal |
| **Attribution Engine** | 11-factor algorithm computing responsibility weights across four actors (agent, service, workflow author, validator); weights always sum to 1.0 |
| **Regulatory Compliance Export** | EU AI Act, HIPAA, and SEC-ready PDF packages built from evidence across all six layers |

Layer 6 does **not** include: licensed insurance underwriting, binding legal rulings,
payment settlement, smart contract escrow execution, or governance organization
structure. It is evidence infrastructure - the data layer that enables those things.

---

## 2. New API Surface

Base URL: `http://localhost:8000/v1/liability`

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/liability/snapshots/{execution_id}` | Retrieve trust state snapshot for a workflow execution |
| `GET` | `/liability/snapshots` | Admin: list all snapshots with filters |
| `POST` | `/liability/claims` | File a liability claim against an execution |
| `GET` | `/liability/claims/{claim_id}` | Retrieve claim with evidence and determination |
| `POST` | `/liability/claims/{claim_id}/gather` | Manually trigger evidence gathering |
| `POST` | `/liability/claims/{claim_id}/determine` | Compute attribution weights |
| `POST` | `/liability/claims/{claim_id}/resolve` | Close a claim with resolution note |
| `POST` | `/liability/claims/{claim_id}/appeal` | Contest a determination |
| `GET` | `/liability/compliance/export` | Generate EU AI Act / HIPAA / SEC PDF export |

---

## 3. Database Schema - New Tables

Migration: `007_layer6_liability.py`

| Table | Purpose |
|---|---|
| `liability_snapshots` | Append-only point-in-time trust state per execution |
| `liability_claims` | Dispute claims with lifecycle status tracking |
| `liability_evidence` | Evidence items from Layers 1-5, gathered per claim |
| `liability_determinations` | Attribution weight records, versioned for appeals |
| `compliance_exports` | Audit log of all regulatory exports generated |

---

## 4. Attribution Engine

11 factors across 4 actors. Weights start at 25/25/25/25 and shift from evidence.

**Factors that increase agent weight:**
- `service_trust_below_step_minimum` (+0.15)
- `service_trust_tier_below_step_minimum` (+0.20)
- `service_revoked_before_execution` (+0.25)
- `critical_context_mismatch_ignored` (+0.20)

**Factors that increase service weight:**
- `service_capability_not_verified` (+0.15)
- `service_context_over_request` (+0.20)
- `service_revoked_after_execution_for_related_reason` (+0.15)

**Factors that increase workflow author weight:**
- `workflow_quality_score_low_at_execution` (+0.10)
- `workflow_trust_threshold_inadequate` (+0.15)
- `workflow_no_fallback_for_critical_step` (+0.10)

**Factors that increase validator weight:**
- `validator_approved_inadequate_trust_threshold` (+0.10)
- `validator_approved_non_minimal_context` (+0.10)

Weights floor at 0.0. Normalized to sum exactly to 1.0.
Confidence: `min(1.0, 0.3 + (factors_applied * 0.1))`

---

## 5. Key Design Decisions

| Decision | Rationale |
|---|---|
| Snapshots are synchronous (not async) | Trust scores in the `services` table are overwritten by crawl cycles. Sync creation captures state before the 201 response returns - closing the timing attack window |
| Attribution gaming is self-defeating | Using an undertrusted service shifts weight TO the agent, not away from them |
| Evidence records copy raw data at gather time | Source records can be modified or GDPR-erased after the claim is filed |
| GDPR-erased disclosures produce tombstone evidence records | The disclosure happened - its absence would mislead attribution |
| Layer 6 is evidence infrastructure, not adjudication | Avoids regulated financial/legal entity classification |

---

## 6. Build Phases Completed

| Phase | Scope | Status |
|---|---|---|
| 1 - Snapshots | Migration 007, snapshot service, sync wiring into L5 executor | Done |
| 2 - Dispute Protocol | Claim filing, 8-source evidence gathering, resolve/appeal | Done |
| 3 - Attribution Engine | 11-factor algorithm, all scenarios verified | Done |
| 4 - Compliance Export | EU AI Act, HIPAA, SEC PDF generation with scope validation | Done |
| 5 - Hardening | 10 claim/hour rate limit, Redis claim status cache, p95 load test | Done |

---

## 7. Acceptance Criteria - All Verified

| # | Criterion | Result |
|---|---|---|
| 1 | POST /workflows/{id}/executions auto-creates liability snapshot | Passed |
| 2 | Snapshot step_trust_states captures trust_score/trust_tier per step | Passed |
| 3 | POST /liability/claims returns 201; duplicate returns 409 | Passed |
| 4 | Evidence gathering populates records from all 8 sources | Passed |
| 5 | Attribution weights sum to 1.0 | Passed |
| 6 | Revoked service increases service_weight | Passed |
| 7 | Critical mismatch increases agent_weight | Passed |
| 8 | EU AI Act export returns valid PDF | Passed |
| 9 | HIPAA export returns 400 when no health.* tags in scope | Passed |
| 10 | GET /liability/snapshots/{id} p95 < 200ms @ 100 concurrent | Passed |

---

## 8. Test Coverage

- **Layer 6 test files:** `test_liability_snapshot.py`, `test_liability_claims.py`,
  `test_liability_attribution.py`, `test_liability_compliance.py`
- **Full suite at Layer 6 completion:** 346 passed, 0 failures
- **Non-blocking warnings:** ReportLab Python 3.14 deprecation, pytest cache
  write permission (local environment only)

---

## 9. Layer 5 Integration Points Activated

| # | Integration Point | What Layer 6 Added |
|---|---|---|
| 1 | `workflow_executions` | Synchronous snapshot creation wired into executor write path |
| 2 | `workflow_context_bundles` + L4 `context_disclosures` | Combined into evidence package per claim |
| 3 | `workflows.quality_score` | Captured in snapshot; feeds quality_score_low attribution factor |
| 4 | `workflow_validations.validator_did` | Named actor in attribution determination |
| 5 | `workflow_steps.service_id` + L3 revocation | Revocation timing drives service_revoked attribution factors |

---

## 10. What Layer 6 Does NOT Include

These are explicitly deferred by design:

| Item | Status |
|---|---|
| Licensed insurance underwriting | Out of scope for v0.1 - requires licensed insurers |
| Binding legal determinations | Out of scope - Layer 6 produces evidence, not rulings |
| Payment settlement for claims | Out of scope - requires payment rails |
| Smart contract escrow | Deferred - requires Layer 3 blockchain deployment |
| AgentLedger governance framework | Organizational, not code |

---

*Canonical spec: `spec/LAYER6_SPEC.md`*
*This completion summary is a point-in-time snapshot for release. The spec remains the source of truth.*
