# 🎓 Lesson 02: The Vault — Database Schema and Ontology

> **Beginner frame:** A database schema is the evidence room layout: every shelf, label, and relationship decides whether records stay useful later. AgentLedger uses this structure to keep service manifests, ontology tags, search vectors, trust fields, and operational history consistent enough to audit.

## 🛡️ Welcome Back, Data Architect!

Where does AgentLedger actually keep all the services, manifests, trust scores, and embeddings? 🔍 Today we're exploring the **database schema** — the "vault" that holds every piece of data in the system.

**Goal:** Understand every table, column, index, and relationship in the Layer 1 database.  
**Time:** 60 minutes  
**Prerequisites:** Lesson 01 (The Big Picture), basic SQL knowledge  
**Why this matters:** The schema IS the data model. Every API endpoint maps directly to these tables. If you don't understand the schema, you can't understand the queries.

---

## 🎯 Learning Objectives

- Name all 9 tables in Layer 1 and explain each one's purpose ✅
- Trace how a single manifest registration touches 6 tables ✅
- Explain why `service_capabilities.embedding` is `vector(384)` ✅
- Understand the ontology seeding process ✅
- Read the Alembic migration files ✅
- Explain the trust tier progression (1 → 2 → 3 → 4) ✅

---

## 🔍 How the Schema Connects to the System

```
📁 Manifest Arrives     🧠 Embedder          🔍 Query Engine
(POST /manifests)       (all-MiniLM-L6-v2)   (GET /services, POST /search)
       |                      |                      |
       v                      v                      v
┌─────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  services   │--->│ service_         │--->│ pgvector cosine  │
│  manifests  │    │ capabilities     │    │ search on        │
│  pricing    │    │ (with embedding) │    │ embedding column │
│  context    │    └──────────────────┘    └──────────────────┘
│  operations │
└─────────────┘
       ^
       |
┌─────────────┐    ┌──────────────────┐
│ ontology_   │    │   crawl_events   │
│ tags (65)   │    │   (event log)    │
└─────────────┘    └──────────────────┘
       ^                    ^
       |                    |
  Seeded at startup    Written by crawler
  (seed_ontology.py)   (crawl.py, verify_domain.py)
```

---

## 📝 Code Walkthrough: The Schema

All tables are defined in `db/schema.sql`. Let's walk through each one.

### Table 1: `ontology_tags` — The Taxonomy

```sql
-- db/schema.sql, Lines 10-18
-- Capability ontology (seeded from ontology/v0.1.json, not user-editable)
CREATE TABLE ontology_tags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tag TEXT UNIQUE NOT NULL,              -- e.g. "travel.air.book"
    domain TEXT NOT NULL,                  -- e.g. "TRAVEL"
    function TEXT NOT NULL,                -- e.g. "travel.air"
    label TEXT NOT NULL,                   -- e.g. "Book a flight"
    description TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1  -- 1=low, 2=medium, 3=high, 4=critical
);
```

🔍 **Line-by-Line:**
- `tag TEXT UNIQUE NOT NULL` — The hierarchical identifier like `travel.air.book`. Three levels: `domain.function.action`. This is the primary lookup key.
- `sensitivity_tier INTEGER` — Tags with tier >= 3 push a newly registered manifest into `pending_review`, which keeps the service inactive until a separate activation path runs. For example, `health.records.retrieve` is tier 3 (accessing medical records requires scrutiny).
- This table is **read-only at runtime**. It's seeded once from `ontology/v0.1.json` and never modified by API calls.

**65 tags across 5 domains:**

| Domain | Example Tags | Count |
|--------|-------------|-------|
| TRAVEL | `travel.air.book`, `travel.hotel.search` | 13 |
| FINANCE | `finance.payments.send`, `finance.banking.transfer` | 13 |
| HEALTH | `health.records.retrieve`, `health.rx.order` | 13 |
| COMMERCE | `commerce.marketplace.list`, `commerce.payments.process` | 13 |
| PRODUCTIVITY | `productivity.calendar.schedule`, `productivity.email.send` | 13 |

### Table 2: `services` — The Core Registry

