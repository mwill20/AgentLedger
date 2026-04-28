# AgentLedger — Lesson Index (Layers 1, 2 & 3)

**Project:** AgentLedger — Trust & Discovery Infrastructure for the Autonomous Agent Web
**Layers covered:** Layer 1 — Manifest Registry · Layer 2 — Identity & Credentials · Layer 3 — Trust & Verification
**Total Lessons:** 30
**Estimated Total Time:** 30–42 hours
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

## How to Use These Lessons

1. **Sequential learner:** Go 01 → 10 (Layer 1), then 11 → 20 (Layer 2), then 21 → 30 (Layer 3). Each lesson builds on the previous within a layer.
2. **Interview prep — Layer 1:** Read 01 (overview), 10 (architecture), 05 and 06 (core logic).
3. **Interview prep — Layer 2:** Read 11 (crypto), 14 (agent identity), 15 (sessions), 20 (full flow).
4. **Interview prep — Layer 3:** Read 21 (contracts), 25 (trust scoring), 26 (Merkle), 30 (hardening).
5. **Debugging a specific area:** Jump to the relevant lesson using the tables above.
6. **Contributing:** Read all required lessons for the layer you're modifying, then focus on the module's lesson.

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
|   |   `-- layer3.py             # Layer 3 models (auditor, attestation, audit)
|   |-- routers/                  # FastAPI route handlers (thin)
|   |   `-- identity.py           # 13 Layer 2 endpoints (3 auth tiers)
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
|       `-- ranker.py             # Pure scoring math (no I/O)
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
|                                 # 004_layer3_trust_verification, 005_layer4_context
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

*Start with [Lesson 01: The Big Picture](Lesson01_BigPicture.md) — or jump to [Lesson 11: The Lock and Key](Lesson11_CryptoFoundations.md) for Layer 2, or [Lesson 21: The Notary's Seal](Lesson21_TrustArchitecture.md) for Layer 3.*
