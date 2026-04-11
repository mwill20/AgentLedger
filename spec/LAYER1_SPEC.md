# AgentLedger — Layer 1 Implementation Spec
## Manifest Registry: Discovery & Distribution

**Version:** 0.1  
**Status:** Ready for Implementation  
**Author:** Michael Williams  
**Last Updated:** April 2026

---

## Purpose of This Document

This is the implementation specification for Layer 1 of AgentLedger — the Manifest Registry. It is written for Claude Code or any developer building the system from scratch. Every design decision is documented. Nothing should require guessing.

Do not build anything not described here without updating this spec first.

---

## What Layer 1 Builds

A registry system with three capabilities:
1. **Ingest** — crawl and index agent manifests from services on the web
2. **Store** — maintain a searchable index of verified service manifests
3. **Serve** — answer agent queries about available services via a REST API

Layer 1 does NOT include the Trust Ledger (blockchain), the Audit Chain, or identity attestation. Those are Layers 2–3. This layer handles discovery and basic trust tiering only.

---

## Technology Stack

All stack decisions are final for v0.1. Do not substitute without updating this spec.

| Component | Technology | Reason |
|-----------|-----------|--------|
| API Framework | FastAPI (Python 3.11+) | OpenAPI auto-generation, async support, fast |
| Database | PostgreSQL 15+ | JSONB for manifest storage, full-text search, pgvector for embeddings |
| Vector Search | pgvector extension | Semantic query mode over capability descriptions |
| Cache | Redis 7+ | Query result caching, rate limit enforcement |
| Crawler | Celery + Redis | Async background workers for manifest crawling |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2) | Local model, no API dependency, good semantic quality |
| Auth | API key (header: X-API-Key) | Simple for v0.1, OAuth2 in v0.2 |
| Containerization | Docker + Docker Compose | Single command local setup |
| Testing | pytest + httpx | Async-compatible test suite |

---

## Repository Structure

Build this exact structure. Do not add top-level directories not listed here.

```
AgentLedger/
├── api/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # Settings via pydantic-settings
│   ├── dependencies.py          # Shared dependencies (db, cache, auth)
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── services.py          # GET /services — structured query
│   │   ├── search.py            # POST /search — NL semantic query
│   │   ├── manifests.py         # POST /manifests — service registration
│   │   └── health.py            # GET /health
│   ├── models/
│   │   ├── __init__.py
│   │   ├── manifest.py          # Pydantic models for manifest structure
│   │   ├── service.py           # Pydantic models for service records
│   │   └── query.py             # Pydantic models for query params/responses
│   └── services/
│       ├── __init__.py
│       ├── registry.py          # Core registry CRUD logic
│       ├── ranker.py            # Ranking algorithm
│       ├── embedder.py          # Embedding generation for semantic search
│       └── verifier.py          # Domain verification logic
├── crawler/
│   ├── __init__.py
│   ├── worker.py                # Celery worker entry point
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── crawl.py             # Vector A: standard path crawl
│   │   ├── verify_domain.py     # Vector B: DNS TXT verification
│   │   └── probe_capability.py  # Vector C: live capability probing
│   └── scheduler.py             # Periodic task schedule
├── db/
│   ├── migrations/              # Alembic migration files
│   ├── schema.sql               # Initial schema (also in migrations)
│   └── seed_ontology.py         # Seed script for capability ontology
├── ontology/
│   └── v0.1.json                # Capability ontology (source of truth)
├── spec/
│   ├── LAYER1_SPEC.md           # This file
│   └── agent-manifest-v0.1.json # JSON Schema for manifest validation
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_api/
│   │   ├── test_services.py
│   │   ├── test_search.py
│   │   └── test_manifests.py
│   └── test_crawler/
│       ├── test_crawl.py
│       └── test_verify.py
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── README.md
```

---

## Database Schema

Run these in order. All tables use UUID primary keys.

