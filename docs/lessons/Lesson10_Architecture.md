# Lesson 10: The Architect's View -- Full Architecture Deep Dive

## Welcome Back, Systems Engineer!

You've now explored the AgentLedger Layer 1 foundation and the current Layer 2 identity stack. This final lesson synthesizes everything into an **end-to-end architectural understanding** -- how data flows through the system, why each design decision was made, and where later layers extend the foundation.

**Goal:** See the complete picture. Trace requests end-to-end, understand the trade-offs behind every design decision, and identify the extension points for future layers.
**Time:** 45 minutes
**Prerequisites:** Lessons 01-09 (optional lesson for comprehensive understanding)
**Why this matters:** Architecture interviews don't ask about individual files. They ask you to explain the system as a whole, justify decisions, and identify weaknesses.

---

## Learning Objectives

- Trace two complete request paths end-to-end (registration and search)
- Identify all six architectural layers and what each is responsible for
- Explain the ten key design decisions and the trade-off behind each one
- Describe the five Layer 2+ extension points built into Layer 1
- Answer comprehensive architecture interview questions

---

## The Six Architectural Layers

```
+-------------------------------------------+
|          ASGI Middleware Layer             |
|  RateLimitMiddleware (pure ASGI)          |
|  - Per-IP: Redis pipeline (100/min)       |
|  - Per-key: DB monthly quota              |
|  - Exempt: /health, /docs                 |
+-------------------------------------------+
|          Router Layer (thin)              |
|  health, ontology, manifests, services,   |
|  search, verify, identity                 |
|  - Receives validated models              |
|  - Delegates to service layer             |
|  - Returns response models                |
+-------------------------------------------+
|          Dependency Layer                 |
|  get_db (async session)                   |
|  get_redis (aioredis client)              |
|  require_api_key (config + DB check)      |
|  require_admin_api_key (with fallback)    |
|  require_bearer_credential (Layer 2 VC)   |
+-------------------------------------------+
|          Service Layer (thick)            |
|  registry    - CRUD for manifests/queries |
|  embedder    - model/hash dual-mode       |
|  ranker      - 6-factor ranking           |
|  typosquat   - Levenshtein detection      |
|  verifier    - DNS token generation       |
|  crypto      - Ed25519/JWT (Layer 2)      |
|  did         - DID methods (Layer 2)      |
|  credentials - VC issuance (Layer 2)      |
|  identity    - Agent identity (Layer 2)   |
|  service_identity - did:web activation    |
|  sessions    - session assertions         |
|  authorization - HITL queue               |
+-------------------------------------------+
|          Model Layer                      |
|  manifest.py  - ServiceManifest (request) |
|  query.py     - SearchRequest (request)   |
|  service.py   - All response models       |
|  identity.py  - Layer 2 models            |
|  sanitize.py  - Null bytes, whitespace    |
+-------------------------------------------+
|          Data Layer                       |
|  PostgreSQL 15 + pgvector                 |
|   - asyncpg (FastAPI)                     |
|   - psycopg2 (Celery)                     |
|  Redis 7                                  |
|   - aioredis (FastAPI)                    |
|   - redis-py (Celery broker/backend)      |
+-------------------------------------------+
```

Each layer has a single responsibility:
- **Middleware**: rate limiting and request filtering (cross-cutting)
- **Router**: HTTP handling, dependency injection, delegation
- **Dependency**: resource provisioning (DB sessions, Redis clients, auth)
- **Service**: all business logic (validation, embedding, ranking, crawling)
- **Model**: data shapes and input validation
- **Data**: persistence and caching

---

## End-to-End: Manifest Registration

