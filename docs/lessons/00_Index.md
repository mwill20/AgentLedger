# AgentLedger — Lesson Index (Layers 1, 2, 3, 4, 5 & 6)

**Project:** AgentLedger — Trust & Discovery Infrastructure for the Autonomous Agent Web
**Layers covered:** Layer 1 — Manifest Registry · Layer 2 — Identity & Credentials · Layer 3 — Trust & Verification · Layer 4 — Context Matching & Selective Disclosure · Layer 5 — Workflow Registry & Quality Signals · Layer 6 — Liability, Attribution & Regulatory Compliance
**Total Lessons:** 60
**Estimated Total Time:** 60–84 hours
**Prerequisites:** Basic Python, basic SQL, basic understanding of REST APIs

---

## Curriculum Map

### Layer 1 — Manifest Registry (Lessons 01–10)

```
  Lesson 00   Lesson 01   Lesson 02   Lesson 03   Lesson 04
  Index       Big Picture  Database    Config &    Data Models
              & Stack      Schema      Dependencies
     |           |            |            |            |
     v           v            v            v            v
  Lesson 05   Lesson 06   Lesson 07   Lesson 08   Lesson 09
  Registry    Query &     Crawler &   Hardening   Testing &
  (Ingest)    Search      Verification             Load Test
     |           |            |            |            |
     v           v            v            v            v
                        Lesson 10
                        Architecture
                        Deep Dive
```

### Layer 2 — Identity & Credentials (Lessons 11–20)
> **Prerequisites:** Complete Lesson 01. No cryptography background required — Lesson 11 covers the math from scratch.

```
  Lesson 11    Lesson 12    Lesson 13    Lesson 14    Lesson 15
  Crypto       DID Methods  Credential   Agent        Session
  Foundations               Issuance     Identity     Assertions
      |            |             |             |           |
      v            v             v             v           v
  Lesson 16    Lesson 17    Lesson 18    Lesson 19    Lesson 20
  Service      Human-in-   Data Models  Background   Full Flow &
  Identity     the-Loop    & API Routes Workers      Interview
```

### Layer 3 — Trust & Verification (Lessons 21–30)
> **Prerequisites:** Complete Lessons 01 and 11. Basic blockchain concepts (transaction, event, hash) are helpful but covered in Lesson 21.

```
  Lesson 21    Lesson 22    Lesson 23    Lesson 24    Lesson 25
  Contracts    Chain         Auditor      Attestation  Trust
  & Rationale  Abstraction   Network      Pipeline     Scoring
      |            |             |             |           |
      v            v             v             v           v
  Lesson 26    Lesson 27    Lesson 28    Lesson 29    Lesson 30
  Merkle       Federation   Background   Live Amoy    Hardening &
  Audit Chain  & Blocklist  Workers      Acceptance   Interview
```

### Layer 4 — Context Matching & Selective Disclosure (Lessons 31–40)
> **Prerequisites:** Complete Lessons 01, 11, and 21. Layer 4 builds on the session assertions from Lesson 15 and trust tiers from Lesson 25.

```
  Lesson 31    Lesson 32    Lesson 33    Lesson 34    Lesson 35
  Context      Context      Mismatch     Matching     Trust
  Architecture Profiles     Detection    Engine       Gates
      |            |             |             |           |
      v            v             v             v           v
  Lesson 36    Lesson 37    Lesson 38    Lesson 39    Lesson 40
  HMAC         Selective    Audit        Compliance   Hardening &
  Commitment   Disclosure   Trail        Export       Interview
```

### Layer 5 — Workflow Registry & Quality Signals (Lessons 41–50)
> **Prerequisites:** Complete Lessons 01, 11, 21, and 31. Layer 5 builds on Layer 3 trust tiers (Lesson 25) and Layer 4 context matching (Lesson 34).

```
  Lesson 41    Lesson 42    Lesson 43    Lesson 44    Lesson 45
  Workflow     Workflow     CRUD &       Validation   Quality
  Architecture Spec         Caching      Queue        Score
      |            |             |             |           |
      v            v             v             v           v
  Lesson 46    Lesson 47    Lesson 48    Lesson 49    Lesson 50
  Ranking      Context      Execution    Hardening    Final
  Engine       Bundle       Feedback     & Threats    Debrief
```

### Layer 6 — Liability, Attribution & Regulatory Compliance (Lessons 51–60)
> **Prerequisites:** Complete Lessons 01, 11, 21, 31, and 41. Layer 6 closes the accountability loop using evidence from all five prior layers.