```sql
-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- Capability ontology (seeded from ontology/v0.1.json, not user-editable)
CREATE TABLE ontology_tags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tag TEXT UNIQUE NOT NULL,          -- e.g. "travel.air.book"
    domain TEXT NOT NULL,              -- e.g. "TRAVEL"
    function TEXT NOT NULL,            -- e.g. "travel.air"
    label TEXT NOT NULL,               -- e.g. "Book a flight"
    description TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1  -- 1=low, 2=medium, 3=high, 4=critical
);

-- Core service registry
CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    legal_entity TEXT,
    manifest_url TEXT NOT NULL,         -- /.well-known/agent-manifest.json
    public_key TEXT,                    -- for manifest signature verification
    trust_tier INTEGER NOT NULL DEFAULT 1,  -- 1=crawled, 2=domain_verified, 3=capability_probed, 4=ledger_attested
    trust_score FLOAT NOT NULL DEFAULT 0.0, -- composite 0.0–100.0
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_banned BOOLEAN NOT NULL DEFAULT false,
    ban_reason TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_crawled_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Raw manifest storage (versioned — never delete old versions)
CREATE TABLE manifests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    raw_json JSONB NOT NULL,            -- full manifest as stored
    manifest_hash TEXT NOT NULL,        -- sha256 of raw_json
    manifest_version TEXT,
    is_current BOOLEAN NOT NULL DEFAULT true,
    crawled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index to enforce one current manifest per service
CREATE UNIQUE INDEX manifests_service_current 
    ON manifests(service_id) WHERE is_current = true;

-- Capability claims (one row per tag per service)
CREATE TABLE service_capabilities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    description TEXT,                   -- service's own description
    embedding vector(384),              -- all-MiniLM-L6-v2 output
    input_schema_url TEXT,
    output_schema_url TEXT,
    success_rate_30d FLOAT,             -- self-reported, flagged if unverified
    avg_latency_ms INTEGER,
    is_verified BOOLEAN NOT NULL DEFAULT false,  -- true only after probe
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(service_id, ontology_tag)
);

-- Economics
CREATE TABLE service_pricing (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    pricing_model TEXT NOT NULL,        -- per_transaction | subscription | freemium | free
    tiers JSONB NOT NULL DEFAULT '[]',
    billing_method TEXT,                -- x402 | stripe | api_key | none
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Context requirements
CREATE TABLE service_context_requirements (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    field_name TEXT NOT NULL,
    field_type TEXT NOT NULL,
    is_required BOOLEAN NOT NULL DEFAULT false,
    sensitivity TEXT NOT NULL DEFAULT 'low',  -- low | medium | pii_medium | pii_high | financial | medical
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Operational metadata
CREATE TABLE service_operations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL UNIQUE REFERENCES services(id),
    uptime_sla_percent FLOAT,
    rate_limit_rpm INTEGER,
    rate_limit_rpd INTEGER,
    geo_restrictions TEXT[] DEFAULT '{}',
    compliance_certs TEXT[] DEFAULT '{}',
    sandbox_url TEXT,
    deprecation_notice_days INTEGER DEFAULT 30,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Crawler event log (audit trail for crawl operations)
CREATE TABLE crawl_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID REFERENCES services(id),
    event_type TEXT NOT NULL,           -- crawl_attempt | crawl_success | crawl_failure | domain_verified | capability_probed | probe_failed
    domain TEXT,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- API keys for query access
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash TEXT UNIQUE NOT NULL,      -- sha256 of actual key, never store plaintext
    name TEXT NOT NULL,
    owner TEXT,
    query_count BIGINT NOT NULL DEFAULT 0,
    monthly_limit BIGINT DEFAULT 1000000,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- Useful indexes
CREATE INDEX services_trust_tier ON services(trust_tier);
CREATE INDEX services_trust_score ON services(trust_score DESC);
CREATE INDEX services_domain ON services(domain);
CREATE INDEX service_capabilities_tag ON service_capabilities(ontology_tag);
CREATE INDEX service_capabilities_embedding ON service_capabilities 
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX crawl_events_service ON crawl_events(service_id, created_at DESC);
```