```
Agent sends POST /manifests with JSON body
  |
  v
[1] ASGI Middleware (ratelimit.py)
  - Extract client IP from scope
  - Redis pipeline: INCR + EXPIRE + TTL (1 round-trip)
  - If count > 100: return 429 with Retry-After
  - Extract X-API-Key header
  - Check per-key quota against api_keys table
  - If quota exceeded: return 429
  - Pass through with rate limit headers injected
  |
  v
[2] FastAPI Router (routers/manifests.py)
  - Depends(require_api_key): check X-API-Key against
    config keys, then DB keys. Reject 401 if invalid.
  - Depends(get_db): create async DB session
  - Parse JSON body into ServiceManifest model
  |
  v
[3] Pydantic Validation (models/manifest.py)
  - sanitize_inputs (mode="before"):
    - check_null_bytes_recursive() -> reject if found
    - strip_strings_recursive() -> trim whitespace
  - Parse fields: UUID, HttpUrl, Literal, datetime
  - validate_domain() -> FQDN regex check
  - validate_capabilities() -> 1-50 items, no duplicates
  |
  v
[4] Service Layer (services/registry.py::register_manifest)
  - Validate ontology tags against cached index
  - Compute manifest SHA-256 hash
  - Determine initial trust score from operations metadata
  - Check sensitivity tier -> set status (registered|pending_review)
  |
  - Query existing service by ID and domain
  - IDEMPOTENCY CHECK: same service + same hash -> short-circuit return
  |
  - If domain changed: run typosquat detection
    - Extract domain bases, compute Levenshtein distances
    - Collect warnings (advisory only)
  |
  - Service UPSERT: INSERT ... ON CONFLICT (id) DO UPDATE
  - Manifest versioning: mark old is_current=false, insert new
  - Batch embed all capability descriptions (embed_batch)
  - Delete old capabilities, insert new with embeddings
  - Pricing UPSERT (CTE delete + insert)
  - Context requirements: delete + insert
  - Operations UPSERT: ON CONFLICT (service_id) DO UPDATE
  - COMMIT (single transaction for all 6 tables)
  |
  v
[5] Enqueue Verification (crawler/tasks/verify_domain.py)
  - If Celery available: verify_domain_task.delay()
  - Non-blocking, returns immediately
  |
  v
[6] Response
  - 201 Created
  - ManifestRegistrationResponse:
    {service_id, trust_tier:1, trust_score, status, capabilities_indexed, typosquat_warnings}
```

Total tables written in one transaction: **services, manifests, service_capabilities, service_pricing, service_context_requirements, service_operations**.

---

## End-to-End: Semantic Search

```
Agent sends POST /search {"query": "book a flight with seat selection"}
  |
  v
[1] Rate Limiting (same as above)
  |
  v
[2] Router + Auth + Validation
  - SearchRequest: query max 500 chars, limit 1-100
  - sanitize_inputs: null bytes + whitespace
  |
  v
[3] Service Layer (services/registry.py::search_services)
  |
  - CACHE CHECK: SHA-256 of all params -> Redis GET
    - Hit? Return cached ServiceSearchResponse (skip everything below)
  |
  - EMBED: embed_text(query) -> 384-dim vector
    - Model mode: sentence-transformers all-MiniLM-L6-v2
    - Hash mode: tokenize -> SHA-256 scatter -> L2 normalize
  |
  - SERIALIZE: "[0.123456,0.234567,...]" for pgvector
  |
  - QUERY: pgvector cosine distance with overfetch
    - candidate_limit = max(limit * 5, 50)
    - 1.0 - (embedding <=> query_vector) AS cosine_similarity
    - Filter: is_active, not banned, trust >= trust_min
    - ORDER BY embedding <=> query_vector (uses IVFFlat index)
  |
  - GROUP by service_id (one service may match multiple capabilities)
    - First occurrence: create ServiceSummary
    - Subsequent: append to matched_capabilities
    - Recalculate rank_score using best capability match
  |
  - RANK: compute_rank_score for each service
    - capability_match * 0.35
    - trust_score * 0.25
    - latency_score * 0.15
    - cost_score * 0.10
    - reliability_score * 0.10
    - context_fit * 0.05
  |
  - SORT by rank_score DESC
  - PAGINATE: slice [offset : offset + limit]
  |
  - CACHE WRITE: Redis SET with 60s TTL (best-effort)
  |
  v
[4] Response
  - ServiceSearchResponse:
    {total, limit, offset, results: [ServiceSummary...]}
```

---

## Ten Key Design Decisions

### 1. pgvector Instead of Dedicated Vector DB

**Decision:** Keep vectors in PostgreSQL instead of Pinecone/Weaviate.
**Trade-off:** Simpler infrastructure and transactional consistency vs. lower vector search throughput at massive scale.
**Why it works:** At Layer 1's volume (thousands of services, not millions of documents), pgvector is fast enough. The IVFFlat index provides sublinear search time.

### 2. Pure ASGI Middleware

**Decision:** Implement rate limiting as a pure ASGI middleware instead of using Starlette's BaseHTTPMiddleware.
**Trade-off:** More complex code (manual ASGI protocol handling) vs. dramatically better performance.
**Why it works:** BaseHTTPMiddleware spawns a thread per request and buffers response bodies. At 100 concurrent users, this creates thread contention that pushes p95 above 500ms. Pure ASGI eliminates both problems.

### 3. Thin Routers, Thick Services

