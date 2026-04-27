"""Layer 3 trust verification and audit chain

Revision ID: 004
Revises: 003
Create Date: 2026-04-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auditors (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            did TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            ontology_scope TEXT[] NOT NULL,
            accreditation_refs JSONB NOT NULL DEFAULT '[]',
            chain_address TEXT,
            credential_hash TEXT,
            is_active BOOLEAN NOT NULL DEFAULT true,
            approved_at TIMESTAMPTZ,
            credential_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE attestation_records (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            service_id UUID NOT NULL REFERENCES services(id),
            auditor_id UUID NOT NULL REFERENCES auditors(id),
            ontology_scope TEXT NOT NULL,
            certification_ref TEXT,
            evidence_hash TEXT NOT NULL,
            tx_hash TEXT NOT NULL UNIQUE,
            block_number BIGINT NOT NULL,
            chain_id INTEGER NOT NULL DEFAULT 137,
            is_confirmed BOOLEAN NOT NULL DEFAULT false,
            confirmed_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            is_active BOOLEAN NOT NULL DEFAULT true,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        "CREATE INDEX attestation_records_service ON attestation_records(service_id, is_active);"
    )
    op.execute("CREATE INDEX attestation_records_auditor ON attestation_records(auditor_id);")
    op.execute(
        """
        CREATE INDEX attestation_records_unconfirmed
        ON attestation_records(is_confirmed, block_number)
        WHERE is_confirmed = false;
        """
    )

    op.execute(
        """
        CREATE TABLE audit_records (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            agent_did TEXT NOT NULL,
            service_id UUID NOT NULL REFERENCES services(id),
            ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
            session_assertion_id UUID REFERENCES session_assertions(id),
            action_context JSONB NOT NULL,
            outcome TEXT NOT NULL,
            outcome_details JSONB NOT NULL DEFAULT '{}',
            record_hash TEXT NOT NULL,
            batch_id UUID,
            merkle_proof JSONB,
            tx_hash TEXT,
            block_number BIGINT,
            is_anchored BOOLEAN NOT NULL DEFAULT false,
            anchored_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    op.execute(
        "CREATE INDEX audit_records_agent ON audit_records(agent_did, created_at DESC);"
    )
    op.execute(
        "CREATE INDEX audit_records_service ON audit_records(service_id, created_at DESC);"
    )
    op.execute(
        """
        CREATE INDEX audit_records_unanchored
        ON audit_records(is_anchored, created_at)
        WHERE is_anchored = false;
        """
    )
    op.execute("CREATE INDEX audit_records_batch ON audit_records(batch_id);")

    op.execute(
        """
        CREATE TABLE audit_batches (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            merkle_root TEXT NOT NULL,
            record_count INTEGER NOT NULL,
            tx_hash TEXT UNIQUE,
            block_number BIGINT,
            chain_id INTEGER NOT NULL DEFAULT 137,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            submitted_at TIMESTAMPTZ,
            confirmed_at TIMESTAMPTZ
        );
        """
    )

    op.execute(
        """
        CREATE TABLE federated_registries (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            webhook_url TEXT,
            public_key_pem TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            last_push_at TIMESTAMPTZ,
            last_push_status TEXT,
            push_failure_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE chain_events (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            event_type TEXT NOT NULL,
            service_id UUID REFERENCES services(id),
            tx_hash TEXT NOT NULL UNIQUE,
            block_number BIGINT NOT NULL,
            chain_id INTEGER NOT NULL DEFAULT 137,
            is_confirmed BOOLEAN NOT NULL DEFAULT false,
            event_data JSONB NOT NULL,
            indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            confirmed_at TIMESTAMPTZ
        );
        """
    )
    op.execute("CREATE INDEX chain_events_service ON chain_events(service_id, event_type);")
    op.execute("CREATE INDEX chain_events_block ON chain_events(block_number DESC);")
    op.execute(
        """
        CREATE INDEX chain_events_unconfirmed
        ON chain_events(is_confirmed, block_number)
        WHERE is_confirmed = false;
        """
    )

    op.execute(
        """
        ALTER TABLE audit_records
        ADD CONSTRAINT audit_records_batch_fk
        FOREIGN KEY (batch_id) REFERENCES audit_batches(id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chain_events CASCADE;")
    op.execute("DROP TABLE IF EXISTS federated_registries CASCADE;")
    op.execute("DROP TABLE IF EXISTS audit_batches CASCADE;")
    op.execute("DROP TABLE IF EXISTS audit_records CASCADE;")
    op.execute("DROP TABLE IF EXISTS attestation_records CASCADE;")
    op.execute("DROP TABLE IF EXISTS auditors CASCADE;")