---

## Manifest Schema

Every registered service must publish a valid manifest at `/.well-known/agent-manifest.json`. The registry validates incoming manifests against this schema before indexing.

### Required Fields

```json
{
  "manifest_version": "1.0",
  "service_id": "string (UUID v4, self-generated)",
  "name": "string",
  "domain": "string (e.g. flightbookerpro.com)",
  "public_key": "string (PEM or JWK, optional in v0.1)",
  "capabilities": [
    {
      "id": "string",
      "ontology_tag": "string (must exist in ontology v0.1)",
      "description": "string (min 20 chars)",
      "input_schema_url": "string (URL, optional)",
      "output_schema_url": "string (URL, optional)"
    }
  ],
  "pricing": {
    "model": "string (per_transaction|subscription|freemium|free)",
    "tiers": [],
    "billing_method": "string (x402|stripe|api_key|none)"
  },
  "context": {
    "required": [],
    "optional": [],
    "data_retention_days": "integer (0 = no retention)",
    "data_sharing": "string (none|anonymized|third_party)"
  },
  "operations": {
    "uptime_sla_percent": "float",
    "rate_limits": {
      "rpm": "integer",
      "rpd": "integer"
    },
    "sandbox_url": "string (URL, optional)"
  },
  "legal_entity": "string (optional in v0.1)",
  "last_updated": "string (ISO 8601)"
}
```

### Validation Rules

Apply these in the `/manifests` registration endpoint before writing to the database:

1. `manifest_version` must equal `"1.0"`
2. `domain` must be a valid FQDN
3. All `ontology_tag` values must exist in the `ontology_tags` table
4. `capabilities` array must have at least 1 entry and no more than 50
5. No duplicate `ontology_tag` values within one manifest
6. `pricing.model` must be one of the enum values
7. `context.data_sharing` must be one of `none | anonymized | third_party`
8. If any capability tag has `sensitivity_tier >= 3`, flag for manual review before activating

---

## API Specification

Base URL: `https://api.agentledger.io/v1` (local: `http://localhost:8000/v1`)

All endpoints require `X-API-Key` header except `/health`.

---

### GET /health
No auth required.

**Response 200:**
```json
{
  "status": "ok",
  "version": "0.1.0",
  "timestamp": "2026-04-10T00:00:00Z"
}
```

---

### POST /manifests
Register a new service or update an existing manifest.

**Request body:** Valid agent manifest JSON (see Manifest Schema above)

**Behavior:**
- Validate manifest against schema — return 422 if invalid
- Check if `domain` already exists in `services` table
  - If new: create service record at trust_tier=1, crawl_events entry
  - If existing: archive current manifest (is_current=false), insert new version
- Generate embedding for each capability description
- Queue domain verification task (async, does not block response)
- Return service record

**Response 201:**
```json
{
  "service_id": "uuid",
  "name": "string",
  "domain": "string",
  "trust_tier": 1,
  "trust_score": 0.0,
  "capabilities_indexed": 3,
  "status": "pending_verification",
  "message": "Manifest indexed. Domain verification queued."
}
```

**Response 422:** Manifest validation failure with field-level errors.

---

### GET /services
Structured query for services by ontology tag and filters.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `ontology` | string | yes | Ontology tag (e.g. `travel.air.book`) |
| `trust_min` | integer | no | Minimum trust score 0–100 (default: 0) |
| `trust_tier_min` | integer | no | Minimum trust tier 1–4 (default: 1) |
| `geo` | string | no | ISO country code filter |
| `pricing_model` | string | no | `per_transaction\|subscription\|freemium\|free` |
| `latency_max_ms` | integer | no | Maximum avg latency |
| `limit` | integer | no | Results to return (default: 10, max: 50) |
| `offset` | integer | no | Pagination offset (default: 0) |

