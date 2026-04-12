"""Initial Layer 1 schema

Revision ID: 001
Revises: None
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Run the full schema SQL — kept in sync with db/schema.sql
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.execute("""
        CREATE TABLE ontology_tags (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            tag TEXT UNIQUE NOT NULL,
            domain TEXT NOT NULL,
            function TEXT NOT NULL,
            label TEXT NOT NULL,
            description TEXT NOT NULL,
            sensitivity_tier INTEGER NOT NULL DEFAULT 1
        );
    """)

    op.execute("""
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
    """)

    op.execute("""
        CREATE TABLE manifests (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            service_id UUID NOT NULL REFERENCES services(id),
            raw_json JSONB NOT NULL,
            manifest_hash TEXT NOT NULL,
            manifest_version TEXT,
            is_current BOOLEAN NOT NULL DEFAULT true,
            crawled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    op.execute("""
        CREATE UNIQUE INDEX manifests_service_current
            ON manifests(service_id) WHERE is_current = true;
    """)

    op.execute("""
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
    """)

    op.execute("""
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
    """)

    op.execute("""
        CREATE TABLE service_context_requirements (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            service_id UUID NOT NULL REFERENCES services(id),
            field_name TEXT NOT NULL,
            field_type TEXT NOT NULL,
            is_required BOOLEAN NOT NULL DEFAULT false,
            sensitivity TEXT NOT NULL DEFAULT 'low',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    op.execute("""
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
    """)

    op.execute("""
        CREATE TABLE crawl_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            service_id UUID REFERENCES services(id),
            event_type TEXT NOT NULL,
            domain TEXT,
            details JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    op.execute("""
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
    """)

    # Indexes
    op.execute("CREATE INDEX services_trust_tier ON services(trust_tier);")
    op.execute("CREATE INDEX services_trust_score ON services(trust_score DESC);")
    op.execute("CREATE INDEX services_domain ON services(domain);")
    op.execute("CREATE INDEX service_capabilities_tag ON service_capabilities(ontology_tag);")
    op.execute("""
        CREATE INDEX service_capabilities_embedding ON service_capabilities
            USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
    """)
    op.execute("CREATE INDEX crawl_events_service ON crawl_events(service_id, created_at DESC);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS crawl_events CASCADE;")
    op.execute("DROP TABLE IF EXISTS api_keys CASCADE;")
    op.execute("DROP TABLE IF EXISTS service_operations CASCADE;")
    op.execute("DROP TABLE IF EXISTS service_context_requirements CASCADE;")
    op.execute("DROP TABLE IF EXISTS service_pricing CASCADE;")
    op.execute("DROP TABLE IF EXISTS service_capabilities CASCADE;")
    op.execute("DROP TABLE IF EXISTS manifests CASCADE;")
    op.execute("DROP TABLE IF EXISTS services CASCADE;")
    op.execute("DROP TABLE IF EXISTS ontology_tags CASCADE;")
