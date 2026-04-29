"""Layer 6 liability snapshots, claims, evidence, and compliance exports.

Revision ID: 007
Revises: 006
Create Date: 2026-04-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE liability_snapshots (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            execution_id UUID NOT NULL UNIQUE REFERENCES workflow_executions(id),
            workflow_id UUID NOT NULL REFERENCES workflows(id),
            agent_did TEXT NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            workflow_quality_score FLOAT NOT NULL,
            workflow_author_did TEXT NOT NULL,
            workflow_validator_did TEXT,
            workflow_validation_checklist JSONB,
            step_trust_states JSONB NOT NULL DEFAULT '[]',
            context_summary JSONB NOT NULL DEFAULT '{}',
            critical_mismatch_count INTEGER NOT NULL DEFAULT 0,
            agent_profile_default_policy TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE liability_claims (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            execution_id UUID NOT NULL REFERENCES workflow_executions(id),
            snapshot_id UUID NOT NULL REFERENCES liability_snapshots(id),
            claimant_did TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            description TEXT NOT NULL,
            harm_value_usd FLOAT,
            status TEXT NOT NULL DEFAULT 'filed',
            reviewer_did TEXT,
            resolution_note TEXT,
            filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            evidence_gathered_at TIMESTAMPTZ,
            determined_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(execution_id, claimant_did)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE liability_evidence (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            claim_id UUID NOT NULL REFERENCES liability_claims(id),
            evidence_type TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_id UUID NOT NULL,
            source_layer INTEGER NOT NULL,
            summary TEXT NOT NULL,
            raw_data JSONB NOT NULL DEFAULT '{}',
            gathered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(claim_id, source_table, source_id)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE liability_determinations (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            claim_id UUID NOT NULL REFERENCES liability_claims(id),
            determination_version INTEGER NOT NULL DEFAULT 1,
            agent_weight FLOAT NOT NULL DEFAULT 0.0,
            service_weight FLOAT NOT NULL DEFAULT 0.0,
            workflow_author_weight FLOAT NOT NULL DEFAULT 0.0,
            validator_weight FLOAT NOT NULL DEFAULT 0.0,
            agent_did TEXT NOT NULL,
            service_id UUID REFERENCES services(id),
            workflow_author_did TEXT,
            validator_did TEXT,
            attribution_factors JSONB NOT NULL DEFAULT '[]',
            confidence FLOAT NOT NULL DEFAULT 0.5,
            determined_by TEXT NOT NULL DEFAULT 'system',
            determined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE compliance_exports (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            export_type TEXT NOT NULL,
            agent_did TEXT,
            service_id UUID REFERENCES services(id),
            execution_id UUID REFERENCES workflow_executions(id),
            claim_id UUID REFERENCES liability_claims(id),
            from_date TIMESTAMPTZ,
            to_date TIMESTAMPTZ,
            record_count INTEGER NOT NULL DEFAULT 0,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute("CREATE INDEX liability_snapshots_execution ON liability_snapshots(execution_id);")
    op.execute("CREATE INDEX liability_claims_execution ON liability_claims(execution_id);")
    op.execute("CREATE INDEX liability_claims_status ON liability_claims(status);")
    op.execute("CREATE INDEX liability_claims_claimant ON liability_claims(claimant_did);")
    op.execute("CREATE INDEX liability_evidence_claim ON liability_evidence(claim_id, source_layer);")
    op.execute(
        """
        CREATE INDEX liability_determinations_claim
        ON liability_determinations(claim_id, determination_version DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX compliance_exports_agent
        ON compliance_exports(agent_did, generated_at DESC)
        WHERE agent_did IS NOT NULL;
        """
    )
    op.execute("CREATE INDEX compliance_exports_type ON compliance_exports(export_type, generated_at DESC);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS compliance_exports CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_determinations CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_evidence CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_claims CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_snapshots CASCADE;")
