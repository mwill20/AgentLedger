"""Tests for Layer 3 routers."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from api.models.layer3 import (
    AttestationCreateResponse,
    AuditorRecord,
    AuditorRegistrationResponse,
    ChainStatusResponse,
    FederationBlocklistResponse,
)
from api.routers import attestation as attestation_router
from api.routers import audit as audit_router
from api.routers import chain as chain_router
from api.routers import federation as federation_router
from api.services import audit as audit_service
from api.services import chain as chain_service
from api.services import federation as federation_service


class _FakeMappings:
    """Minimal mappings wrapper for Layer 3 service unit tests."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    """Minimal SQLAlchemy result wrapper for Layer 3 service unit tests."""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _InspectableSession:
    """Async session double that records executed SQL text."""

    def __init__(self, rows: list[list[dict]]) -> None:
        self._rows = list(rows)
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        rows = self._rows.pop(0) if self._rows else []
        return _FakeResult(rows)


def test_get_chain_status_is_public(client, monkeypatch):
    """GET /v1/chain/status should be reachable without an API key."""

    async def fake_get_chain_status_for_tx(db, tx_hash=None):
        return ChainStatusResponse(
            chain_id=137,
            network="polygon-pos-local",
            latest_block=12,
            contracts={"attestation_ledger": "", "audit_chain": ""},
        )

    monkeypatch.setattr(
        chain_router.chain,
        "get_chain_status_for_tx",
        fake_get_chain_status_for_tx,
    )

    response = client.get("/v1/chain/status")

    assert response.status_code == 200
    assert response.json()["chain_id"] == 137


def test_post_auditors_register_returns_created(client, api_key_headers, monkeypatch):
    """POST /v1/auditors/register should create an active auditor."""

    async def fake_register_auditor(db, request):
        return AuditorRegistrationResponse(
            application_id=uuid4(),
            status="active",
        )

    monkeypatch.setattr(attestation_router.auditor, "register_auditor", fake_register_auditor)

    response = client.post(
        "/v1/auditors/register",
        json={
            "did": "did:web:auditfirm.example",
            "name": "Audit Firm",
            "ontology_scope": ["travel.*"],
            "accreditation_refs": [{"type": "SOC2"}],
            "chain_address": "0x1234567890abcdef1234567890abcdef12345678",
        },
        headers=api_key_headers,
    )

    assert response.status_code == 201
    assert response.json()["status"] == "active"


def test_get_auditors_returns_records(client, api_key_headers, monkeypatch):
    """GET /v1/auditors should list active auditors."""

    async def fake_list_auditors(db):
        return [
            AuditorRecord(
                did="did:web:auditfirm.example",
                name="Audit Firm",
                ontology_scope=["travel.*"],
                accreditation_refs=[{"type": "SOC2"}],
                chain_address="0x1234567890abcdef1234567890abcdef12345678",
                is_active=True,
                approved_at=datetime(2026, 4, 14, tzinfo=timezone.utc),
                credential_expires_at=datetime(2027, 4, 14, tzinfo=timezone.utc),
            )
        ]

    monkeypatch.setattr(attestation_router.auditor, "list_auditors", fake_list_auditors)

    response = client.get("/v1/auditors", headers=api_key_headers)

    assert response.status_code == 200
    assert response.json()[0]["did"] == "did:web:auditfirm.example"


def test_post_attestations_returns_tx_metadata(client, api_key_headers, monkeypatch):
    """POST /v1/attestations should return the synthetic tx metadata."""

    async def fake_submit_attestation(db, request):
        return AttestationCreateResponse(
            attestation_id=uuid4(),
            tx_hash="0xabc123",
            block_number=7,
        )

    monkeypatch.setattr(
        attestation_router.attestation,
        "submit_attestation",
        fake_submit_attestation,
    )

    response = client.post(
        "/v1/attestations",
        json={
            "auditor_did": "did:web:auditfirm.example",
            "service_domain": "skybridge.example",
            "ontology_scope": "travel.*",
            "certification_ref": "SOC2-2026",
            "expires_at": "2027-04-14T00:00:00Z",
            "evidence_package": {"report": "SOC2"},
        },
        headers=api_key_headers,
    )

    assert response.status_code == 201
    assert response.json()["tx_hash"] == "0xabc123"


def test_post_attestations_revoke_returns_tx_metadata(client, api_key_headers, monkeypatch):
    """POST /v1/attestations/revoke should return revocation tx metadata."""

    async def fake_submit_revocation(db, request):
        return {
            "revocation_id": str(uuid4()),
            "tx_hash": "0xdef456",
            "block_number": 9,
        }

    monkeypatch.setattr(
        attestation_router.attestation,
        "submit_revocation",
        fake_submit_revocation,
    )

    response = client.post(
        "/v1/attestations/revoke",
        json={
            "auditor_did": "did:web:auditfirm.example",
            "service_domain": "skybridge.example",
            "reason_code": "security_incident",
            "evidence_package": {"report": "IR-123"},
        },
        headers=api_key_headers,
    )

    assert response.status_code == 201
    assert response.json()["tx_hash"] == "0xdef456"


def test_post_audit_records_returns_created(client, api_key_headers, monkeypatch):
    """POST /v1/audit/records should return a pending anchor record."""

    async def fake_create_audit_record(db, request):
        return {
            "record_id": str(uuid4()),
            "record_hash": "0xdeadbeef",
            "status": "pending_anchor",
        }

    monkeypatch.setattr(audit_router.audit, "create_audit_record", fake_create_audit_record)

    response = client.post(
        "/v1/audit/records",
        json={
            "session_assertion_id": str(uuid4()),
            "ontology_tag": "travel.air.book",
            "action_context": {"inputs": ["traveler_name"]},
            "outcome": "success",
            "outcome_details": {"latency_ms": 120},
        },
        headers=api_key_headers,
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending_anchor"


def test_get_federation_blocklist_is_public(client, monkeypatch):
    """GET /v1/federation/blocklist should be public."""

    async def fake_get_blocklist(db, page=1, limit=50, since=None):
        return FederationBlocklistResponse(
            revocations=[],
            total=0,
            next_page=None,
        )

    monkeypatch.setattr(federation_router.federation, "get_blocklist", fake_get_blocklist)

    response = client.get("/v1/federation/blocklist")

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_get_well_known_blocklist_is_public(client, monkeypatch):
    """GET /.well-known/agentledger-blocklist.json should be public."""

    async def fake_get_blocklist(db, page=1, limit=1000, since=None):
        return FederationBlocklistResponse(
            revocations=[],
            total=0,
            next_page=None,
        )

    monkeypatch.setattr(federation_router.federation, "get_blocklist", fake_get_blocklist)

    response = client.get("/v1/.well-known/agentledger-blocklist.json")

    assert response.status_code == 200
    assert response.json()["revocations"] == []


def test_get_blocklist_omits_since_clause_when_not_provided():
    """Service blocklist queries should not emit NULL-guarded date filters."""

    db = _InspectableSession(rows=[[]])

    response = asyncio.run(
        federation_service.get_blocklist(
            db=db,
            page=1,
            limit=50,
            since=None,
        )
    )

    sql_text, params = db.executed[0]
    assert response.total == 0
    assert ":since IS NULL OR" not in sql_text
    assert ">= :since" not in sql_text
    assert "since" not in params


def test_get_blocklist_includes_since_clause_when_provided():
    """Service blocklist queries should include the date filter only when requested."""

    db = _InspectableSession(rows=[[]])
    since = datetime(2026, 4, 14, tzinfo=timezone.utc)

    asyncio.run(
        federation_service.get_blocklist(
            db=db,
            page=1,
            limit=50,
            since=since,
        )
    )

    sql_text, params = db.executed[0]
    assert "COALESCE(ce.confirmed_at, ce.indexed_at) >= :since" in sql_text
    assert params["since"] == since


def test_list_audit_records_omits_null_guard_filters():
    """Audit list queries should not use asyncpg-ambiguous NULL guard clauses."""

    db = _InspectableSession(rows=[[]])

    response = asyncio.run(audit_service.list_audit_records(db=db))

    sql_text, params = db.executed[0]
    assert response.total == 0
    assert " IS NULL OR " not in sql_text
    assert params == {"limit": 50, "offset": 0}


def test_list_chain_events_omits_null_guard_filters():
    """Chain list queries should not use asyncpg-ambiguous NULL guard clauses."""

    db = _InspectableSession(rows=[[]])

    response = asyncio.run(chain_service.list_chain_events(db=db))

    sql_text, params = db.executed[0]
    assert response.total == 0
    assert " IS NULL OR " not in sql_text
    assert params == {"limit": 50}