```
  Lesson 51    Lesson 52    Lesson 53    Lesson 54    Lesson 55
  Liability    Snapshot     Claim        Attribution  Compliance
  Architecture Creation     Filing       Engine       Export
      |            |             |             |           |
      v            v             v             v           v
  Lesson 56    Lesson 57    Lesson 58    Lesson 59    Lesson 60
  Claims       Data Models  Hardening    Schema &     Final
  Lifecycle    & Routes     & Load Test  Migration    Debrief
```

---

## Layer 1 Lesson List

| # | Title | Files Covered | Time | Required? |
|---|-------|--------------|------|-----------|
| 00 | **Index** (this file) | — | 5 min | Yes |
| 01 | **The Big Picture** — What AgentLedger Builds and Why | `spec/LAYER1_SPEC.md`, `README.md`, `docker-compose.yml`, `entrypoint.sh` | 45 min | Yes |
| 02 | **The Vault** — Database Schema and Ontology | `db/schema.sql`, `db/seed_ontology.py`, `db/migrations/`, `ontology/v0.1.json` | 60 min | Yes |
| 03 | **Mission Control** — Configuration and Dependencies | `api/config.py`, `api/dependencies.py`, `api/main.py` | 45 min | Yes |
| 04 | **The Blueprints** — Pydantic Data Models and Input Sanitization | `api/models/manifest.py`, `api/models/query.py`, `api/models/service.py`, `api/models/sanitize.py` | 60 min | Yes |
| 05 | **The Filing Cabinet** — Manifest Registration (Ingest) | `api/routers/manifests.py`, `api/services/registry.py` (registration half) | 90 min | Yes |
| 06 | **The Search Engine** — Structured Queries and Semantic Search | `api/services/registry.py` (query half), `api/services/embedder.py`, `api/services/ranker.py`, `api/routers/search.py`, `api/routers/services.py`, `api/routers/ontology.py` | 90 min | Yes |
| 07 | **The Watchdog** — Crawler, DNS Verification, and Trust Tiers | `crawler/worker.py`, `crawler/tasks/crawl.py`, `crawler/tasks/verify_domain.py`, `api/services/verifier.py`, `api/routers/verify.py` | 60 min | Yes |
| 08 | **The Bouncer** — Rate Limiting, Typosquat Detection, and Hardening | `api/ratelimit.py`, `api/services/typosquat.py`, `api/models/sanitize.py` | 60 min | Yes |
| 09 | **The Proving Ground** — Testing and Load Testing | `tests/conftest.py`, `tests/test_api/`, `tests/test_integration/`, `tests/load/locustfile.py` | 60 min | Yes |
| 10 | **The Architect's View** — Full Architecture Deep Dive | All files — end-to-end data flow, design decisions, extension points | 45 min | Optional |

---

## Layer 2 Lesson List

> **Prerequisites for all Layer 2 lessons:** Complete Lesson 01. Lesson 11 introduces cryptographic concepts from scratch.

| # | Title | Files Covered | Time | Required? |
|---|-------|--------------|------|-----------|
| 11 | **The Lock and Key** — Cryptographic Foundations | `api/services/crypto.py`, `tests/test_api/test_crypto.py` | 60 min | Yes |
| 12 | **The Name Badge** — DID Methods (did:key & did:web) | `api/services/did.py`, `spec/LAYER2_SPEC.md` | 60 min | Yes |
| 13 | **The Notary** — Credential Issuance & Verification | `api/services/credentials.py` | 75 min | Yes |
| 14 | **The Enrollment Office** — Agent Identity Registration & Revocation | `api/services/identity.py`, `db/migrations/versions/002_layer2_identity.py` | 90 min | Yes |
| 15 | **The Day Pass** — Session Assertions | `api/services/sessions.py`, `db/migrations/versions/003_layer2_sessions.py` | 90 min | Yes |
| 16 | **The Business Card** — Service Identity & did:web Activation | `api/services/service_identity.py` | 75 min | Yes |
| 17 | **The Approval Desk** — Human-in-the-Loop Authorization | `api/services/authorization.py`, `tests/test_api/test_authorization.py` | 75 min | Yes |
| 18 | **The Forms** — Data Models & API Routes | `api/models/identity.py`, `api/routers/identity.py` | 60 min | Yes |
| 19 | **The Night Shift** — Background Workers & Redis Patterns | `crawler/tasks/expire_identity_records.py`, `crawler/tasks/revalidate_service_identity.py` | 60 min | Yes |
| 20 | **The Final Debrief** — Full Layer 2 Flow & Interview Readiness | `spec/LAYER2_COMPLETION.md`, all Layer 2 files | 90 min | Yes |