```sql
-- db/schema.sql, Lines 21-38
CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,                         -- "FlightBookerPro"
    domain TEXT NOT NULL UNIQUE,                -- "flightbookerpro.com"
    legal_entity TEXT,                          -- "FlightBooker Inc."
    manifest_url TEXT NOT NULL,                 -- "https://flightbookerpro.com/.well-known/agent-manifest.json"
    public_key TEXT,                            -- PEM/JWK (stored, not validated in L1)
    trust_tier INTEGER NOT NULL DEFAULT 1,      -- 1=crawled, 2=domain_verified, 3=probed, 4=attested
    trust_score FLOAT NOT NULL DEFAULT 0.0,     -- 0.0 - 100.0
    is_active BOOLEAN NOT NULL DEFAULT true,    -- false after 3 crawl failures
    is_banned BOOLEAN NOT NULL DEFAULT false,   -- admin ban
    ban_reason TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_crawled_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

🔍 **Key Design Decisions:**
- `domain TEXT NOT NULL UNIQUE` — One service per domain. Prevents two different `service_id`s from claiming `flightbookerpro.com`.
- `trust_tier INTEGER` — Progresses through verification stages:

```
  Tier 1: Crawled          ← Service submitted a manifest
  Tier 2: Domain Verified  ← DNS TXT record confirms ownership
  Tier 3: Capability Probed← Synthetic tests verify claimed capabilities
  Tier 4: Ledger Attested  ← (Layer 2+) Third-party attestation on-chain
```

- `public_key TEXT` — Stored but **not validated** in Layer 1. This is the integration point for Layer 2 identity.
- `is_active` + `is_banned` — Two separate flags. A service can be inactive (3 crawl failures) but not banned. A banned service was manually flagged.

`★ Insight ─────────────────────────────────────`
**Why separate `is_active` and `is_banned`?** Because they have different resolution paths. `is_active=false` gets auto-recovered when the next crawl succeeds. `is_banned=true` requires an admin action. Conflating them into a single status field would make it impossible to auto-recover a temporarily-offline service that was also manually banned.
`─────────────────────────────────────────────────`

### Table 3: `manifests` — Versioned Manifest Storage

```sql
-- db/schema.sql, Lines 41-52
CREATE TABLE manifests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    raw_json JSONB NOT NULL,                    -- The full manifest as submitted
    manifest_hash TEXT NOT NULL,                -- SHA-256 for change detection
    manifest_version TEXT,                      -- "1.0"
    is_current BOOLEAN NOT NULL DEFAULT true,   -- Only one current per service
    crawled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial unique index: only one current manifest per service
CREATE UNIQUE INDEX manifests_service_current
    ON manifests(service_id) WHERE is_current = true;
