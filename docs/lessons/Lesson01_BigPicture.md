# 🎓 Lesson 01: The Big Picture — What AgentLedger Builds and Why

## 🛡️ Welcome, Future AgentLedger Engineer!

What happens when billions of AI agents need to find, trust, and transact with services on the open web? 🔍 Today we're exploring **AgentLedger Layer 1** — the "phone book and credit bureau" that lets any AI agent discover any service, verify its claims, and decide whether to trust it.

**Goal:** Understand what AgentLedger builds, why each technology was chosen, and how the five build phases fit together.  
**Time:** 45 minutes  
**Prerequisites:** Basic understanding of REST APIs and Docker  
**Why this matters:** Without this context, you'll be writing code without knowing what problem it solves — the fastest way to make bad design decisions.

---

## 🎯 Learning Objectives

After this lesson you will be able to:

- Explain AgentLedger's purpose in one sentence ✅
- Name the three core capabilities of Layer 1 (Ingest, Store, Serve) ✅
- List every component in the technology stack and why it was chosen ✅
- Trace the five build phases and explain what each one delivers ✅
- Run `docker compose up` and verify the stack is healthy ✅
- Describe what Layer 1 does NOT include (and which layers own those features) ✅

---

## 🔍 The Problem AgentLedger Solves

Imagine a world where AI agents — not humans — browse the web, book flights, process payments, and retrieve medical records. Today, there's no standard way for these agents to:

1. **Find services** — How does a travel-planning agent discover that FlightBookerPro exists?
2. **Verify claims** — FlightBookerPro says it can book flights. Is that actually true?
3. **Assess trust** — Should I send my user's credit card to this service I just found?

AgentLedger solves this by building a **Manifest Registry** — think of it as the DNS + Yelp + credit bureau of the agent web.

### Real-World Analogy

Think of AgentLedger like a city's business licensing office:

| Analogy | AgentLedger |
|---------|-------------|
| A restaurant applies for a license | A service registers its **manifest** |
| The license lists what food they serve | The manifest lists **capabilities** (ontology tags) |
| Health inspectors verify the kitchen | The **crawler** verifies domain ownership via DNS |
| Yelp reviews build reputation | The **trust score** reflects verification history |
| A hungry person searches "pizza near me" | An agent queries `POST /search "book a flight"` |

---

## 📝 The Three Capabilities

Layer 1 builds exactly three things:

```
📁 Agent Manifest          🔍 Verify & Index           🧠 Answer Queries
(.well-known/              Crawl, hash-check,          GET /services
 agent-manifest.json)      embed descriptions          POST /search
        |                        |                          |
        v                        v                          v
┌──────────────┐        ┌──────────────┐         ┌──────────────┐
│  1. INGEST   │   ->   │  2. STORE    │   ->    │  3. SERVE    │
│  Crawl &     │        │  Searchable  │         │  REST API    │
│  validate    │        │  index with  │         │  for agent   │
│  manifests   │        │  pgvector    │         │  discovery   │
└──────────────┘        └──────────────┘         └──────────────┘
```

---

## 📝 Code Walkthrough: The Technology Stack

### Why These Specific Technologies?

Every choice in the stack is documented in `spec/LAYER1_SPEC.md` (lines 32-44). Let's look at each and understand the "why":

```
📁 spec/LAYER1_SPEC.md (Lines 34-44)
```

| Component | Technology | Why This Choice |
|-----------|-----------|----------------|
| API Framework | **FastAPI** (Python 3.11+) | Auto-generates OpenAPI docs, native async/await, Pydantic validation built in |
| Database | **PostgreSQL 15** | JSONB for flexible manifest storage, full-text search, and the pgvector extension |
| Vector Search | **pgvector** | Semantic search over capability descriptions using cosine similarity — no separate vector DB needed |
| Cache | **Redis 7** | Query result caching (60s TTL) and per-IP rate limiting with atomic INCR/EXPIRE |
| Crawler | **Celery + Redis** | Async background workers for manifest fetching — separate from the API process |
| Embeddings | **sentence-transformers** (all-MiniLM-L6-v2) | Local model, no API dependency, 384-dim vectors, good semantic quality |
| Auth | **API key** (X-API-Key header) | Simple for v0.1 — OAuth2 planned for v0.2 |
| Containerization | **Docker + Docker Compose** | Single `docker compose up` launches the entire stack |