**Decision:** Router files are 15-50 lines. All business logic lives in `api/services/`.
**Trade-off:** More files to navigate vs. clear separation of concerns.
**Why it works:** Routers handle HTTP (status codes, headers, dependency injection). Services handle logic (SQL, embedding, ranking). You can test services without HTTP and routers without databases.

### 4. Fail-Open Rate Limiting

**Decision:** Redis or database failures allow requests through instead of blocking them.
**Trade-off:** Temporary loss of rate limiting during failures vs. guaranteed availability.
**Why it works:** AgentLedger is a discovery registry. Blocking all requests because Redis is restarting is worse than temporarily allowing unlimited requests. Auth (API keys) provides a separate defense layer.

### 5. Hash-Mode Embeddings

**Decision:** Support a deterministic hash-based embedding fallback alongside the real ML model.
**Trade-off:** Hash embeddings produce poor semantic similarity vs. enabling CI and testing without a 100MB model download.
**Why it works:** CI pipelines and load tests need fast startup. The hash embedder produces consistent (if crude) vectors that exercise the full code path without model dependencies.

### 6. NullRedisClient Pattern

**Decision:** The app runs without Redis by substituting a no-op client.
**Trade-off:** No caching or rate limiting vs. the app starts and serves requests.
**Why it works:** Local development and unit testing shouldn't require Redis. The NullRedisClient causes rate limiting to be skipped (fail-open) and caching to miss (fall through to DB).

### 7. Conditional Celery Registration

**Decision:** Task implementations are plain functions. Celery decorators are conditionally applied.
**Trade-off:** Duplicated function definitions vs. testability without Celery infrastructure.
**Why it works:** Test code calls `_crawl_service_impl()` directly. Production code calls `crawl_service_task.delay()`. Same logic, different invocation.

### 8. Advisory Typosquat Warnings

**Decision:** Similar domains produce warnings in the response but don't block registration.
**Trade-off:** Typosquats can still register vs. legitimate similar domains aren't blocked.
**Why it works:** String similarity is a heuristic, not a definitive test. `airbnb-flights.com` is similar to `airbnb.com` but could be legitimate. Warnings are logged for review; blocking is a human decision.

### 9. Sensitivity-Based Review Flagging

**Decision:** Manifests with sensitivity_tier >= 3 capabilities are set to `pending_review` status.
**Trade-off:** High-sensitivity services are registered but inactive vs. automatic safety gate at ingest time.
**Why it works:** In the current build, `pending_review` is an ingest-time status flag rather than a full human review queue. A service claiming to handle medical records (`sensitivity_tier: 3`) or financial transactions is kept inactive until a separate activation path runs.

### 10. Idempotent Manifest Registration

**Decision:** If the manifest hash hasn't changed, skip all database writes.
**Trade-off:** Extra hash comparison per request vs. dramatically fewer DB writes under load.
**Why it works:** During load testing, the bounded manifest pool re-submits the same manifests. Without idempotency, each re-submission rewrites 6 tables. With it, re-submissions return instantly. This was essential for meeting the <500ms p95 target.

---

## Extension Points for Layer 2+

### 1. Trust Score Components

```python
# api/services/ranker.py
def compute_trust_score(
    capability_probe_score: float,  # current branch: verified capability ratio
    attestation_score: float,       # current branch: active did:web identity
    operational_score: float,       # Layer 1: uptime SLA
    reputation_score: float,        # current branch: session redemption outcomes
) -> float:
```

Layer 1 starts with only the operational component populated. On the current branch, Layer 2 now contributes:
- `capability_probe_score` from verified capabilities on the service record
- `attestation_score` from active service identity (`did:web` activation)
- `reputation_score` from recent session redemption outcomes

That leaves later layers to deepen the evidence sources, not to introduce these score dimensions for the first time.

### 2. Trust Tier 3 and 4

The `services` table has `trust_tier INTEGER`. Layer 1 achieves tiers 1 (crawled) and 2 (domain_verified). Tier 3 (probed) requires Layer 2's capability verification. Tier 4 (attested) requires Layer 3's auditor network.

### 3. Public Key Validation

The `services.public_key` field started as dormant storage in Layer 1. On the current branch, Layer 2 activates it by validating signed manifests against the service's `did:web` document and persisting the verified key during service identity activation.

### 4. Crawl Events Audit Trail

The `crawl_events` table logs every crawl attempt, verification attempt, and status change. Layer 6 (Audit Chain & Liability) will use this trail for compliance and dispute resolution.

### 5. Layer 2 Identity & Authorization (Already Built)