---

## Layer 3 Lesson List

> **Prerequisites for all Layer 3 lessons:** Complete Lessons 01 and 11. Basic blockchain concepts are covered in Lesson 21.

| # | Title | Files Covered | Time | Required? |
|---|-------|--------------|------|-----------|
| 21 | **The Notary's Seal** — Layer 3 Overview & Why Blockchain | `contracts/AttestationLedger.sol`, `contracts/AuditChain.sol`, `spec/LAYER3_SPEC.md`, `spec/LAYER3_COMPLETION.md` | 90 min | Yes |
| 22 | **The Switchboard** — Chain Abstraction Layer | `api/services/chain.py`, `api/config.py`, `db/migrations/versions/004_layer3_trust_verification.py` | 75 min | Yes |
| 23 | **The Badge Office** — Auditor Registration & Credentialing | `api/services/auditor.py`, `api/services/attestation.py` (`_scope_allows`), `api/models/layer3.py` | 60 min | Yes |
| 24 | **The Stamp of Approval** — The Attestation Pipeline | `api/services/attestation.py`, `api/routers/attestation.py`, `api/models/layer3.py` | 75 min | Yes |
| 25 | **The Ledger of Trust** — Trust Tier 4 & Scoring Engine | `api/services/ranker.py`, `api/services/trust.py` | 90 min | Yes |
| 26 | **The Fingerprint File** — Audit Records & Merkle Batching | `api/services/audit.py`, `api/services/merkle.py`, `api/routers/audit.py` | 90 min | Yes |
| 27 | **The Neighborhood Watch** — Federation & Blocklist Distribution | `api/services/federation.py`, `api/services/crypto.py`, `api/routers/federation.py` | 75 min | Yes |
| 28 | **The Night Watchman** — Celery Background Workers | `crawler/tasks/index_chain_events.py`, `crawler/tasks/confirm_chain_events.py`, `crawler/tasks/anchor_audit_batch.py`, `crawler/tasks/push_revocations.py`, `crawler/worker.py` | 60 min | Yes |
| 29 | **The Inspector General** — Live Amoy Acceptance Run | `contracts/scripts/deploy.js`, `contracts/scripts/grant_roles.js`, `spec/LAYER3_COMPLETION.md`, `handoffs/LAYER3_DEPLOYMENT_HANDOFF.md` | 90–120 min | Optional* |
| 30 | **The Audit Examiner** — Hardening, Load Testing & Interview Readiness | `api/services/ranker.py`, `api/services/chain.py`, `api/services/audit.py`, `api/services/federation.py`, `spec/LAYER3_COMPLETION.md` | 90 min | Yes |

> *Lesson 29 requires a funded Polygon Amoy wallet (≥0.05 POL). All other lessons work with `CHAIN_MODE=local` and no testnet tokens.

---

## Layer 4 Lesson List

> **Prerequisites for all Layer 4 lessons:** Complete Lessons 01, 11, and 21. Lessons 15 (session assertions) and 25 (trust tiers) are especially relevant.

| # | Title | Files Covered | Time | Required? |
|---|-------|--------------|------|-----------|
| 31 | **The Privacy Engine** — Layer 4 Overview & Three-Part Invariant | `spec/LAYER4_SPEC.md`, `api/services/context_matcher.py`, `api/services/context_disclosure.py`, `api/routers/context.py`, `db/migrations/versions/005_layer4_context.py` | 75 min | Yes |
| 32 | **The Permission Slip** — Context Profiles & Rule Engine | `api/services/context_profiles.py`, `api/models/context.py` | 75 min | Yes |
| 33 | **The Watchdog** — Mismatch Detection & Escalation | `api/services/context_mismatch.py` | 60 min | Yes |
| 34 | **The Gatekeeper** — The 8-Step Matching Engine | `api/services/context_matcher.py` | 90 min | Yes |
| 35 | **The Trust Ladder** — Trust Gates & Sensitivity Tiers | `api/services/context_matcher.py`, `api/services/context_disclosure.py` | 60 min | Yes |
| 36 | **The Safe Deposit Box** — HMAC Commitment Scheme | `api/services/context_disclosure.py` (lines 34–134), `api/models/context.py` | 75 min | Yes |
| 37 | **The Key Handoff** — Selective Disclosure & Nonce Release | `api/services/context_disclosure.py` (lines 519–704) | 75 min | Yes |
| 38 | **The Paper Trail** — Disclosure Audit History & GDPR Erasure | `api/services/context_disclosure.py` (lines 592–704), `api/routers/context.py` | 60 min | Yes |
| 39 | **The Compliance Dossier** — PDF Export & Regulatory Package | `api/services/context_compliance.py`, `api/routers/context.py` | 60 min | Yes |
| 40 | **The Stress Test** — Hardening, Caching, Rate Limiting & Interview Readiness | `api/services/context_matcher.py`, `api/services/context_profiles.py`, `api/services/context_disclosure.py`, `spec/LAYER4_SPEC.md` | 90 min | Yes |