`★ Insight ─────────────────────────────────────`
**Why pgvector instead of Pinecone/Weaviate?** AgentLedger keeps the vector store inside PostgreSQL rather than adding a separate vector database. This means: (1) no additional service to deploy and manage, (2) vector search and relational queries happen in the same transaction, (3) fewer moving parts in production. The trade-off is that pgvector is slower than dedicated vector DBs at scale — but at Layer 1's volume (thousands of services, not millions of documents), it's fast enough.
`─────────────────────────────────────────────────`

---

## 📝 Code Walkthrough: Docker Compose

The entire stack is defined in `docker-compose.yml`. Here's what it orchestrates:

```yaml
# docker-compose.yml — Five services, one command

services:
  db:        # PostgreSQL 15 with pgvector extension
  redis:     # Redis 7 — caching + rate limiting + Celery broker
  app:       # FastAPI API server (uvicorn, multiple workers)
  worker:    # Celery worker (processes crawl/verify tasks)
  beat:      # Celery beat (schedules periodic tasks)
```

```
┌──────────────────────────────────────────────────────┐
│                    Docker Compose                     │
│                                                      │
│  ┌─────────┐  ┌─────────┐  ┌──────────────────────┐ │
│  │   db    │  │  redis  │  │        app           │ │
│  │ pg15 +  │  │  7-alp  │  │  uvicorn (4 workers) │ │
│  │ pgvector│  │         │  │  FastAPI on :8000     │ │
│  │  :5432  │  │  :6379  │  │                      │ │
│  └────┬────┘  └────┬────┘  └──────────┬───────────┘ │
│       │            │                  │              │
│       │            │     ┌────────────┴───────────┐  │
│       │            │     │                        │  │
│  ┌────┴────┐  ┌────┴────┐│                        │  │
│  │ worker  │  │  beat   ││  Port 8000 exposed     │  │
│  │ Celery  │  │ Celery  ││  to host machine       │  │
│  │ tasks   │  │ sched.  ││                        │  │
│  └─────────┘  └─────────┘└────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

The `entrypoint.sh` script runs three things in sequence before starting the API:

```bash
# entrypoint.sh — Container startup sequence

#!/usr/bin/env bash
set -e

# 1. Run database migrations (Alembic)
alembic upgrade head

# 2. Seed the ontology tags (65 tags from ontology/v0.1.json)
python db/seed_ontology.py

echo "Starting AgentLedger API..."

# 3. Start uvicorn with configurable workers (default: 4)
exec uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-4}
```

`★ Insight ─────────────────────────────────────`
**Why `exec` before uvicorn?** The `exec` replaces the shell process with uvicorn, so uvicorn becomes PID 1 inside the container. This means Docker's SIGTERM goes directly to uvicorn (graceful shutdown) instead of to the shell (which would require signal forwarding). A small detail that matters in production.
`─────────────────────────────────────────────────`

---

## 📝 Code Walkthrough: Build Phases

Layer 1 was built in five sequential phases. Each phase has a "done when" gate that must pass before moving on.

| Phase | What It Builds | Done When |
|-------|---------------|-----------|
| **1 — Foundation** | Docker stack, DB schema, Alembic migrations, ontology seed, `GET /health`, `GET /ontology` | `docker compose up` clean, `/health` returns 200, `/ontology` returns 65 tags |
| **2 — Manifest Ingestion** | Pydantic models, `POST /manifests`, embedding generation, all DB writes | Sample manifest returns 201 |
| **3 — Query API** | `GET /services`, `POST /search`, ranking algorithm, `GET /services/{id}`, Redis caching, API key auth | Structured and semantic queries return ranked results |
| **4 — Crawler** | Celery workers, Vector A crawl task, Vector B DNS verification, beat schedule | DNS verification updates `trust_tier` 1 → 2 |
| **5 — Hardening** | Rate limiting, input sanitization, typosquat detection, 80%+ test coverage, load test | All endpoints < 500ms p95 @ 100 concurrent |

---

## 📝 What Layer 1 Does NOT Include

This is just as important as what it does include:

| Feature | Owned By | Why Not Layer 1 |
|---------|----------|----------------|
| Blockchain / on-chain storage | Layer 3 | Trust anchoring requires its own infrastructure |
| Agent identity verification | Layer 2 | Identity credentials are a separate concern |
| Third-party trust attestation | Layer 3 | Requires auditor network |
| Cross-registry federation | Layer 3 | Requires inter-registry protocol |
| Audit chain / liability | Layer 6 | Legal and insurance layer |
| OAuth2 auth | v0.2 | API key is sufficient for v0.1 |
| Automated capability probing | v0.2 | Requires opt-in protocol design |

---

## 🧪 Hands-On Exercises

### 🔬 Exercise 1: Start the Stack

```powershell
# From the repo root
cd C:\Projects\AgentLedger
docker compose up --build -d
```

Wait for all containers to be healthy, then verify:

```powershell
curl http://localhost:8000/v1/health
```

📊 **Expected Output:**
```json
{"status":"ok","version":"0.1.0","timestamp":"2026-04-13T12:00:00.000000+00:00"}
```

### 🔬 Exercise 2: Fetch the Ontology

```powershell
curl -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
```

📊 **Expected Output** (truncated):
```json
{
  "ontology_version": "0.1",
  "total_tags": 65,
  "domains": ["TRAVEL", "FINANCE", "HEALTH", "COMMERCE", "PRODUCTIVITY"],
  "tags": [
    {"tag": "travel.air.search", "domain": "TRAVEL", ...},
    ...
  ]
}
```

### 🔬 Exercise 3: Verify Auth is Required

```powershell
# No API key — should get 401
curl http://localhost:8000/v1/ontology
```

📊 **Expected Output:**
```json
{"detail":"missing X-API-Key header"}
```

### 🔬 Exercise 4: Check the API Docs

Open your browser to `http://localhost:8000/docs` — you'll see the auto-generated Swagger UI with all endpoints documented.