Layer 2's identity and authorization stack is already implemented:
- `agent_identities` table -- DID-based agent records
- `session_assertions` table -- short-lived session JWT records
- `authorization_requests` table -- HITL approval queue
- `revocation_events` table -- credential revocation log
- `api/services/crypto.py` -- Ed25519 signing and verification
- `api/services/did.py` -- did:key and did:web methods
- `api/services/credentials.py` -- JWT Verifiable Credentials
- `api/services/identity.py` -- registration, verification, revocation
- `api/services/service_identity.py` -- service DID resolution and activation
- `api/services/sessions.py` -- session request, poll, and redemption
- `api/services/authorization.py` -- approval queue and webhook dispatch
- `api/routers/identity.py` -- 13 REST endpoints across identity and authorization

---

## The Trust Model (Complete View)

```
                    Trust Score Formula
                    ===================
              capability_probe * 0.35      <-- Layer 2 (future)
            + attestation      * 0.30      <-- Layer 3 (future)
            + operational      * 0.20      <-- Layer 1 (uptime SLA)
            + reputation       * 0.15      <-- Layer 3 (future)
            = 0-100 score

                    Ranking Formula
                    ===============
              capability_match * 0.35      <-- cosine similarity or 1.0
            + trust_score      * 0.25      <-- normalized 0-100 to 0-1
            + latency_score    * 0.15      <-- inverse of avg_latency_ms
            + cost_score       * 0.10      <-- pricing model tier
            + reliability      * 0.10      <-- success_rate_30d
            + context_fit      * 0.05      <-- reserved (always 1.0)
            = 0-1 rank score

                    Trust Tier Progression
                    ======================
              Tier 1: crawled              <-- Layer 1 (registration)
              Tier 2: domain_verified      <-- Layer 1 (DNS TXT)
              Tier 3: probed               <-- Layer 2 (capability test)
              Tier 4: attested             <-- Layer 3 (auditor vouch)
```

---

## Current API Surface Summary

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/v1/health` | GET | None | Liveness and version check |
| `/v1/ontology` | GET | API key | Return ontology metadata and tags |
| `/v1/manifests` | POST | API key | Register or update a service manifest |
| `/v1/services` | GET | API key | Structured service query |
| `/v1/services/{id}` | GET | API key | Full service detail lookup |
| `/v1/services/{id}/verify` | POST | API key | Trigger manual domain verification flow |
| `/v1/search` | POST | API key | Semantic search over capability embeddings |
| `/v1/identity/.well-known/did.json` | GET | None | Issuer DID document |
| `/v1/identity/agents/register` | POST | API key | Register agent DID and issue VC |
| `/v1/identity/agents/verify` | POST | None | Verify a presented agent credential |
| `/v1/identity/agents/{did}` | GET | None | Resolve a registered agent record |
| `/v1/identity/agents/{did}/revoke` | POST | Admin key | Revoke an agent credential |
| `/v1/identity/sessions/request` | POST | Bearer VC | Request a scoped session assertion |
| `/v1/identity/sessions/{id}` | GET | Bearer VC | Poll issued or pending session status |
| `/v1/identity/sessions/redeem` | POST | None | Redeem a session assertion once |
| `/v1/identity/services/{domain}/did` | GET | None | Resolve service did:web document |
| `/v1/identity/services/{domain}/activate` | POST | API key | Activate service identity and trust updates |
| `/v1/authorization/pending` | GET | Admin key | List pending HITL requests |
| `/v1/authorization/approve/{id}` | POST | Admin key | Approve HITL request and issue linked session |
| `/v1/authorization/deny/{id}` | POST | Admin key | Deny HITL request |

---

## Hands-On Exercises

### Exercise 1: Full Request Trace

Start the Docker stack and use your browser's network tab or curl with `-v` to trace a complete request:

```powershell
# Observe all headers including rate limit
curl -v -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
```

Identify: status code, rate limit headers, content-type, response body structure.

### Exercise 2: Cache Behavior

```powershell
# First request (cache miss -- hits DB)
curl -H "X-API-Key: dev-local-only" "http://localhost:8000/v1/services?ontology=travel.air.book"

# Second request within 60s (cache hit -- skips DB)
curl -H "X-API-Key: dev-local-only" "http://localhost:8000/v1/services?ontology=travel.air.book"