**Behavior:**
- Validate `ontology` tag exists in ontology_tags table — return 400 if not
- Query service_capabilities joined to services
- Apply all provided filters
- Apply ranking algorithm (see Ranking Algorithm section)
- Return ranked list

**Response 200:**
```json
{
  "query": {
    "ontology": "travel.air.book",
    "trust_min": 0,
    "limit": 10
  },
  "total": 42,
  "results": [
    {
      "service_id": "uuid",
      "name": "string",
      "domain": "string",
      "trust_tier": 3,
      "trust_score": 87.4,
      "capability": {
        "ontology_tag": "travel.air.book",
        "description": "string",
        "is_verified": true,
        "avg_latency_ms": 1200,
        "success_rate_30d": 0.97
      },
      "pricing": {
        "model": "per_transaction",
        "billing_method": "x402"
      },
      "rank_score": 0.834
    }
  ]
}
```

---

### POST /search
Natural language semantic search over the capability index.

**Request body:**
```json
{
  "query": "string (natural language, required)",
  "trust_min": 0,
  "trust_tier_min": 1,
  "geo": "US",
  "limit": 10
}
```

**Behavior:**
- Embed the `query` string using sentence-transformers (all-MiniLM-L6-v2)
- Run cosine similarity search against `service_capabilities.embedding` using pgvector
- Filter by trust and geo constraints
- Apply ranking algorithm to re-rank top candidates
- Return results in same format as GET /services

**Response 200:** Same schema as GET /services, with `semantic_score` added to each result.

---

### GET /services/{service_id}
Retrieve full details for a single service.

**Response 200:**
```json
{
  "service_id": "uuid",
  "name": "string",
  "domain": "string",
  "legal_entity": "string|null",
  "trust_tier": 2,
  "trust_score": 54.2,
  "trust_tier_label": "domain_verified",
  "is_active": true,
  "capabilities": [],
  "pricing": {},
  "context_requirements": [],
  "operations": {},
  "manifest_version": "1.0",
  "last_crawled_at": "ISO 8601",
  "first_seen_at": "ISO 8601"
}
```

---

### GET /ontology
Return the full capability ontology tree.

**Response 200:**
```json
{
  "version": "0.1",
  "domains": [
    {
      "domain": "TRAVEL",
      "functions": [
        {
          "function": "travel.air",
          "capabilities": [
            {
              "tag": "travel.air.book",
              "label": "Book a flight",
              "description": "string",
              "sensitivity_tier": 2
            }
          ]
        }
      ]
    }
  ]
}
```

---

## Ranking Algorithm

Applies to all query results before returning. Compute `rank_score` as a float 0.0–1.0.

```python
def compute_rank_score(
    capability_match: float,  # cosine similarity or exact tag match (1.0 if exact)
    trust_score: float,       # 0.0–100.0 from services table, normalize to 0.0–1.0
    latency_score: float,     # 1.0 - (avg_latency_ms / 10000), clamped 0.0–1.0
    cost_score: float,        # inverse of normalized cost, 1.0 = free
    reliability_score: float, # success_rate_30d if available, else 0.5
    context_fit: float        # 1.0 if agent context matches required fields, else 0.5
) -> float:
    return (
        capability_match  * 0.35 +
        trust_score       * 0.25 +
        latency_score     * 0.15 +
        cost_score        * 0.10 +
        reliability_score * 0.10 +
        context_fit       * 0.05
    )
```

**Implementation notes:**
- For structured queries (`GET /services`): `capability_match = 1.0` (exact tag match)
- For semantic queries (`POST /search`): `capability_match = cosine_similarity` from pgvector
- If `avg_latency_ms` is null: `latency_score = 0.5`
- If `success_rate_30d` is null: `reliability_score = 0.5`
- If `cost_score` cannot be computed (no pricing data): `cost_score = 0.5`
- `trust_score` normalization: `trust_score / 100.0`

