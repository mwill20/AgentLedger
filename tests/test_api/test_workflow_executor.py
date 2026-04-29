"""Tests for Layer 5 workflow execution outcome feedback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from api.models.workflow import ExecutionReportResponse
from api.routers import workflows as workflows_router
from api.services import workflow_executor
from api.services.workflow_ranker import compute_workflow_quality_score

AGENT_DID = "did:key:z6MkWorkflowExecutorAgent"


class _FakeMappings:
    """Minimal mappings wrapper for workflow executor tests."""

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

    def scalar_one(self):
        row = self._rows[0] if self._rows else None
        if isinstance(row, dict):
            return next(iter(row.values()))
        return row


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


class _FakeRedis:
    """Small Redis double for rank cache invalidation tests."""

    def __init__(self, store):
        self.store = dict(store)


def _execution_payload(outcome: str = "success") -> dict:
    """Build a valid execution report payload."""
    return {
        "agent_did": AGENT_DID,
        "context_bundle_id": None,
        "outcome": outcome,
        "steps_completed": 2,
        "steps_total": 2,
        "failure_step_number": None,
        "failure_reason": None,
        "duration_ms": 4200,
    }


def test_report_execution_route_returns_201_unverified(
    client,
    api_key_headers,
    monkeypatch,
):
    """POST /workflows/{id}/executions should expose executor responses."""
    workflow_id = uuid4()
    execution_id = uuid4()

    async def fake_report(
        *,
        workflow_id,
        request,
        db,
        redis=None,
        background_tasks=None,
        verify_sync=None,
    ):
        assert request.agent_did == AGENT_DID
        assert request.outcome == "success"
        return ExecutionReportResponse(
            execution_id=execution_id,
            verified=False,
            quality_score=42.8,
        )

    monkeypatch.setattr(
        workflows_router.workflow_executor,
        "report_execution_from_request",
        fake_report,
    )

    response = client.post(
        f"/v1/workflows/{workflow_id}/executions",
        json=_execution_payload(),
        headers=api_key_headers,
    )

    assert response.status_code == 201
    assert response.json() == {
        "execution_id": str(execution_id),
        "verified": False,
        "quality_score": 42.8,
    }


def test_report_execution_outcome_inserts_and_uses_atomic_counter_update(monkeypatch):
    """Reporting an outcome should insert false-verified row and update counters."""
    workflow_id = uuid4()
    execution_id = uuid4()
    redis = _FakeRedis(
        {
            f"workflow:rank:{workflow_id}:any:any:anonymous": "cached",
            f"workflow:rank:{uuid4()}:any:any:anonymous": "other",
        }
    )
    db = _InspectableSession(
        rows=[
            [{"id": workflow_id, "status": "published"}],
            [{"did": AGENT_DID}],
            [{"id": execution_id}],
            [],
            [{"status": "published", "execution_count": 1, "success_count": 1}],
            [{"verified_count": 0}],
            [],
            [],
        ]
    )
    snapshot_calls = []

    async def fake_create_snapshot(*, db, execution_id):
        snapshot_calls.append((db, execution_id, db.commit_count))

    monkeypatch.setattr(
        "api.services.liability_snapshot.create_snapshot",
        fake_create_snapshot,
    )

    response = asyncio.run(
        workflow_executor.report_execution_outcome(
            workflow_id=workflow_id,
            agent_did=AGENT_DID,
            context_bundle_id=None,
            outcome="success",
            steps_completed=2,
            steps_total=2,
            failure_step_number=None,
            failure_reason=None,
            duration_ms=4200,
            db=db,
            redis=redis,
            verify_sync=False,
        )
    )

    assert response.execution_id == execution_id
    assert response.verified is False
    insert_params = next(
        params for sql, params in db.executed if "INSERT INTO workflow_executions" in sql
    )
    assert insert_params["outcome"] == "success"
    counter_sql, counter_params = next(
        (sql, params) for sql, params in db.executed if "success_count = success_count" in sql
    )
    assert "execution_count = execution_count + 1" in counter_sql
    assert "CASE WHEN :outcome = 'success'" in counter_sql
    assert "CASE WHEN :outcome = 'failure'" in counter_sql
    assert counter_params["outcome"] == "success"
    assert snapshot_calls == [(db, execution_id, 0)]
    assert db.commit_count == 2
    assert f"workflow:rank:{workflow_id}:any:any:anonymous" not in redis.store


def test_partial_outcome_increments_neither_success_nor_failure_directly(monkeypatch):
    """Partial outcomes should rely on CASE SQL and only increment execution_count."""
    workflow_id = uuid4()
    execution_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"id": workflow_id, "status": "published"}],
            [{"did": AGENT_DID}],
            [{"id": execution_id}],
            [],
            [{"status": "published", "execution_count": 1, "success_count": 0}],
            [{"verified_count": 0}],
            [],
            [],
        ]
    )
    monkeypatch.setattr(
        "api.services.liability_snapshot.create_snapshot",
        lambda **kwargs: _async_noop(),
    )

    asyncio.run(
        workflow_executor.report_execution_outcome(
            workflow_id=workflow_id,
            agent_did=AGENT_DID,
            context_bundle_id=None,
            outcome="partial",
            steps_completed=1,
            steps_total=2,
            failure_step_number=2,
            failure_reason="Hotel booking degraded.",
            duration_ms=4200,
            db=db,
            redis=None,
            verify_sync=False,
        )
    )

    counter_params = next(
        params for sql, params in db.executed if "CASE WHEN :outcome" in sql
    )
    assert counter_params["outcome"] == "partial"


async def _async_noop():
    """Async no-op used by monkeypatched collaborators."""


def test_snapshot_failure_rolls_back_execution_report(monkeypatch):
    """Snapshot creation failure should fail closed before committing execution."""
    workflow_id = uuid4()
    execution_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"id": workflow_id, "status": "published"}],
            [{"did": AGENT_DID}],
            [{"id": execution_id}],
            [],
        ]
    )

    async def fail_create_snapshot(*, db, execution_id):
        raise SQLAlchemyError("snapshot insert failed")

    monkeypatch.setattr(
        "api.services.liability_snapshot.create_snapshot",
        fail_create_snapshot,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            workflow_executor.report_execution_outcome(
                workflow_id=workflow_id,
                agent_did=AGENT_DID,
                context_bundle_id=None,
                outcome="success",
                steps_completed=2,
                steps_total=2,
                failure_step_number=None,
                failure_reason=None,
                duration_ms=4200,
                db=db,
                redis=None,
                verify_sync=False,
            )
        )

    assert exc_info.value.status_code == 500
    assert "failed to report execution" in exc_info.value.detail
    assert db.commit_count == 0
    assert db.rollback_count == 1


def test_verify_execution_sets_verified_true_when_disclosures_cover_required_steps():
    """Verification should pass when every required step has disclosure evidence."""
    workflow_id = uuid4()
    execution_id = uuid4()
    bundle_id = uuid4()
    reported_at = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    db = _InspectableSession(
        rows=[
            [
                {
                    "id": execution_id,
                    "workflow_id": workflow_id,
                    "agent_did": AGENT_DID,
                    "context_bundle_id": bundle_id,
                    "reported_at": reported_at,
                }
            ],
            [
                {"ontology_tag": "travel.air.book"},
                {"ontology_tag": "travel.lodging.book"},
            ],
            [
                {"ontology_tag": "travel.air.book"},
                {"ontology_tag": "travel.lodging.book"},
            ],
            [],
            [{"status": "published", "execution_count": 1, "success_count": 1}],
            [{"verified_count": 1}],
            [],
            [],
        ]
    )

    result = asyncio.run(
        workflow_executor.verify_execution(
            execution_id=execution_id,
            db=db,
            redis=None,
        )
    )

    assert result.verified is True
    update_params = next(
        params
        for sql, params in db.executed
        if "UPDATE workflow_executions" in sql
    )
    assert update_params["verified"] is True
    assert result.quality_score == 62.8


def test_quality_score_cap_holds_for_unverifiable_success_reports():
    """Unverified success reports should not push quality score above 70.0."""
    workflow_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"status": "published", "execution_count": 100, "success_count": 100}],
            [{"verified_count": 0}],
            [{"trust_score": 100.0}],
        ]
    )

    score = asyncio.run(
        compute_workflow_quality_score(
            workflow_id=workflow_id,
            db=db,
            redis=None,
        )
    )

    assert score == 70.0
