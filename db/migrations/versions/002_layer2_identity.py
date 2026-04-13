"""Layer 2 agent identity foundation

Revision ID: 002
Revises: 001
Create Date: 2026-04-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
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
    """)

    op.execute("""
        CREATE TABLE revocation_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            revoked_by TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    op.execute("CREATE INDEX agent_identities_platform ON agent_identities(issuing_platform);")
    op.execute("CREATE INDEX agent_identities_risk_tier ON agent_identities(risk_tier);")
    op.execute("CREATE INDEX revocation_events_target ON revocation_events(target_type, target_id, created_at DESC);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS revocation_events CASCADE;")
    op.execute("DROP TABLE IF EXISTS agent_identities CASCADE;")
