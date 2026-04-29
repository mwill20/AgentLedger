"""Tests for Layer 6 liability snapshot creation and read paths."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import perf_counter
from uuid import UUID, uuid4

from api.models.liability import (
    LiabilitySnapshotListResponse,
    LiabilitySnapshotRecord,
    LiabilitySnapshotSummary,
    SnapshotContextSummary,
    SnapshotStepTrustState,
)
from api.routers import liability as liability_router
from api.services import liability_snapshot

AGENT_DID = "did:key:z6MkLiabilitySnapshotAgent"
AUTHOR_DID = "did:key:z6MkWorkflowAuthor"
VALIDATOR_DID = "did:key:z6MkWorkflowValidator"


class _FakeMappings:
    """Minimal mappings wrapper for liability snapshot tests."""

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


class _InspectableSession:
    """Async DB double that records SQL and returns rows in order."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executed = []

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        return _FakeResult(self._rows.pop(0) if self._rows else [])


def _snapshot_row(
    *,
    snapshot_id: UUID,
    execution_id: UUID,
    workflow_id: UUID,
    service_id: UUID,
    captured_at: datetime,
) -> dict:
    """Build a stored liability snapshot row."""
    return {
        "id": snapshot_id,
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "agent_did": AGENT_DID,
        "captured_at": captured_at,
        "workflow_quality_score": 88.4,
        "workflow_author_did": AUTHOR_DID,
        "workflow_validator_did": VALIDATOR_DID,
        "workflow_validation_checklist": {"steps_achievable": True},
        "step_trust_states": [
            {
                "step_number": 1,
                "ontology_tag": "travel.air.book",
                "service_id": service_id,
                "service_name": "FlightBookerPro",
                "min_trust_tier": 3,
                "min_trust_score": 75.0,
                "trust_score": 91.2,
                "trust_tier": 4,
                "trust_score_source": "services.trust_score_at_snapshot",
            }
        ],
        "context_summary": {
            "fields_disclosed": ["user.email", "user.name"],
            "fields_withheld": ["user.ssn"],
            "fields_committed": ["user.passport_number"],
            "mismatch_count": 1,
        },
        "critical_mismatch_count": 1,
        "agent_profile_default_policy": "deny",
        "created_at": captured_at,
    }


def test_create_snapshot_populates_step_trust_states_and_context_summary():
    """Snapshot creation should capture point-in-time trust and Layer 4 context evidence."""
    workflow_id = uuid4()
    execution_id = uuid4()
    snapshot_id = uuid4()
    service_id = uuid4()
    captured_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    db = _InspectableSession(
        rows=[
            [],
            [
                {
                    "id": execution_id,
                    "workflow_id": workflow_id,
                    "agent_did": AGENT_DID,
                    "context_bundle_id": uuid4(),
                    "reported_at": captured_at,
                }
            ],
            [{"id": workflow_id, "quality_score": 88.4, "author_did": AUTHOR_DID}],
            [
                {
                    "validator_did": VALIDATOR_DID,
                    "checklist": {"steps_achievable": True},
                }
            ],
            [
                {
                    "step_number": 1,
                    "ontology_tag": "travel.air.book",
                    "min_trust_tier": 3,
                    "min_trust_score": 75.0,
                    "service_id": service_id,
                    "service_name": "FlightBookerPro",
                    "trust_score": 91.2,
                    "trust_tier": 4,
                }
            ],
            [
                {
                    "fields_disclosed": ["user.name", "user.email"],
                    "fields_withheld": ["user.ssn"],
                    "fields_committed": ["user.passport_number"],
                }
            ],
            [{"mismatch_count": 1, "critical_count": 1}],
            [{"default_policy": "deny"}],
            [
                _snapshot_row(
                    snapshot_id=snapshot_id,
                    execution_id=execution_id,
                    workflow_id=workflow_id,
                    service_id=service_id,
                    captured_at=captured_at,
                )
            ],
        ]
    )

    snapshot = asyncio.run(
        liability_snapshot.create_snapshot(
            db=db,
            execution_id=execution_id,
        )
    )

    assert snapshot.snapshot_id == snapshot_id
    assert snapshot.workflow_quality_score == 88.4
    assert snapshot.workflow_validator_did == VALIDATOR_DID
    assert snapshot.step_trust_states[0].service_id == service_id
    assert snapshot.step_trust_states[0].trust_score == 91.2
    assert snapshot.step_trust_states[0].trust_tier == 4
    assert snapshot.context_summary.fields_disclosed == ["user.email", "user.name"]
    assert snapshot.context_summary.fields_committed == ["user.passport_number"]
    assert snapshot.context_summary.mismatch_count == 1
    assert snapshot.critical_mismatch_count == 1
    insert_params = next(
        params for sql, params in db.executed if "INSERT INTO liability_snapshots" in sql
    )
    assert insert_params["workflow_quality_score"] == 88.4
    assert insert_params["workflow_validator_did"] == VALIDATOR_DID


