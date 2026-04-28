"""Tests for Layer 4 compliance PDF exports."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from api.dependencies import get_db
from api.main import app
from api.services import context_compliance


AGENT_DID = "did:key:z6MkContextHealthAgent"


class _FakeMappings:
    """Minimal mappings wrapper for compliance tests."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    """Minimal SQLAlchemy result wrapper for row mappings."""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _InspectableSession:
    """Async DB double that returns rows in query order."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        return _FakeResult(self._rows.pop(0) if self._rows else [])


def _export_rows():
    """Build the four source datasets used by a compliance export."""
    profile_id = uuid4()
    service_id = uuid4()
    disclosure_id = uuid4()
    erased_disclosure_id = uuid4()
    now = datetime(2026, 4, 27, 18, 30, tzinfo=timezone.utc)
    return [
        [
            {
                "id": profile_id,
                "profile_name": "default",
                "default_policy": "deny",
            }
        ],
        [
            {
                "priority": 10,
                "scope_type": "domain",
                "scope_value": "HEALTH",
                "permitted_fields": ["user.name", "user.insurance_id"],
                "denied_fields": ["user.ssn"],
                "action": "permit",
            }
        ],
        [
            {
                "id": disclosure_id,
                "service_id": service_id,
                "ontology_tag": "health.pharmacy.order",
                "fields_disclosed": ["user.name"],
                "fields_committed": ["user.insurance_id"],
                "fields_withheld": ["user.ssn"],
                "disclosure_method": "direct+committed",
                "erased": False,
                "erased_at": None,
                "created_at": now,
            },
            {
                "id": erased_disclosure_id,
                "service_id": service_id,
                "ontology_tag": "health.pharmacy.order",
                "fields_disclosed": [],
                "fields_committed": [],
                "fields_withheld": [],
                "disclosure_method": "direct",
                "erased": True,
                "erased_at": now,
                "created_at": now,
            },
        ],
        [
            {
                "service_id": service_id,
                "over_requested_fields": ["user.ssn"],
                "severity": "critical",
                "resolved": True,
                "created_at": now,
            }
        ],
    ]


def test_generate_compliance_pdf_returns_valid_pdf_bytes():
    """The real PDF generator should produce an in-memory PDF document."""
    db = _InspectableSession(_export_rows())

    import asyncio

    pdf_bytes = asyncio.run(
        context_compliance.generate_compliance_pdf(
            db=db,
            agent_did=AGENT_DID,
        )
    )

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(db.executed) == 4


def test_get_context_compliance_export_returns_pdf(api_key_headers):
    """GET /v1/context/compliance/export/{agent_did} should return a PDF."""

    async def override_get_db():
        yield _InspectableSession(_export_rows())

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/v1/context/compliance/export/{AGENT_DID}",
                headers=api_key_headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="agentledger_compliance_'
    )
    assert response.content.startswith(b"%PDF-")


def test_get_context_compliance_export_returns_404_for_unknown_agent(api_key_headers):
    """Unknown agents with no profile, disclosures, or mismatches return 404."""

    async def override_get_db():
        yield _InspectableSession([[], [], []])

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.get(
                "/v1/context/compliance/export/did:key:z6MkNoRecords",
                headers=api_key_headers,
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "no compliance records found for agent_did"