---

## Crawler Design

### Vector A — Standard Path Crawl (trust_tier stays at 1)

**Trigger:** Periodic Celery beat task every 24 hours per service, or on-demand after registration.

**Logic:**
```
1. Fetch GET https://{domain}/.well-known/agent-manifest.json
2. Timeout: 10 seconds
3. On success:
   a. Validate JSON structure (not full schema validation — just parseable)
   b. Compute sha256 hash of raw response
   c. If hash differs from current manifest_hash: update manifest (archive old, insert new)
   d. Log crawl_event (event_type=crawl_success)
4. On failure (timeout, 4xx, 5xx, invalid JSON):
   a. Log crawl_event (event_type=crawl_failure, details={error})
   b. If 3 consecutive failures: set services.is_active=false
```

---

### Vector B — Domain Verification (trust_tier 1 → 2)

**Trigger:** Queued on manifest registration, retried once per day if pending.

**Logic:**
```
1. Look up TXT records for {domain} via DNS
2. Check for record matching: agentledger-verify={service_id}
3. On match:
   a. Set services.trust_tier = MAX(current_tier, 2)
   b. Set services.last_verified_at = NOW()
   c. Log crawl_event (event_type=domain_verified)
4. On failure: log attempt, retry next day (max 30 days)
```

**Instructions for service owners** (surface in API response on registration):
> Add a DNS TXT record to your domain:
> `Name: @ | Type: TXT | Value: agentledger-verify={your_service_id}`

---

### Vector C — Capability Probing (trust_tier 2 → 3)

**Trigger:** Manual request only in v0.1 (service must opt in via API). Automated in v0.2.

**Logic:**
```
1. Service must be at trust_tier >= 2
2. For each capability in service_capabilities where is_verified=false:
   a. Fetch input_schema_url to understand required params
   b. Generate a synthetic test payload matching the schema
   c. Call the service API with test payload
   d. Evaluate response against output_schema_url
   e. Record latency
   f. On success:
      - Set service_capabilities.is_verified = true
      - Set service_capabilities.verified_at = NOW()
      - Update service_capabilities.avg_latency_ms
      - Log crawl_event (event_type=capability_probed)
   g. On failure:
      - Log crawl_event (event_type=probe_failed, details={error})
      - Tag is NOT marked verified
3. If ALL claimed tags are verified:
   - Set services.trust_tier = MAX(current_tier, 3)
```

---

## Trust Score Computation

Recompute trust_score after any crawl event or verification. Store result in `services.trust_score`.

```python
def compute_trust_score(
    capability_probe_score: float,   # % of claimed tags that are verified (0.0–1.0)
    attestation_score: float,        # 0.0 in Layer 1 (no ledger yet), 1.0 = fully attested
    operational_score: float,        # avg of: uptime_sla/100, (1 - error_rate), consistency
    reputation_score: float          # 0.0 in Layer 1 (no federation yet)
) -> float:
    raw = (
        capability_probe_score * 0.35 +
        attestation_score      * 0.30 +
        operational_score      * 0.20 +
        reputation_score       * 0.15
    )
    return round(raw * 100, 2)  # return as 0.0–100.0
```

**Layer 1 defaults:**
- `attestation_score = 0.0` (Trust Ledger not built yet)
- `reputation_score = 0.0` (cross-registry federation not built yet)
- `operational_score`: compute from `uptime_sla_percent` and crawl success rate

---

## Environment Variables

All config via environment variables. Provide `.env.example` with these keys:

```bash
# Database
DATABASE_URL=postgresql://postgres:password@localhost:5432/agentledger

# Redis
REDIS_URL=redis://localhost:6379/0

# API
API_HOST=0.0.0.0
API_PORT=8000
API_SECRET_KEY=change-me-in-production

# Embeddings
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DEVICE=cpu

# Crawler
CRAWLER_TIMEOUT_SECONDS=10
CRAWLER_MAX_CONSECUTIVE_FAILURES=3
CRAWL_INTERVAL_HOURS=24

# Trust
TRUST_TIER_HIGH_SENSITIVITY_MIN=3
```