def test_get_liability_snapshot_route_returns_snapshot(client, api_key_headers, monkeypatch):
    """GET /liability/snapshots/{execution_id} should expose one full snapshot."""
    workflow_id = uuid4()
    execution_id = uuid4()
    snapshot_id = uuid4()
    service_id = uuid4()
    captured_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)

    async def fake_get_snapshot_by_execution(*, db, execution_id):
        return LiabilitySnapshotRecord(
            snapshot_id=snapshot_id,
            execution_id=execution_id,
            workflow_id=workflow_id,
            agent_did=AGENT_DID,
            captured_at=captured_at,
            workflow_quality_score=88.4,
            workflow_author_did=AUTHOR_DID,
            workflow_validator_did=VALIDATOR_DID,
            workflow_validation_checklist={"steps_achievable": True},
            step_trust_states=[
                SnapshotStepTrustState(
                    step_number=1,
                    ontology_tag="travel.air.book",
                    service_id=service_id,
                    service_name="FlightBookerPro",
                    min_trust_tier=3,
                    min_trust_score=75.0,
                    trust_score=91.2,
                    trust_tier=4,
                    trust_score_source="services.trust_score_at_snapshot",
                )
            ],
            context_summary=SnapshotContextSummary(
                fields_disclosed=["user.name"],
                fields_withheld=[],
                fields_committed=["user.passport_number"],
                mismatch_count=0,
            ),
            critical_mismatch_count=0,
            agent_profile_default_policy="deny",
            created_at=captured_at,
        )

    monkeypatch.setattr(
        liability_router.liability_snapshot,
        "get_snapshot_by_execution",
        fake_get_snapshot_by_execution,
    )

    response = client.get(
        f"/v1/liability/snapshots/{execution_id}",
        headers=api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot_id"] == str(snapshot_id)
    assert body["execution_id"] == str(execution_id)
    assert body["step_trust_states"][0]["trust_score"] == 91.2
    assert body["context_summary"]["fields_committed"] == ["user.passport_number"]


def test_list_liability_snapshots_route_returns_admin_page(
    client,
    admin_api_key_headers,
    monkeypatch,
):
    """GET /liability/snapshots should expose filtered admin snapshot summaries."""
    workflow_id = uuid4()
    execution_id = uuid4()
    snapshot_id = uuid4()
    captured_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)

    async def fake_list_snapshots(**kwargs):
        assert kwargs["workflow_id"] == workflow_id
        assert kwargs["limit"] == 25
        return LiabilitySnapshotListResponse(
            total=1,
            limit=25,
            offset=0,
            snapshots=[
                LiabilitySnapshotSummary(
                    snapshot_id=snapshot_id,
                    execution_id=execution_id,
                    workflow_id=workflow_id,
                    agent_did=AGENT_DID,
                    workflow_quality_score=88.4,
                    critical_mismatch_count=1,
                    captured_at=captured_at,
                    created_at=captured_at,
                )
            ],
        )

    monkeypatch.setattr(
        liability_router.liability_snapshot,
        "list_snapshots",
        fake_list_snapshots,
    )

    response = client.get(
        f"/v1/liability/snapshots?workflow_id={workflow_id}&limit=25",
        headers=admin_api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["snapshots"][0]["snapshot_id"] == str(snapshot_id)
    assert body["snapshots"][0]["critical_mismatch_count"] == 1


def test_snapshot_route_handler_p95_under_200ms_at_100_concurrent_calls(
    api_key_headers,
    monkeypatch,
):
    """The snapshot GET handler should meet the Layer 6 p95 target."""
    workflow_id = uuid4()
    execution_id = uuid4()
    snapshot_id = uuid4()
    service_id = uuid4()
    captured_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)

    async def fake_get_snapshot_by_execution(*, db, execution_id):
        return LiabilitySnapshotRecord(
            snapshot_id=snapshot_id,
            execution_id=execution_id,
            workflow_id=workflow_id,
            agent_did=AGENT_DID,
            captured_at=captured_at,
            workflow_quality_score=88.4,
            workflow_author_did=AUTHOR_DID,
            workflow_validator_did=VALIDATOR_DID,
            workflow_validation_checklist={"steps_achievable": True},
            step_trust_states=[
                SnapshotStepTrustState(
                    step_number=1,
                    ontology_tag="travel.air.book",
                    service_id=service_id,
                    service_name="FlightBookerPro",
                    min_trust_tier=3,
                    min_trust_score=75.0,
                    trust_score=91.2,
                    trust_tier=4,
                    trust_score_source="services.trust_score_at_snapshot",
                )
            ],
            context_summary=SnapshotContextSummary(
                fields_disclosed=["user.name"],
                fields_withheld=[],
                fields_committed=["user.passport_number"],
                mismatch_count=0,
            ),
            critical_mismatch_count=0,
            agent_profile_default_policy="deny",
            created_at=captured_at,
        )

    monkeypatch.setattr(
        liability_router.liability_snapshot,
        "get_snapshot_by_execution",
        fake_get_snapshot_by_execution,
    )

    async def fetch_once():
        started = perf_counter()
        response = await liability_router.get_liability_snapshot(
            execution_id=execution_id,
            api_key=api_key_headers["X-API-Key"],
            db=object(),
        )
        assert response.snapshot_id == snapshot_id
        return perf_counter() - started

    async def run_load():
        return await asyncio.gather(*(fetch_once() for _ in range(100)))

    durations = asyncio.run(run_load())
    p95 = sorted(durations)[94]

    assert p95 < 0.2