```

🔍 **Key Design Decisions:**
- `raw_json JSONB` — Stores the manifest exactly as submitted. This is the audit trail. Old manifests are never deleted, just marked `is_current = false`.
- `manifest_hash TEXT` — SHA-256 of the sorted JSON payload. The crawler uses this to detect changes without comparing the full JSONB.
- The **partial unique index** `WHERE is_current = true` is a PostgreSQL feature that ensures exactly one "current" manifest per service, while allowing unlimited historical manifests.

`★ Insight ─────────────────────────────────────`
**Why JSONB instead of normalized columns?** The manifest spec may evolve across versions. Storing the raw JSON means the database doesn't need schema migrations when new manifest fields are added. The structured data (capabilities, pricing, etc.) is also stored in normalized tables for efficient querying — it's a "store both" pattern common in document-relational hybrid designs.
`─────────────────────────────────────────────────`

### Table 4: `service_capabilities` — Capability Claims with Embeddings

```sql
-- db/schema.sql, Lines 55-69
CREATE TABLE service_capabilities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),  -- FK to ontology
    description TEXT,
    embedding vector(384),              -- all-MiniLM-L6-v2 output dimension
    input_schema_url TEXT,
    output_schema_url TEXT,
    success_rate_30d FLOAT,             -- rolling 30-day success rate
    avg_latency_ms INTEGER,             -- rolling average latency
    is_verified BOOLEAN NOT NULL DEFAULT false,  -- set by capability probing
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(service_id, ontology_tag)     -- one claim per tag per service
);
```

🔍 **This is the most important table for search.** Key columns:
- `embedding vector(384)` — The 384-dimensional vector produced by `all-MiniLM-L6-v2` from the capability `description`. This is what makes semantic search possible.
- `ontology_tag TEXT REFERENCES ontology_tags(tag)` — Foreign key ensures only valid tags can be claimed.
- `UNIQUE(service_id, ontology_tag)` — A service can only claim each capability once.
- `success_rate_30d` and `avg_latency_ms` — Fed by capability probing (currently populated by Layer 1 but primarily used by the ranking algorithm).

### Table 5: `service_pricing` — Economics

```sql
-- db/schema.sql, Lines 72-81
CREATE TABLE service_pricing (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    pricing_model TEXT NOT NULL,        -- per_transaction|subscription|freemium|free
    tiers JSONB NOT NULL DEFAULT '[]',  -- flexible tier structure
    billing_method TEXT,                -- x402|stripe|api_key|none
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Table 6: `service_context_requirements` — What Data Does the Service Need?

```sql
-- db/schema.sql, Lines 84-92
CREATE TABLE service_context_requirements (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    field_name TEXT NOT NULL,            -- e.g. "traveler_name"
    field_type TEXT NOT NULL,            -- e.g. "string"
    is_required BOOLEAN NOT NULL DEFAULT false,
    sensitivity TEXT NOT NULL DEFAULT 'low',  -- low|medium|high|critical
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

This tells agents what data a service needs before it can act. A flight booking service might require `traveler_name` (required) and `loyalty_number` (optional).

### Table 7: `service_operations` — Operational Metadata

```sql
-- db/schema.sql, Lines 95-107
CREATE TABLE service_operations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL UNIQUE REFERENCES services(id),  -- one row per service
    uptime_sla_percent FLOAT,
    rate_limit_rpm INTEGER,
    rate_limit_rpd INTEGER,
    geo_restrictions TEXT[] DEFAULT '{}',       -- PostgreSQL array
    compliance_certs TEXT[] DEFAULT '{}',
    sandbox_url TEXT,
    deprecation_notice_days INTEGER DEFAULT 30,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Note: `UNIQUE REFERENCES services(id)` — This is a one-to-one relationship enforced at the schema level.

### Table 8: `crawl_events` — The Audit Trail

```sql
-- db/schema.sql, Lines 110-117
CREATE TABLE crawl_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID REFERENCES services(id),
    event_type TEXT NOT NULL,            -- crawl_success|crawl_failure|domain_verified|...
    domain TEXT,
    details JSONB DEFAULT '{}',         -- flexible event payload
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

This is an **append-only log**. Events are never updated or deleted. The crawler writes here on every crawl attempt, and the domain verifier writes here on every DNS check.

### Table 9: `api_keys` — Authentication

```sql
-- db/schema.sql, Lines 120-130
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash TEXT UNIQUE NOT NULL,       -- SHA-256 of the actual key
    name TEXT NOT NULL,
    owner TEXT,
    query_count BIGINT NOT NULL DEFAULT 0,
    monthly_limit BIGINT DEFAULT 1000000,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);
```

`★ Insight ─────────────────────────────────────`
**Why `key_hash` not the raw key?** The same reason you hash passwords. If the database is compromised, the attacker gets hashes, not usable API keys. The hash is SHA-256 of the key, computed at registration time. On each request, the submitted key is hashed and compared.
`─────────────────────────────────────────────────`

---

## 📝 Code Walkthrough: Indexes

```sql
-- db/schema.sql, Lines 163-169
CREATE INDEX services_trust_tier ON services(trust_tier);
CREATE INDEX services_trust_score ON services(trust_score DESC);
CREATE INDEX services_domain ON services(domain);
CREATE INDEX service_capabilities_tag ON service_capabilities(ontology_tag);
CREATE INDEX service_capabilities_embedding ON service_capabilities
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX crawl_events_service ON crawl_events(service_id, created_at DESC);
```

The critical index is `service_capabilities_embedding` — this is an **IVFFlat** index for approximate nearest neighbor (ANN) search on the 384-dim vectors. `lists = 100` means the vectors are partitioned into 100 clusters. At query time, pgvector only searches the closest clusters instead of every vector.

**Recommended (not implemented here):** For production at scale, consider adding `HNSW` indexes (available in pgvector 0.5+) which provide better recall-vs-speed trade-offs than IVFFlat at higher vector counts.

---

## 📝 Code Walkthrough: Ontology Seeding

The ontology is loaded at container startup by `db/seed_ontology.py`:

```python
# db/seed_ontology.py — Key section (Lines 48-56)
upsert_sql = """
    INSERT INTO ontology_tags (tag, domain, function, label, description, sensitivity_tier)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (tag) DO UPDATE SET
        domain = EXCLUDED.domain,
        function = EXCLUDED.function,
        label = EXCLUDED.label,
        description = EXCLUDED.description,
        sensitivity_tier = EXCLUDED.sensitivity_tier;
"""
```

This is **idempotent** — safe to re-run. The `ON CONFLICT ... DO UPDATE` pattern (upsert) means:
- If the tag doesn't exist: INSERT it
- If it already exists: UPDATE its fields to match the JSON source

The source of truth is `ontology/v0.1.json`, a flat JSON file with all 65 tags.

---

## 📝 Code Walkthrough: Alembic Migration

The initial migration lives at `db/migrations/versions/001_initial_schema.py`. Alembic handles schema versioning:

```
Container starts → entrypoint.sh → alembic upgrade head → seed_ontology.py → uvicorn
```

This means:
1. First run: Creates all tables
2. Subsequent runs: No-op (schema already at latest version)
3. New migration added: Automatically applied on next container restart

---

## 🧪 Hands-On Exercises

### 🔬 Exercise 1: Explore the Schema

With the Docker stack running, connect to PostgreSQL:

```powershell
docker compose exec db psql -U agentledger -d agentledger -c "\dt"
```

📊 **Expected Output:**
```
              List of relations
 Schema |             Name              | Type  |    Owner