---

## Layer 5 Lesson List

> **Prerequisites for all Layer 5 lessons:** Complete Lessons 01, 11, 21, and 31. Lessons 25 (trust tiers) and 34 (8-step matching engine) are especially relevant.

| # | Title | Files Covered | Time | Required? |
|---|-------|--------------|------|-----------|
| 41 | **The Registry That Doesn't Execute** — Layer 5 Architecture | `spec/LAYER5_SPEC.md`, `api/services/workflow_registry.py`, `api/services/workflow_ranker.py`, `api/services/workflow_validator.py`, `api/services/workflow_context.py`, `db/migrations/versions/006_layer5_workflows.py` | 75 min | Yes |
| 42 | **The Blueprint** — Workflow Spec Format & Validation Rules | `api/models/workflow.py`, `api/services/workflow_registry.py` (`_validate_workflow_spec`) | 75 min | Yes |
| 43 | **The Filing System** — CRUD, Caching & Rate Limiting | `api/services/workflow_registry.py` (CRUD half), `api/routers/workflows.py` | 75 min | Yes |
| 44 | **The Expert Witness** — Human Validation Queue | `api/services/workflow_validator.py`, `api/models/workflow.py` (checklist) | 60 min | Yes |
| 45 | **The Quality Ledger** — Composite Scoring Engine | `api/services/workflow_ranker.py` (lines 69–161), `api/services/workflow_validator.py` (lines 30–44) | 60 min | Yes |
| 46 | **The Talent Agency** — Per-Step Ranking Engine | `api/services/workflow_ranker.py` (lines 55–411), `api/routers/workflows.py` | 75 min | Yes |
| 47 | **The One-Stop Approval** — Context Bundle Integration | `api/services/workflow_context.py`, `api/models/workflow.py` | 75 min | Yes |
| 48 | **The Feedback Machine** — Execution Outcome Reporting | `api/services/workflow_executor.py` | 60 min | Yes |
| 49 | **The Four Threats** — Anti-Gaming & Hardening | `api/services/workflow_validator.py`, `api/services/workflow_ranker.py`, `api/services/workflow_context.py`, `api/services/workflow_registry.py` | 60 min | Yes |
| 50 | **The Final Debrief** — Full Layer 5 Flow & Interview Readiness | `spec/LAYER5_COMPLETION.md`, all Layer 5 service files | 90 min | Yes |

---

## How to Use These Lessons

1. **Sequential learner:** Go 01 → 10 (Layer 1), then 11 → 20 (Layer 2), then 21 → 30 (Layer 3), then 31 → 40 (Layer 4), then 41 → 50 (Layer 5). Each lesson builds on the previous within a layer.
2. **Interview prep — Layer 1:** Read 01 (overview), 10 (architecture), 05 and 06 (core logic).
3. **Interview prep — Layer 2:** Read 11 (crypto), 14 (agent identity), 15 (sessions), 20 (full flow).
4. **Interview prep — Layer 3:** Read 21 (contracts), 25 (trust scoring), 26 (Merkle), 30 (hardening).
5. **Interview prep — Layer 4:** Read 31 (invariant), 34 (matching engine), 36 (HMAC), 37 (disclose), 40 (hardening).
6. **Interview prep — Layer 5:** Read 41 (architecture), 45 (quality score), 47 (bundle), 49 (threats), 50 (final debrief).
7. **Debugging a specific area:** Jump to the relevant lesson using the tables above.
8. **Contributing:** Read all required lessons for the layer you're modifying, then focus on the module's lesson.

---

## Project Quick Reference

