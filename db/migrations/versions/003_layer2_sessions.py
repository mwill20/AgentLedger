"""Layer 2 session assertion engine

Revision ID: 003
Revises: 002
Create Date: 2026-04-13
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
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
    """)

    op.execute("""
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
    """)

    op.execute("CREATE INDEX authorization_requests_status ON authorization_requests(status, expires_at);")
    op.execute("CREATE INDEX authorization_requests_service ON authorization_requests(service_id, status);")
    op.execute("CREATE INDEX session_assertions_agent ON session_assertions(agent_did, expires_at);")
    op.execute("CREATE INDEX session_assertions_service ON session_assertions(service_id, expires_at);")
    op.execute("CREATE INDEX session_assertions_expires ON session_assertions(expires_at);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_assertions CASCADE;")
    op.execute("DROP TABLE IF EXISTS authorization_requests CASCADE;")