--------+-------------------------------+-------+-------------
 public | alembic_version               | table | agentledger
 public | api_keys                      | table | agentledger
 public | crawl_events                  | table | agentledger
 public | manifests                     | table | agentledger
 public | ontology_tags                 | table | agentledger
 public | service_capabilities          | table | agentledger
 public | service_context_requirements  | table | agentledger
 public | service_operations            | table | agentledger
 public | service_pricing               | table | agentledger
 public | services                      | table | agentledger
```

### 🔬 Exercise 2: Query the Ontology

```powershell
docker compose exec db psql -U agentledger -d agentledger -c "SELECT tag, sensitivity_tier FROM ontology_tags WHERE sensitivity_tier >= 3 ORDER BY tag;"
```

📊 **Expected Output:** A list of high-sensitivity tags (medical records, financial transfers, etc.)

### 🔬 Exercise 3: Trace a Registration

After registering a manifest via the API, check how many tables were touched:

```powershell
docker compose exec db psql -U agentledger -d agentledger -c "SELECT 'services' as tbl, count(*) FROM services UNION ALL SELECT 'manifests', count(*) FROM manifests UNION ALL SELECT 'service_capabilities', count(*) FROM service_capabilities UNION ALL SELECT 'service_pricing', count(*) FROM service_pricing UNION ALL SELECT 'service_context_requirements', count(*) FROM service_context_requirements UNION ALL SELECT 'service_operations', count(*) FROM service_operations;"
```

---

## 📚 Interview Prep

**Q: How does AgentLedger store and search vector embeddings?**

**A:** Each capability description is embedded into a 384-dimensional vector using `all-MiniLM-L6-v2` and stored in the `service_capabilities.embedding` column, which uses PostgreSQL's `pgvector` extension with type `vector(384)`. At search time, the query is embedded and pgvector computes cosine distance (`<=>` operator) against all stored embeddings, using an IVFFlat index with 100 lists for approximate nearest neighbor search.

---

**Q: Why does the schema store manifests as both raw JSONB and normalized tables?**

**A:** The raw JSONB in `manifests.raw_json` is the audit trail — the exact payload as submitted, never modified. The normalized tables (`service_capabilities`, `service_pricing`, etc.) enable efficient SQL queries and joins. This dual-storage pattern means the query engine benefits from relational indexes while the audit system preserves complete fidelity.

---

**Q: Explain the trust tier system.**

**A:** Trust is earned through progressive verification: Tier 1 (Crawled) means a manifest was submitted. Tier 2 (Domain Verified) means the service proved domain ownership via DNS TXT record. Tier 3 (Capability Probed) means synthetic tests confirmed the service actually does what it claims. Tier 4 (Ledger Attested) is reserved for Layer 2+ where third-party auditors provide on-chain attestation. Each tier unlocks higher trust scores and better search ranking.

---

## 🎯 Key Takeaways

- 9 tables: `ontology_tags`, `services`, `manifests`, `service_capabilities`, `service_pricing`, `service_context_requirements`, `service_operations`, `crawl_events`, `api_keys`
- One manifest registration touches 6 tables (services, manifests, capabilities, pricing, context, operations)
- `vector(384)` column with IVFFlat index enables semantic search inside PostgreSQL
- Ontology is seeded from a JSON file and is read-only at runtime
- Manifests are versioned (never deleted) — audit trail by design
- Trust tiers progress 1 → 2 → 3 → 4 through increasing verification

---

## 🚀 Ready for Lesson 03?

Next up, we'll explore **Mission Control** — the configuration system (`config.py`) and dependency injection (`dependencies.py`) that wire everything together. Get ready to see how settings flow from environment variables into the running application! ⚙️

*Remember: The schema IS the data model — if you understand these 9 tables, you understand what AgentLedger knows!* 🛡️
