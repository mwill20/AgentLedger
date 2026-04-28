"""Tests for Layer 5 workflow context bundle integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

from api.models.workflow import (
    BundleApproveRequest,
    BundleApproveResponse,
    BundleFieldBreakdown,
    BundleResponse,
)
from api.routers import workflows as workflows_router
from api.services import workflow_context, workflow_ranker
from tests.test_api.test_workflow_ranker import _FilteringRankSession

AGENT_DID = "did:key:z6MkWorkflowContextAgent"


class _FakeMappings:
    """Minimal mappings wrapper for workflow context tests."""

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
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        return _FakeResult(self._rows.pop(0) if self._rows else [])

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _profile(*, permit_frequent_flyer: bool = False):
    """Build a profile-like object for direct evaluate_profile calls."""
    permitted_fields = ["user.name", "user.email", "user.ssn"]
    denied_fields = []
    if permit_frequent_flyer:
        permitted_fields.append("user.frequent_flyer_id")
    else:
        denied_fields.append("user.frequent_flyer_id")
    return SimpleNamespace(
        profile_id=uuid4(),
        default_policy="deny",
        rules=[
            SimpleNamespace(
                priority=10,
                scope_type="domain",
                scope_value="TRAVEL",
                permitted_fields=permitted_fields,
                denied_fields=denied_fields,
                action="permit",
            )
        ],
    )


def _workflow_step_rows(workflow_id):
    """Build published workflow rows joined to two ordered steps."""
    return [
        {
            "workflow_id": workflow_id,
            "workflow_ontology_domain": "TRAVEL",
            "step_id": uuid4(),
            "step_number": 1,
            "ontology_tag": "travel.air.book",
            "service_id": None,
            "is_required": True,
            "context_fields_required": ["user.name", "user.frequent_flyer_id"],
            "context_fields_optional": ["user.ssn"],
            "min_trust_tier": 3,
            "min_trust_score": 75.0,
        },
        {
            "workflow_id": workflow_id,
            "workflow_ontology_domain": "TRAVEL",
            "step_id": uuid4(),
            "step_number": 2,
            "ontology_tag": "travel.lodging.book",
            "service_id": None,
            "is_required": True,
            "context_fields_required": ["user.name", "user.email"],
            "context_fields_optional": [],
            "min_trust_tier": 2,
            "min_trust_score": 60.0,
        },
    ]


def test_create_context_bundle_returns_by_step_field_breakdown(monkeypatch):
    """Bundle creation should classify fields per step with Layer 4 evaluator."""
    workflow_id = uuid4()
    bundle_id = uuid4()
    expires_at = datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc)
    db = _InspectableSession(
        rows=[
            _workflow_step_rows(workflow_id),
            [{"id": bundle_id, "expires_at": expires_at}],
        ]
    )

    async def fake_profile(db, agent_did, redis=None):
        return _profile()

    monkeypatch.setattr(
        workflow_context.context_profiles,
        "get_active_profile",
        fake_profile,
    )

    response = asyncio.run(
        workflow_context.create_context_bundle(
            workflow_id=workflow_id,
            agent_did=AGENT_DID,
            scoped_profile_overrides={},
            db=db,
            redis=None,
        )
    )

    assert response.bundle_id == bundle_id
    assert response.status == "pending"
    assert response.by_step["step_1"].permitted == ["user.name"]
    assert response.by_step["step_1"].withheld == ["user.frequent_flyer_id"]
    assert response.by_step["step_1"].committed == ["user.ssn"]
    assert response.by_step["step_2"].permitted == ["user.name", "user.email"]
    assert response.all_permitted == ["user.name", "user.email"]
    assert response.all_committed == ["user.ssn"]
    assert response.all_withheld == ["user.frequent_flyer_id"]
    assert db.commit_count == 1
    insert_params = next(
        params
        for sql, params in db.executed
        if "INSERT INTO workflow_context_bundles" in sql
    )
    assert '"step_1"' in insert_params["approved_fields"]


def test_scoped_profile_override_changes_field_classification(monkeypatch):
    """A scoped override should permit a field denied by the base profile."""
    workflow_id = uuid4()
    scoped_profile_id = uuid4()
    bundle_id = uuid4()
    expires_at = datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc)
    db = _InspectableSession(
        rows=[
            _workflow_step_rows(workflow_id),
            [{"id": scoped_profile_id}],
            [{"id": bundle_id, "expires_at": expires_at}],
        ]
    )

    async def fake_profile(db, agent_did, redis=None):
        return _profile()

    monkeypatch.setattr(
        workflow_context.context_profiles,
        "get_active_profile",
        fake_profile,
    )

    response = asyncio.run(
        workflow_context.create_context_bundle(
            workflow_id=workflow_id,
            agent_did=AGENT_DID,
            scoped_profile_overrides={"user.frequent_flyer_id": "permit"},
            db=db,
            redis=None,
        )
    )

    assert response.by_step["step_1"].permitted == [
        "user.name",
        "user.frequent_flyer_id",
    ]
    assert response.by_step["step_1"].withheld == []
    scoped_insert = next(
        params
        for sql, params in db.executed
        if "INSERT INTO workflow_scoped_profiles" in sql
    )
    assert scoped_insert["base_profile_id"] is not None
    assert "user.frequent_flyer_id" in scoped_insert["overrides"]


def test_approve_context_bundle_transitions_to_approved():
    """Approving a pending bundle should set status approved."""
    bundle_id = uuid4()
    approved_at = datetime(2026, 4, 28, 12, 1, tzinfo=timezone.utc)
    db = _InspectableSession(
        rows=[
            [
                {
                    "id": bundle_id,
                    "agent_did": AGENT_DID,
                    "status": "pending",
                    "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
                }
            ],
            [{"user_approved_at": approved_at}],
        ]
    )

    response = asyncio.run(
        workflow_context.approve_context_bundle(
            bundle_id=bundle_id,
            request=BundleApproveRequest(agent_did=AGENT_DID),
            db=db,
        )
    )

    assert response.bundle_id == bundle_id
    assert response.status == "approved"
    assert response.approved_at == approved_at
    assert db.commit_count == 1
    assert any("SET status = 'approved'" in sql for sql, _ in db.executed)


def test_approve_context_bundle_returns_410_when_expired():
    """Expired bundles should return 410 Gone, not 404."""
    bundle_id = uuid4()
    db = _InspectableSession(
        rows=[
            [
                {
                    "id": bundle_id,
                    "agent_did": AGENT_DID,
                    "status": "pending",
                    "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
                }
            ]
        ]
    )

    try:
        asyncio.run(
            workflow_context.approve_context_bundle(
                bundle_id=bundle_id,
                request=BundleApproveRequest(agent_did=AGENT_DID),
                db=db,
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected expired bundle")

    assert response.status_code == 410
    assert response.detail == "bundle expired"
    assert db.rollback_count == 1


def test_rank_with_agent_did_marks_candidate_can_disclose_false(monkeypatch):
    """Rank should flag candidates when a required field is withheld."""
    workflow_id = uuid4()
    service_id = uuid4()
    db = _FilteringRankSession(
        workflow_id=workflow_id,
        steps=[
            {
                "step_number": 1,
                "ontology_tag": "travel.air.book",
                "is_required": True,
                "context_fields_required": [
                    "user.name",
                    "user.frequent_flyer_id",
                ],
                "context_fields_optional": [],
                "min_trust_tier": 3,
                "min_trust_score": 75.0,
            }
        ],
        candidates_by_tag={
            "travel.air.book": [
                {
                    "service_id": service_id,
                    "name": "FlightBookerPro",
                    "domain": "flightbooker.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 91.2,
                    "trust_tier": 3,
                    "pricing_model": "usage",
                }
            ]
        },
    )

    async def fake_profile(db, agent_did, redis=None):
        return _profile()

    monkeypatch.setattr(
        workflow_ranker.context_profiles,
        "get_active_profile",
        fake_profile,
    )

    response = asyncio.run(
        workflow_ranker.get_workflow_rank(
            workflow_id=workflow_id,
            geo=None,
            pricing_model=None,
            agent_did=AGENT_DID,
            db=db,
            redis=None,
        )
    )

    assert response.ranked_steps[0].candidates[0].can_disclose is False


def test_context_bundle_routes_return_expected_models(
    client,
    api_key_headers,
    monkeypatch,
):
    """Workflow context bundle endpoints should expose service responses."""
    workflow_id = uuid4()
    bundle_id = uuid4()
    expires_at = datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc)
    approved_at = datetime(2026, 4, 28, 12, 1, tzinfo=timezone.utc)

    async def fake_create(request, db, redis=None):
        assert request.workflow_id == workflow_id
        assert request.agent_did == AGENT_DID
        return BundleResponse(
            bundle_id=bundle_id,
            workflow_id=workflow_id,
            status="pending",
            by_step={
                "step_1": BundleFieldBreakdown(
                    permitted=["user.name"],
                    withheld=[],
                    committed=[],
                )
            },
            all_permitted=["user.name"],
            all_committed=[],
            all_withheld=[],
            expires_at=expires_at,
        )

    async def fake_approve(bundle_id, request, db):
        assert request.agent_did == AGENT_DID
        return BundleApproveResponse(
            bundle_id=bundle_id,
            status="approved",
            approved_at=approved_at,
        )

    monkeypatch.setattr(
        workflows_router.workflow_context,
        "create_context_bundle_from_request",
        fake_create,
    )
    monkeypatch.setattr(
        workflows_router.workflow_context,
        "approve_context_bundle",
        fake_approve,
    )

    create_response = client.post(
        "/v1/workflows/context/bundle",
        json={
            "workflow_id": str(workflow_id),
            "agent_did": AGENT_DID,
            "scoped_profile_overrides": {},
        },
        headers=api_key_headers,
    )
    approve_response = client.post(
        f"/v1/workflows/context/bundle/{bundle_id}/approve",
        json={"agent_did": AGENT_DID},
        headers=api_key_headers,
    )

    assert create_response.status_code == 201
    assert create_response.json()["by_step"]["step_1"]["permitted"] == ["user.name"]
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "approved"