---

## 📚 Interview Prep

**Q: What is AgentLedger and what problem does it solve?**

**A:** AgentLedger is a manifest registry that solves the discovery and trust problem for the autonomous agent web. When AI agents need to find services, verify their claims, and assess trustworthiness, AgentLedger provides a searchable index of verified service manifests with semantic search (pgvector), structured queries, and a trust scoring system. Think of it as DNS + Yelp for AI agents.

---

**Q: Why does Layer 1 use PostgreSQL with pgvector instead of a dedicated vector database like Pinecone?**

**A:** Three reasons: (1) Fewer moving parts — one database for both relational and vector data, (2) transactional consistency — embedding writes and service record writes happen in the same transaction, (3) at Layer 1's scale (thousands of services, not millions of documents), pgvector's performance is sufficient. The trade-off is that a dedicated vector DB would scale better for very high-dimensional or very high-volume workloads.

---

**Q: What are the five build phases and why does the order matter?**

**A:** Foundation, Ingestion, Query, Crawler, Hardening. The order matters because each phase depends on the one before it: you can't ingest manifests without a database (Phase 1), you can't query without data (Phase 2), you can't crawl without knowing which services exist (Phase 3), and you can't harden without a working system to test (Phase 4). Each phase has a concrete "done when" gate that must pass before proceeding.

---

## 🎯 Key Takeaways

- AgentLedger Layer 1 is a **Manifest Registry** with three capabilities: Ingest, Store, Serve
- The stack is **FastAPI + PostgreSQL (pgvector) + Redis + Celery + sentence-transformers**
- Five build phases from foundation to hardening, each with a testable gate
- Layer 1 does NOT include blockchain, agent identity, or audit chain — those are higher layers
- A single `docker compose up` launches the entire development stack

---

## 📋 Summary Reference Card

| Item | Value |
|------|-------|
| **Entry point** | `api/main.py` |
| **Spec** | `spec/LAYER1_SPEC.md` |
| **Stack** | FastAPI, PostgreSQL 15 + pgvector, Redis 7, Celery, sentence-transformers |
| **Start command** | `docker compose up --build` |
| **Health endpoint** | `GET /v1/health` (no auth) |
| **API docs** | `http://localhost:8000/docs` |
| **Default API key** | `dev-local-only` |
| **Build phases** | Foundation → Ingestion → Query → Crawler → Hardening |

---

## 🚀 Ready for Lesson 02?

Next up, we'll explore **The Vault** — the database schema that stores everything from services to trust scores to vector embeddings. Get ready to read SQL! 📝

*Remember: AgentLedger is the phone book AND the credit bureau for the agent web — discovery AND trust in one system!* 🛡️
