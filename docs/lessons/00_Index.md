# AgentLedger Layer 1 — Lesson Index

**Project:** AgentLedger — Trust & Discovery Infrastructure for the Autonomous Agent Web  
**Layer:** 1 — Manifest Registry (Discovery & Distribution)  
**Total Lessons:** 10  
**Estimated Total Time:** 8-12 hours  
**Prerequisites:** Basic Python, basic SQL, basic understanding of REST APIs

---

## Curriculum Map

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

---

## Lesson List

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

## How to Use These Lessons

1. **Sequential learner**: Go 01 -> 10 in order. Each lesson builds on the previous.
2. **Just need to interview**: Read 01 (overview), then 10 (architecture), then 05 and 06 (core logic).
3. **Debugging a specific area**: Jump to the relevant lesson using the table above.
4. **Contributing to the project**: Read all required lessons, then focus on the module you're modifying.

---

## Project Quick Reference

```
AgentLedger/
|-- api/                  # FastAPI application (Python 3.11+)
|   |-- config.py         # Settings via pydantic-settings
|   |-- dependencies.py   # DB, Redis, auth injection
|   |-- main.py           # App entry + router mounting
|   |-- ratelimit.py      # Pure ASGI rate-limiting middleware
|   |-- models/           # Pydantic request/response schemas
|   |-- routers/          # FastAPI route handlers (thin)
|   `-- services/         # Business logic (thick)
|-- crawler/              # Celery background workers
|-- db/                   # Schema, migrations, seed scripts
|-- ontology/v0.1.json    # 65 capability tags (source of truth)
|-- spec/                 # Implementation specs + JSON schemas
|-- tests/                # Unit, integration, and load tests
|-- docker-compose.yml    # Full stack orchestration
`-- Dockerfile            # Multi-worker uvicorn container
```

**Stack:** FastAPI + PostgreSQL 15 (pgvector) + Redis 7 + Celery + sentence-transformers

---

*Start with [Lesson 01: The Big Picture](Lesson01_BigPicture.md)*