# Observe timing difference (second should be faster)
```

### Exercise 3: Architecture Diagram

Draw the complete system architecture from memory. Include:
- All 5 Docker services (db, redis, app, worker, beat)
- All 6 code layers (middleware, router, dependency, service, model, data)
- The two query paths (structured and semantic)
- The two crawl vectors (manifest fetch and DNS verification)
- The trust tier progression

---

## Interview Prep

**Q: Walk me through what happens when an AI agent searches for a service.**

**A:** The agent sends `POST /search` with a natural language query. The request passes through the ASGI rate limiter (Redis pipeline check), then FastAPI routing with API key authentication. The SearchRequest model validates and sanitizes the input. The service layer first checks Redis for a cached result (SHA-256 of all params). On cache miss, it embeds the query into a 384-dim vector using sentence-transformers, then runs a pgvector cosine distance query that overfetches 5x the requested limit. Results are grouped by service (one service may match multiple capabilities), ranked using a six-factor algorithm (capability match 35%, trust 25%, latency 15%, cost 10%, reliability 10%, context 5%), sorted by rank score, paginated, cached in Redis for 60 seconds, and returned.

---

**Q: What would you change if AgentLedger needed to handle 10 million services?**

**A:** Three main changes: (1) Replace pgvector with a dedicated vector database like Pinecone or Milvus -- pgvector's IVFFlat index performance degrades at that scale. (2) Add read replicas for PostgreSQL to distribute query load. (3) Implement query result sharding in Redis (by ontology domain) to reduce cache key space. The rest of the architecture (FastAPI, Celery, the ranking algorithm) would scale horizontally without changes.

---

**Q: What are the security boundaries in AgentLedger?**

**A:** Five boundaries: (1) **Rate limiting** -- per-IP (100/min via Redis) and per-API-key (monthly quota via DB), both fail-open. (2) **Authentication** -- API key required for most endpoints, admin key for revocation. (3) **Input validation** -- Pydantic models reject null bytes, validate FQDN format, enforce field constraints. (4) **Typosquat detection** -- Levenshtein distance warns (but doesn't block) on similar domains. (5) **Sensitivity gating** -- High-sensitivity manifests are flagged as pending_review and don't appear in search until approved. All layers are defense-in-depth -- each works independently.

---

**Q: Why are there two database URL settings?**

**A:** FastAPI uses async SQLAlchemy with the asyncpg driver (`postgresql+asyncpg://`), while Celery workers are synchronous processes that use psycopg2 (`postgresql://`). Same database, different Python drivers. The sync URL is also used by Alembic migrations and the ontology seed script. This dual-driver pattern is necessary because async and sync code can't share database connection pools.

---

## Key Takeaways

- Six architectural layers: middleware, router, dependency, service, model, data
- Thin routers delegate to thick services -- business logic never lives in routers
- Ten key design decisions, each with an explicit trade-off rationale
- Five extension points connect Layer 1 to Layers 2-6
- Trust model: score formula (4 factors), ranking formula (6 factors), tier progression (4 tiers)
- The system handles registration, discovery, and verification as three distinct capabilities
- Every design decision was driven by a specific constraint (performance, security, dev experience, or scale)

---

## Summary Reference Card

| Metric | Value |
|--------|-------|
| **Total source files** | ~35 Python files |
| **Lines of code** | ~4,500 (Layer 1) + ~1,400 (Layer 2) |
| **Database tables** | 9 (Layer 1) + 4 (Layer 2) = 13 |
| **API endpoints** | 20 (7 Layer 1 + 13 Layer 2) |
| **Test count** | 213 |
| **Test coverage** | 80%+ |
| **Load targets** | Layer 1: <500ms p95; Layer 2 identity verify snapshot: 110ms p95 @ 100 concurrent |
| **Docker services** | 5 (db, redis, app, worker, beat) |
| **Build phases** | 5 (Foundation, Ingestion, Query, Crawler, Hardening) |
| **Trust tiers** | 4 (crawled, domain_verified, probed, attested) |
| **Embedding dimensions** | 384 |
| **Ontology tags** | 65 across 5 domains |

---

## Congratulations!

You've completed the full Layer 1 curriculum. You now understand:

- What AgentLedger builds and why (Lesson 01)
- How the database stores everything (Lesson 02)
- How configuration flows and dependencies are injected (Lesson 03)
- How Pydantic validates every input (Lesson 04)
- How manifests are registered across 6 tables (Lesson 05)
- How structured and semantic search work (Lesson 06)
- How the crawler maintains registry integrity (Lesson 07)
- How rate limiting and typosquat detection protect the system (Lesson 08)
- How tests verify everything without Docker (Lesson 09)
- How it all fits together architecturally (Lesson 10)

*AgentLedger is the phone book AND the credit bureau for the agent web. You now know how both work, inside and out.*
