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

Preserve this core runtime structure and keep documentation assets under `docs/`. `spec/LAYER1_SPEC.md` is the canonical Layer 1 spec path.

```
AgentLedger/
├── api/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # Settings via pydantic-settings
│   ├── dependencies.py          # Shared dependencies (db, cache, auth)
│   ├── ratelimit.py             # Per-IP + per-API-key rate limiting middleware
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── health.py            # GET /health
│   │   ├── manifests.py         # POST /manifests — service registration
│   │   ├── ontology.py          # GET /ontology — capability taxonomy
│   │   ├── search.py            # POST /search — NL semantic query
│   │   ├── services.py          # GET /services — structured query
│   │   └── verify.py            # POST /services/{id}/verify — DNS verification
│   ├── models/
│   │   ├── __init__.py
│   │   ├── manifest.py          # Pydantic models for manifest structure
│   │   ├── query.py             # Pydantic models for query params/responses
│   │   ├── sanitize.py          # Recursive strip/null-byte input sanitization
│   │   └── service.py           # Pydantic models for service records
│   └── services/
│       ├── __init__.py
│       ├── embedder.py          # Embedding generation for semantic search
│       ├── ranker.py            # Ranking algorithm
│       ├── registry.py          # Core registry CRUD logic
│       ├── typosquat.py         # Levenshtein-based typosquat detection
│       └── verifier.py          # Domain verification logic
├── crawler/
│   ├── __init__.py
│   ├── worker.py                # Celery worker entry point
│   ├── scheduler.py             # Periodic task schedule
│   └── tasks/
│       ├── __init__.py
│       ├── crawl.py             # Vector A: standard path crawl
│       ├── verify_domain.py     # Vector B: DNS TXT verification
│       └── probe_capability.py  # Vector C: live capability probing
├── db/
│   ├── schema.sql               # Initial schema (also in migrations)
│   ├── seed_ontology.py         # Seed script for capability ontology
│   └── migrations/              # Alembic migration files
├── ontology/
│   └── v0.1.json                # Capability ontology (source of truth)
├── spec/
│   ├── LAYER1_SPEC.md           # This file (canonical spec)
│   ├── agent-manifest-v0.1.json # JSON Schema for manifest validation
│   └── sample-manifest-flightbookerpro.json
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Unit test fixtures (TestClient, mocks)
│   ├── test_api/                # Unit tests for API modules
│   │   ├── test_manifests.py
│   │   ├── test_services.py
│   │   ├── test_search.py
│   │   ├── test_verify.py
│   │   ├── test_ratelimit.py
│   │   ├── test_sanitization.py
│   │   ├── test_typosquat.py
│   │   ├── test_ranker.py
│   │   ├── test_embedder.py
│   │   ├── test_registry_helpers.py
│   │   └── test_crawler_helpers.py
│   ├── test_crawler/            # Crawler task unit tests
│   │   ├── test_crawl.py
│   │   └── test_verify.py
│   └── test_integration/        # Integration tests (live Docker stack)
│       ├── conftest.py
│       └── test_full_stack.py
├── docs/                        # Documentation (Mintlify, research, internal)
│   ├── mint.json
│   ├── introduction.mdx
│   ├── problem.mdx
│   ├── ontology.mdx
│   ├── roadmap.mdx
│   ├── threat-model.mdx
│   ├── architecture/
│   │   ├── overview.mdx
│   │   ├── manifest-registry.mdx
│   │   ├── trust-ledger.mdx
│   │   └── audit-chain.mdx
│   ├── research/
│   │   ├── AgentLedger_Whitepaper_v0.1.docx
│   │   └── Trust-and-Discovery-Infrastructure-for-the-Autonomous-Agent-Web.pdf
│   └── internal/
│       ├── NORTHSTAR.md
│       └── Layer1_prompt.md
├── .gitignore
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── entrypoint.sh
├── pyproject.toml
├── requirements.txt
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
    manifest_url TEXT NOT NULL,
    public_key TEXT,
    trust_tier INTEGER NOT NULL DEFAULT 1,  -- 1=crawled, 2=domain_verified, 3=capability_probed, 4=ledger_attested
    trust_score FLOAT NOT NULL DEFAULT 0.0,
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
    raw_json JSONB NOT NULL,
    manifest_hash TEXT NOT NULL,
    manifest_version TEXT,
    is_current BOOLEAN NOT NULL DEFAULT true,
    crawled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX manifests_service_current
    ON manifests(service_id) WHERE is_current = true;

-- Capability claims (one row per tag per service)
CREATE TABLE service_capabilities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    description TEXT,
    embedding vector(384),              -- all-MiniLM-L6-v2 output
    input_schema_url TEXT,
    output_schema_url TEXT,
    success_rate_30d FLOAT,
    avg_latency_ms INTEGER,
    is_verified BOOLEAN NOT NULL DEFAULT false,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(service_id, ontology_tag)
);

-- Economics
CREATE TABLE service_pricing (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    pricing_model TEXT NOT NULL,
    tiers JSONB NOT NULL DEFAULT '[]',
    billing_method TEXT,
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
    sensitivity TEXT NOT NULL DEFAULT 'low',
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

-- Crawler event log
CREATE TABLE crawl_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID REFERENCES services(id),
    event_type TEXT NOT NULL,
    domain TEXT,
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- API keys
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    owner TEXT,
    query_count BIGINT NOT NULL DEFAULT 0,
    monthly_limit BIGINT DEFAULT 1000000,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- Indexes
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

Every registered service must publish a valid manifest at `/.well-known/agent-manifest.json`.

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
    "rate_limits": { "rpm": "integer", "rpd": "integer" },
    "sandbox_url": "string (URL, optional)"
  },
  "legal_entity": "string (optional in v0.1)",
  "last_updated": "string (ISO 8601)"
}
```