---

## Build Order

Build in this exact sequence. Do not start phase N+1 until phase N has passing tests.

### Phase 1 — Foundation (build first)
- [ ] `docker-compose.yml` with postgres + redis services
- [ ] Database schema (`db/schema.sql`) + Alembic migrations
- [ ] Ontology seed script (`db/seed_ontology.py`) loading `ontology/v0.1.json`
- [ ] FastAPI app skeleton (`api/main.py`, `api/config.py`)
- [ ] `GET /health` endpoint
- [ ] `GET /ontology` endpoint

**Phase 1 done when:** `docker compose up` starts cleanly, `/health` returns 200, `/ontology` returns full taxonomy.

---

### Phase 2 — Manifest Ingestion
- [ ] Pydantic manifest models (`api/models/manifest.py`)
- [ ] Manifest validation logic
- [ ] `POST /manifests` endpoint
- [ ] Embedding generation service (`api/services/embedder.py`)
- [ ] Database writes for all manifest tables

**Phase 2 done when:** Posting the sample manifest in `spec/agent-manifest-v0.1.json` returns 201, service appears in database with correct capability embeddings.

---

### Phase 3 — Query API
- [ ] `GET /services` endpoint with all filter params
- [ ] `POST /search` semantic query with pgvector
- [ ] Ranking algorithm (`api/services/ranker.py`)
- [ ] `GET /services/{service_id}` detail endpoint
- [ ] Redis query caching (TTL: 60 seconds)
- [ ] API key auth middleware

**Phase 3 done when:** Structured and semantic queries return ranked results matching the response schemas above.

---

### Phase 4 — Crawler
- [ ] Celery worker setup (`crawler/worker.py`)
- [ ] Vector A: standard path crawl task
- [ ] Vector B: domain verification task
- [ ] Crawl event logging
- [ ] Periodic beat schedule

**Phase 4 done when:** Running the crawler against a live test service updates trust_tier from 1 to 2 after DNS verification.

---

### Phase 5 — Hardening
- [ ] Rate limiting (per API key, per IP)
- [ ] Input sanitization on all endpoints
- [ ] Manifest similarity scoring (typosquat detection)
- [ ] Full test suite (target: 80%+ coverage)
- [ ] API documentation (FastAPI auto-generates — verify it's accurate)

---

## Acceptance Criteria (Full Layer 1)

Layer 1 is complete when ALL of the following pass:

- [ ] A service can register a manifest via `POST /manifests` and receive a 201
- [ ] The registered service appears in `GET /services?ontology=travel.air.book` results
- [ ] `POST /search` with "book a flight to New York" returns the same service in top 3
- [ ] Domain verification via DNS TXT updates trust_tier from 1 to 2
- [ ] A service with 3 consecutive crawl failures is marked inactive
- [ ] An API key with 0 remaining quota receives a 429 on the next query
- [ ] Posting a manifest with an invalid ontology_tag returns 422
- [ ] Posting a manifest with a sensitive tag (`sensitivity_tier >= 3`) is flagged for review
- [ ] `GET /ontology` returns all 65 capability tags from v0.1
- [ ] All endpoints respond within 500ms at p95 under 100 concurrent requests

---

## What Layer 1 Does NOT Include

Do not build these in Layer 1. They are in scope for later layers:

- Blockchain or any on-chain storage (Layer 3)
- Trust attestation by third-party auditors (Layer 3)
- Agent identity verification (Layer 2)
- Cross-registry federation or blocklist API (Layer 3)
- Liability or audit chain (Layer 6)
- Payment processing (future)
- OAuth2 authentication (v0.2)
- Automated capability probing without opt-in (v0.2)

---

*This spec is the source of truth for Layer 1. Update it before changing any behavior described here.*
