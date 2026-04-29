"""Tests for Layer 6 regulatory compliance PDF exports."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from api.dependencies import get_db
from api.main import app

AGENT_DID = "did:key:z6MkLiabilityComplianceAgent"


class _FakeMappings:
    """Minimal mappings wrapper for compliance tests."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    """Minimal SQLAlchemy result wrapper."""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _ComplianceSession:
    """SQL-aware fake DB for liability compliance export tests."""

    def __init__(self, ontology_tag: str = "health.pharmacy.order", sensitivity_tier: int = 3):
        self.execution_id = uuid4()
        self.workflow_id = uuid4()
        self.service_id = uuid4()
        self.snapshot_id = uuid4()
        self.bundle_id = uuid4()
        self.session_assertion_id = uuid4()
        self.claim_id = uuid4()
        self.manifest_id = uuid4()
        self.evidence_id = uuid4()
        self.disclosure_id = uuid4()
        self.ontology_tag = ontology_tag
        self.sensitivity_tier = sensitivity_tier
        self.now = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        self.executed = []
        self.log_inserts = []
        self.commit_count = 0
        self.rollback_count = 0

    def _execution(self):
        return {
            "id": self.execution_id,
            "workflow_id": self.workflow_id,
            "agent_did": AGENT_DID,
            "outcome": "success",
            "steps_completed": 1,
            "steps_total": 1,
            "failure_step_number": None,
            "failure_reason": None,
            "duration_ms": 1200,
            "reported_at": self.now,
            "verified": True,
            "workflow_name": "Regulated Workflow",
        }

    def _step(self):
        return {
            "id": uuid4(),
            "workflow_id": self.workflow_id,
            "step_number": 1,
            "name": "Regulated step",
            "ontology_tag": self.ontology_tag,
            "service_id": self.service_id,
            "is_required": True,
            "fallback_step_number": None,
            "context_fields_required": ["user.name"],
            "context_fields_optional": [],
            "min_trust_tier": 3,
            "min_trust_score": 75.0,
            "sensitivity_tier": self.sensitivity_tier,
        }

    def _snapshot(self):
        return {
            "id": self.snapshot_id,
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "captured_at": self.now,
            "workflow_quality_score": 88.0,
            "workflow_author_did": "did:key:z6MkAuthor",
            "workflow_validator_did": "did:key:z6MkValidator",
            "step_trust_states": [
                {
                    "step_number": 1,
                    "ontology_tag": self.ontology_tag,
                    "service_id": self.service_id,
                    "service_name": "RegulatedService",
                    "min_trust_tier": 3,
                    "min_trust_score": 75.0,
                    "trust_tier": 4,
                    "trust_score": 91.0,
                }
            ],
            "context_summary": {"fields_disclosed": ["user.name"]},
            "critical_mismatch_count": 0,
        }

    async def execute(self, statement, params=None):
        sql = statement.text if hasattr(statement, "text") else str(statement)
        params = params or {}
        self.executed.append((sql, params))

        if "FROM workflow_executions we" in sql and "WHERE we.id = :execution_id" in sql:
            return _FakeResult([self._execution()])
        if "FROM workflow_steps ws" in sql:
            return _FakeResult([self._step()])
        if "FROM liability_snapshots" in sql:
            return _FakeResult([self._snapshot()])
        if "FROM context_disclosures" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.disclosure_id,
                        "agent_did": AGENT_DID,
                        "service_id": self.service_id,
                        "ontology_tag": self.ontology_tag,
                        "fields_requested": ["user.name"],
                        "fields_disclosed": ["user.name"],
                        "fields_withheld": [],
                        "fields_committed": [],
                        "disclosure_method": "direct",
                        "trust_score_at_disclosure": 91.0,
                        "trust_tier_at_disclosure": 4,
                        "erased": False,
                        "created_at": self.now,
                    }
                ]
            )
        if "FROM workflow_context_bundles" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.bundle_id,
                        "workflow_id": self.workflow_id,
                        "approved_fields": {"all_permitted": ["user.name"]},
                        "user_approved_at": self.now,
                        "created_at": self.now,
                    }
                ]
            )
        if "FROM session_assertions" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.session_assertion_id,
                        "agent_did": AGENT_DID,
                        "service_id": self.service_id,
                        "ontology_tag": self.ontology_tag,
                        "issued_at": self.now,
                        "expires_at": self.now,
                        "authorization_ref": uuid4(),
                        "was_used": True,
                        "used_at": self.now,
                    }
                ]
            )
        if "FROM liability_claims" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.claim_id,
                        "execution_id": self.execution_id,
                        "claim_type": "service_failure",
                        "status": "determined",
                        "filed_at": self.now,
                        "determined_at": self.now,
                        "resolved_at": None,
                    }
                ]
            )
        if "FROM liability_evidence" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.evidence_id,
                        "claim_id": self.claim_id,
                        "evidence_type": "service_capability",
                        "source_layer": 1,
                        "summary": "Capability verified",
                        "raw_data": {"is_verified": True},
                        "gathered_at": self.now,
                    }
                ]
            )
        if "FROM manifests" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.manifest_id,
                        "service_id": self.service_id,
                        "manifest_hash": "hash",
                        "manifest_version": "1.0",
                        "crawled_at": self.now,
                    }
                ]
            )
        if "INSERT INTO compliance_exports" in sql:
            self.log_inserts.append(params)
            return _FakeResult([])

        return _FakeResult([])

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _get_export(session: _ComplianceSession, api_key_headers, export_type: str):
    """Call the liability compliance export route with a fake DB session."""

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            return client.get(
                (
                    "/v1/liability/compliance/export"
                    f"?export_type={export_type}&execution_id={session.execution_id}"
                ),
                headers=api_key_headers,
            )
    finally:
        app.dependency_overrides.clear()


def test_eu_ai_act_export_returns_valid_pdf_and_logs_export(api_key_headers):
    """EU AI Act export should return PDF bytes and create compliance_exports log."""
    session = _ComplianceSession(ontology_tag="health.pharmacy.order", sensitivity_tier=3)

    response = _get_export(session, api_key_headers, "eu_ai_act")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="agentledger_eu_ai_act_'
    )
    assert response.content.startswith(b"%PDF-")
    assert len(session.log_inserts) == 1
    assert session.log_inserts[0]["export_type"] == "eu_ai_act"
    assert session.log_inserts[0]["record_count"] == 1
    assert session.commit_count == 1


def test_hipaa_export_rejects_scope_without_health_tags(api_key_headers):
    """HIPAA export should reject executions without health.* ontology tags."""
    session = _ComplianceSession(ontology_tag="travel.air.book", sensitivity_tier=1)

    response = _get_export(session, api_key_headers, "hipaa")

    assert response.status_code == 400
    assert response.json()["detail"] == "HIPAA export requires health.* ontology tags in scope"
    assert session.log_inserts == []


def test_sec_export_rejects_scope_without_finance_investment_tags(api_key_headers):
    """SEC export should reject executions without finance.investment.* tags."""
    session = _ComplianceSession(ontology_tag="finance.loan.apply", sensitivity_tier=2)

    response = _get_export(session, api_key_headers, "sec")

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "SEC export requires finance.investment.* ontology tags in scope"
    )
    assert session.log_inserts == []
