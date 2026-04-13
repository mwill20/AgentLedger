-- AgentLedger Layer 1 — Database Schema
-- Version: 0.1
-- Run in order. All tables use UUID primary keys.

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- Capability ontology (seeded from ontology/v0.1.json, not user-editable)
CREATE TABLE ontology_tags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tag TEXT UNIQUE NOT NULL,
    domain TEXT NOT NULL,
    function TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1
);

-- Core service registry
CREATE TABLE services (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    legal_entity TEXT,
    manifest_url TEXT NOT NULL,
    public_key TEXT,
    trust_tier INTEGER NOT NULL DEFAULT 1,
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
    embedding vector(384),
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

-- Layer 2: agent identities
CREATE TABLE agent_identities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    did TEXT UNIQUE NOT NULL,
    agent_name TEXT NOT NULL,
    issuing_platform TEXT,
    public_key_jwk JSONB NOT NULL,
    capability_scope TEXT[] NOT NULL DEFAULT '{}',
    risk_tier TEXT NOT NULL DEFAULT 'standard',
    credential_hash TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    revoked_at TIMESTAMPTZ,
    revocation_reason TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    credential_expires_at TIMESTAMPTZ
);

-- Layer 2: revocation event log
CREATE TABLE revocation_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    revoked_by TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Layer 2: human approval queue for sensitive capabilities
CREATE TABLE authorization_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did TEXT NOT NULL,
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    sensitivity_tier INTEGER NOT NULL,
    request_context JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    approver_id TEXT,
    decided_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Layer 2: short-lived session assertions
CREATE TABLE session_assertions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    assertion_jti TEXT UNIQUE NOT NULL,
    agent_did TEXT NOT NULL REFERENCES agent_identities(did),
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    assertion_token TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    authorization_ref UUID REFERENCES authorization_requests(id),
    was_used BOOLEAN NOT NULL DEFAULT false,
    used_at TIMESTAMPTZ
);

-- Indexes
CREATE INDEX services_trust_tier ON services(trust_tier);
CREATE INDEX services_trust_score ON services(trust_score DESC);
CREATE INDEX services_domain ON services(domain);
CREATE INDEX service_capabilities_tag ON service_capabilities(ontology_tag);
CREATE INDEX service_capabilities_embedding ON service_capabilities
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX crawl_events_service ON crawl_events(service_id, created_at DESC);
CREATE INDEX agent_identities_platform ON agent_identities(issuing_platform);
CREATE INDEX agent_identities_risk_tier ON agent_identities(risk_tier);
CREATE INDEX authorization_requests_status ON authorization_requests(status, expires_at);
CREATE INDEX authorization_requests_service ON authorization_requests(service_id, status);
CREATE INDEX session_assertions_agent ON session_assertions(agent_did, expires_at);
CREATE INDEX session_assertions_service ON session_assertions(service_id, expires_at);
CREATE INDEX session_assertions_expires ON session_assertions(expires_at);
CREATE INDEX revocation_events_target ON revocation_events(target_type, target_id, created_at DESC);