```
AgentLedger/
|-- api/                          # FastAPI application (Python 3.11+)
|   |-- config.py                 # Settings via pydantic-settings
|   |-- dependencies.py           # DB, Redis, auth injection
|   |-- main.py                   # App entry + router mounting
|   |-- ratelimit.py              # Pure ASGI rate-limiting middleware
|   |-- models/                   # Pydantic request/response schemas
|   |   |-- manifest.py           # Layer 1 models
|   |   |-- identity.py           # Layer 2 models (agent, session, authorization)
|   |   |-- layer3.py             # Layer 3 models (auditor, attestation, audit)
|   |   `-- context.py            # Layer 4 models (profile, match, disclosure)
|   |-- routers/                  # FastAPI route handlers (thin)
|   |   |-- identity.py           # 13 Layer 2 endpoints (3 auth tiers)
|   |   `-- context.py            # 10 Layer 4 endpoints (profiles, match, disclose, audit)
|   `-- services/                 # Business logic (thick)
|       |-- crypto.py             # Ed25519 sign/verify, canonical JSON (L2+L3 shared)
|       |-- did.py                # did:key derivation, DID document building
|       |-- credentials.py        # JWT VC issuance + session assertion issuance
|       |-- identity.py           # Agent registration, revocation, Redis cache
|       |-- sessions.py           # Session request, polling, one-use redemption
|       |-- service_identity.py   # did:web resolution, manifest signing, activation
|       |-- authorization.py      # HITL queue, approve/deny, webhook dispatch
|       |-- chain.py              # Chain abstraction (local/web3 dispatch)
|       |-- auditor.py            # Auditor registration
|       |-- attestation.py        # Attestation + revocation pipeline
|       |-- audit.py              # Audit records + Merkle anchoring
|       |-- merkle.py             # Merkle tree construction + proof verification
|       |-- trust.py              # Trust score recomputation (6 SQL queries)
|       |-- federation.py         # Blocklist + SSE + webhook push fan-out
|       |-- ranker.py             # Pure scoring math (no I/O)
|       |-- context_profiles.py   # Profile CRUD + Redis 60s cache (L4)
|       |-- context_mismatch.py   # Over-request detection + severity + escalation (L4)
|       |-- context_matcher.py    # 8-step matching engine + trust gate (L4)
|       |-- context_disclosure.py # HMAC commitment + nonce release + audit write (L4)
|       |-- context_compliance.py # ReportLab PDF export for GDPR/CCPA (L4)
|       |-- workflow_registry.py  # Workflow CRUD, caching, rate limiting, execution counters (L5)
|       |-- workflow_ranker.py    # Quality score formula + per-step candidate ranking (L5)
|       |-- workflow_validator.py # Validation queue, state machine, spec hashing (L5)
|       |-- workflow_context.py   # Context bundle creation, scoped overrides, approval (L5)
|       `-- workflow_executor.py  # Execution reporting, verification, quality recompute (L5)
|-- contracts/                    # Solidity contracts + Hardhat toolchain
|   |-- AttestationLedger.sol     # UUPS proxy: attestation + revocation events
|   |-- AuditChain.sol            # UUPS proxy: Merkle batch anchor events
|   |-- test/                     # Hardhat contract tests (4 cases)
|   `-- scripts/                  # deploy.js, grant_roles.js
|-- crawler/                      # Celery background workers
|   `-- tasks/                    # Layer 2: expire_identity_records, revalidate_service_identity
|                                 # Layer 3: index_chain_events, confirm_chain_events,
|                                 #          anchor_audit_batch, push_revocations
|-- db/                           # Schema, migrations, seed scripts
|   `-- migrations/versions/      # 001_layer1, 002_layer2_identity, 003_layer2_sessions,
|                                 # 004_layer3_trust_verification, 005_layer4_context,
|                                 # 006_layer5_workflows, 007_layer6_liability
|-- ontology/v0.1.json            # 65 capability tags (source of truth)
|-- spec/                         # Implementation specs + completion docs
|-- handoffs/                     # Deployment checklists
|-- tests/                        # Unit, integration, and load tests
|-- docker-compose.yml            # Full stack orchestration
`-- Dockerfile                    # Multi-worker uvicorn container
```

**Stack:** FastAPI + PostgreSQL 15 (pgvector) + Redis 7 + Celery + sentence-transformers + web3.py + Hardhat + cryptography (Ed25519) + PyJWT

**Chain:** Polygon Amoy testnet (`chain_id=80002`), ~$0.001/tx, ~2s blocks

---

*Start with [Lesson 01: The Big Picture](Lesson01_BigPicture.md) — or jump to [Lesson 11: The Lock and Key](Lesson11_CryptoFoundations.md) for Layer 2, [Lesson 21: The Notary's Seal](Lesson21_TrustArchitecture.md) for Layer 3, [Lesson 31: The Privacy Engine](Lesson31_ContextArchitecture.md) for Layer 4, or [Lesson 41: The Registry That Doesn't Execute](Lesson41_WorkflowArchitecture.md) for Layer 5.*
