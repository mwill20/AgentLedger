"""Layer 5 workflow registry and validation tables.

Revision ID: 006
Revises: 005
Create Date: 2026-04-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE workflows (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            ontology_domain TEXT NOT NULL,
            tags TEXT[] NOT NULL DEFAULT '{}',
            spec JSONB NOT NULL,
            spec_version TEXT NOT NULL DEFAULT '1.0',
            spec_hash TEXT,
            author_did TEXT NOT NULL REFERENCES agent_identities(did),
            status TEXT NOT NULL DEFAULT 'draft',
            quality_score FLOAT NOT NULL DEFAULT 0.0,
            execution_count BIGINT NOT NULL DEFAULT 0,
            success_count BIGINT NOT NULL DEFAULT 0,
            failure_count BIGINT NOT NULL DEFAULT 0,
            parent_workflow_id UUID REFERENCES workflows(id),
            published_at TIMESTAMPTZ,
            deprecated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE workflow_steps (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            workflow_id UUID NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            step_number INTEGER NOT NULL,
            name TEXT NOT NULL,
            ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
            service_id UUID REFERENCES services(id),
            is_required BOOLEAN NOT NULL DEFAULT true,
            fallback_step_number INTEGER,
            context_fields_required TEXT[] NOT NULL DEFAULT '{}',
            context_fields_optional TEXT[] NOT NULL DEFAULT '{}',
            min_trust_tier INTEGER NOT NULL DEFAULT 2,
            min_trust_score FLOAT NOT NULL DEFAULT 50.0,
            timeout_seconds INTEGER NOT NULL DEFAULT 30,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(workflow_id, step_number)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE workflow_validations (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            workflow_id UUID NOT NULL REFERENCES workflows(id),
            validator_did TEXT NOT NULL,
            validator_domain TEXT NOT NULL,
            assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decision TEXT,
            decision_at TIMESTAMPTZ,
            rejection_reason TEXT,
            revision_notes TEXT,
            checklist JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE workflow_executions (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            workflow_id UUID NOT NULL REFERENCES workflows(id),
            agent_did TEXT NOT NULL REFERENCES agent_identities(did),
            context_bundle_id UUID,
            outcome TEXT NOT NULL,
            steps_completed INTEGER NOT NULL DEFAULT 0,
            steps_total INTEGER NOT NULL,
            failure_step_number INTEGER,
            failure_reason TEXT,
            duration_ms INTEGER,
            reported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            verified BOOLEAN NOT NULL DEFAULT false,
            verified_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE workflow_scoped_profiles (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            workflow_id UUID NOT NULL REFERENCES workflows(id),
            agent_did TEXT NOT NULL REFERENCES agent_identities(did),
            base_profile_id UUID REFERENCES context_profiles(id),
            overrides JSONB NOT NULL DEFAULT '{}',
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(workflow_id, agent_did)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE workflow_context_bundles (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            workflow_id UUID NOT NULL REFERENCES workflows(id),
            agent_did TEXT NOT NULL REFERENCES agent_identities(did),
            scoped_profile_id UUID REFERENCES workflow_scoped_profiles(id),
            status TEXT NOT NULL DEFAULT 'pending',
            approved_fields JSONB NOT NULL DEFAULT '{}',
            user_approved_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    op.execute("CREATE INDEX workflows_status ON workflows(status) WHERE status = 'published';")
    op.execute("CREATE INDEX workflows_domain ON workflows(ontology_domain);")
    op.execute(
        "CREATE INDEX workflows_quality ON workflows(quality_score DESC) WHERE status = 'published';"
    )
    op.execute("CREATE INDEX workflows_tags ON workflows USING GIN(tags);")
    op.execute("CREATE INDEX workflow_steps_workflow ON workflow_steps(workflow_id, step_number);")
    op.execute(
        """
        CREATE INDEX workflow_validations_pending ON workflow_validations(workflow_id)
        WHERE decision IS NULL;
        """
    )
    op.execute(
        "CREATE INDEX workflow_executions_workflow ON workflow_executions(workflow_id, reported_at DESC);"
    )
    op.execute(
        "CREATE INDEX workflow_context_bundles_agent ON workflow_context_bundles(agent_did, status);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workflow_context_bundles CASCADE;")
    op.execute("DROP TABLE IF EXISTS workflow_scoped_profiles CASCADE;")
    op.execute("DROP TABLE IF EXISTS workflow_executions CASCADE;")
    op.execute("DROP TABLE IF EXISTS workflow_validations CASCADE;")
    op.execute("DROP TABLE IF EXISTS workflow_steps CASCADE;")
    op.execute("DROP TABLE IF EXISTS workflows CASCADE;")
