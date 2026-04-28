"""Layer 4 context profiles and disclosure tables

Revision ID: 005
Revises: 004
Create Date: 2026-04-27
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE context_profiles (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            agent_did TEXT NOT NULL REFERENCES agent_identities(did),
            profile_name TEXT NOT NULL DEFAULT 'default',
            is_active BOOLEAN NOT NULL DEFAULT true,
            default_policy TEXT NOT NULL DEFAULT 'deny',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(agent_did, profile_name)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE context_profile_rules (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            profile_id UUID NOT NULL REFERENCES context_profiles(id) ON DELETE CASCADE,
            priority INTEGER NOT NULL DEFAULT 100,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            permitted_fields TEXT[] NOT NULL DEFAULT '{}',
            denied_fields TEXT[] NOT NULL DEFAULT '{}',
            action TEXT NOT NULL DEFAULT 'permit',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE context_commitments (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            match_id UUID,
            agent_did TEXT NOT NULL REFERENCES agent_identities(did),
            service_id UUID NOT NULL REFERENCES services(id),
            session_assertion_id UUID REFERENCES session_assertions(id),
            field_name TEXT NOT NULL,
            commitment_hash TEXT NOT NULL,
            nonce TEXT NOT NULL,
            nonce_released BOOLEAN NOT NULL DEFAULT false,
            nonce_released_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            fields_requested TEXT[] NOT NULL DEFAULT '{}',
            fields_permitted TEXT[] NOT NULL DEFAULT '{}',
            fields_withheld TEXT[] NOT NULL DEFAULT '{}',
            fields_committed TEXT[] NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE context_disclosures (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            agent_did TEXT NOT NULL REFERENCES agent_identities(did),
            service_id UUID NOT NULL REFERENCES services(id),
            session_assertion_id UUID REFERENCES session_assertions(id),
            ontology_tag TEXT NOT NULL,
            fields_requested TEXT[] NOT NULL DEFAULT '{}',
            fields_disclosed TEXT[] NOT NULL DEFAULT '{}',
            fields_withheld TEXT[] NOT NULL DEFAULT '{}',
            fields_committed TEXT[] NOT NULL DEFAULT '{}',
            disclosure_method TEXT NOT NULL DEFAULT 'direct',
            trust_score_at_disclosure FLOAT,
            trust_tier_at_disclosure INTEGER,
            profile_id UUID REFERENCES context_profiles(id),
            erased BOOLEAN NOT NULL DEFAULT false,
            erased_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE context_mismatch_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            service_id UUID NOT NULL REFERENCES services(id),
            agent_did TEXT NOT NULL,
            declared_fields TEXT[] NOT NULL DEFAULT '{}',
            requested_fields TEXT[] NOT NULL DEFAULT '{}',
            over_requested_fields TEXT[] NOT NULL DEFAULT '{}',
            severity TEXT NOT NULL DEFAULT 'warning',
            resolved BOOLEAN NOT NULL DEFAULT false,
            resolution_note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        "CREATE INDEX context_profiles_agent ON context_profiles(agent_did) WHERE is_active = true;"
    )
    op.execute(
        "CREATE INDEX context_profile_rules_profile ON context_profile_rules(profile_id, priority);"
    )
    op.execute(
        "CREATE INDEX context_commitments_agent_service ON context_commitments(agent_did, service_id, expires_at);"
    )
    op.execute(
        "CREATE INDEX context_commitments_match ON context_commitments(match_id, agent_did, service_id, expires_at);"
    )
    op.execute(
        "CREATE INDEX context_disclosures_agent ON context_disclosures(agent_did, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX context_disclosures_service ON context_disclosures(service_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX context_mismatch_events_service ON context_mismatch_events(service_id, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX context_mismatch_events_severity ON context_mismatch_events(severity, resolved);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS context_mismatch_events CASCADE;")
    op.execute("DROP TABLE IF EXISTS context_disclosures CASCADE;")
    op.execute("DROP TABLE IF EXISTS context_commitments CASCADE;")
    op.execute("DROP TABLE IF EXISTS context_profile_rules CASCADE;")
    op.execute("DROP TABLE IF EXISTS context_profiles CASCADE;")