### Validation Rules

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

### GET /health
**Response 200:** `{ "status": "ok", "version": "0.1.0", "timestamp": "ISO 8601" }`

### POST /manifests
Register or update a service manifest.
- Validate → create/update service record → generate embeddings → queue domain verification
- **Response 201:** service_id, trust_tier, status, capabilities_indexed
- **Response 422:** field-level validation errors

### GET /services
Structured query by ontology tag.
- Params: `ontology` (required), `trust_min`, `trust_tier_min`, `geo`, `pricing_model`, `latency_max_ms`, `limit`, `offset`
- Returns ranked list with rank_score

### POST /search
Natural language semantic query.
- Body: `{ "query": "string", "trust_min": 0, "geo": "US", "limit": 10 }`
- Embeds query → pgvector cosine search → ranking → returns same format as GET /services

### GET /services/{service_id}
Full detail for a single service including all blocks.

### GET /ontology
Full capability taxonomy tree — all 65 tags from v0.1.

---

## Ranking Algorithm

```python
def compute_rank_score(
    capability_match: float,  # 1.0 for exact tag, cosine sim for semantic
    trust_score: float,       # normalized from 0-100 to 0.0-1.0
    latency_score: float,     # 1.0 - (avg_latency_ms / 10000), clamped
    cost_score: float,        # inverse normalized, 1.0 = free
    reliability_score: float, # success_rate_30d or 0.5 if unknown
    context_fit: float        # 1.0 if context matches required fields, else 0.5
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

---

## Crawler Design

**Vector A — Standard Path Crawl (trust_tier stays 1)**
- Fetch `/.well-known/agent-manifest.json` every 24h
- On success: hash check, update if changed, log event
- On 3 consecutive failures: set `is_active=false`

**Vector B — Domain Verification (trust_tier 1 → 2)**
- Check DNS TXT for `agentledger-verify={service_id}`
- On match: set trust_tier=2, log event
- Retry daily for 30 days max

**Vector C — Capability Probing (trust_tier 2 → 3)**
- Manual opt-in only in v0.1
- Synthetic test payload per capability, evaluate response
- All tags verified → trust_tier=3

---

## Trust Score Computation

```python
def compute_trust_score(
    capability_probe_score: float,  # % verified tags (0.0-1.0)
    attestation_score: float,       # 0.0 in Layer 1
    operational_score: float,       # uptime + crawl success rate
    reputation_score: float         # 0.0 in Layer 1
) -> float:
    raw = (
        capability_probe_score * 0.35 +
        attestation_score      * 0.30 +
        operational_score      * 0.20 +
        reputation_score       * 0.15
    )
    return round(raw * 100, 2)  # 0.0–100.0
```

---

## Build Order

### Phase 1 — Foundation
- docker-compose.yml (postgres + redis)
- db/schema.sql + Alembic migrations
- Ontology seed from ontology/v0.1.json
- FastAPI skeleton + GET /health + GET /ontology
- **Done when:** `docker compose up` clean, /health = 200, /ontology returns 65 tags

### Phase 2 — Manifest Ingestion
- Pydantic manifest models + validation
- POST /manifests endpoint
- Embedding generation (sentence-transformers)
- All DB writes
- **Done when:** Sample manifest from spec/agent-manifest-v0.1.json returns 201

### Phase 3 — Query API
- GET /services with all filters
- POST /search with pgvector
- Ranking algorithm
- GET /services/{id}
- Redis caching (TTL 60s)
- API key auth middleware
- **Done when:** Structured and semantic queries return ranked results

### Phase 4 — Crawler
- Celery worker setup
- Vector A crawl task
- Vector B domain verification
- Crawl event logging + beat schedule
- **Done when:** DNS verification updates trust_tier 1→2

### Phase 5 — Hardening
- Rate limiting (per key + per IP)
- Input sanitization
- Typosquat detection
- 80%+ test coverage
- Verify FastAPI auto-docs are accurate

#### Running Tests

```bash
# Run all tests from repo root (testpaths configured in pyproject.toml)
pytest -q

# Run with coverage
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

**Windows note:** If the coverage command fails with a permission error writing
`.coverage`, redirect the coverage file to a writable path:

```powershell
$env:COVERAGE_FILE = Join-Path $env:TEMP 'agentledger.coverage'
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

---

## Acceptance Criteria

- [ ] POST /manifests returns 201 for valid manifest
- [ ] Service appears in GET /services?ontology=travel.air.book
- [ ] POST /search "book a flight to New York" returns it in top 3
- [ ] DNS TXT verification updates trust_tier 1→2
- [ ] 3 consecutive crawl failures marks service inactive
- [ ] Exhausted API key quota returns 429
- [ ] Invalid ontology_tag returns 422
- [ ] Sensitive tag (sensitivity_tier >= 3) flagged for review
- [ ] GET /ontology returns all 65 v0.1 tags
- [ ] All endpoints < 500ms p95 under 100 concurrent requests

---

## What Layer 1 Does NOT Include

- Blockchain / on-chain storage (Layer 3)
- Third-party trust attestation (Layer 3)
- Agent identity verification (Layer 2)
- Cross-registry federation (Layer 3)
- Audit chain / liability (Layer 6)
- Payment processing (future)
- OAuth2 auth (v0.2)
- Automated capability probing without opt-in (v0.2)

---

*This spec is the source of truth for Layer 1. Update it before changing any behavior described here.*
