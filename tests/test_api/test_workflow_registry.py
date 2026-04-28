"""Tests for Layer 5 workflow registry Phase 1."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.workflow import (
    ExecutionReportRequest,
    ExecutionReportResponse,
    WorkflowCreateRequest,
    WorkflowCreateResponse,
    WorkflowListResponse,
    WorkflowRecord,
    WorkflowStepRecord,
    WorkflowSummary,
)
from api.routers import workflows as workflows_router
from api.services import workflow_registry


AUTHOR_DID = "did:key:z6MkWorkflowAuthor"


class _FakeMappings:
    """Minimal mappings wrapper for workflow service tests."""

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
        """Return the first column of the first row."""
        row = self._rows[0] if self._rows else None
        if row is None:
            raise Exception("no rows returned")
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


def _workflow_payload() -> dict:
    """Build a valid two-step TRAVEL workflow payload."""
    return {
        "spec_version": "1.0",
        "workflow_id": str(uuid4()),
        "name": "Business Travel Booking",
        "slug": "business-travel-booking",
        "description": "Book a business trip with flight and lodging steps.",
        "ontology_domain": "TRAVEL",
        "tags": ["travel.air.book", "travel.lodging.book"],
        "steps": [
            {
                "step_number": 1,
                "name": "Book flight",
                "ontology_tag": "travel.air.book",
                "service_id": None,
                "is_required": True,
                "fallback_step_number": None,
                "context_fields_required": ["user.name", "user.email"],
                "context_fields_optional": ["user.frequent_flyer_id"],
                "min_trust_tier": 3,
                "min_trust_score": 75.0,
                "timeout_seconds": 30,
            },
            {
                "step_number": 2,
                "name": "Book hotel",
                "ontology_tag": "travel.lodging.book",
                "service_id": None,
                "is_required": True,
                "fallback_step_number": None,
                "context_fields_required": ["user.name", "user.email"],
                "context_fields_optional": [],
                "min_trust_tier": 2,
                "min_trust_score": 60.0,
                "timeout_seconds": 30,
            },
        ],
        "context_bundle": {
            "all_required_fields": ["user.name", "user.email"],
            "all_optional_fields": ["user.frequent_flyer_id"],
            "single_approval": True,
        },
        "quality": {
            "quality_score": 0.0,
            "execution_count": 0,
            "success_rate": 0.0,
            "validation_status": "draft",
            "validated_by_domain": None,
        },
        "accountability": {
            "author_did": AUTHOR_DID,
            "published_at": None,
            "spec_hash": None,
        },
    }


def _workflow_row(workflow_id, slug="business-travel-booking") -> dict:
    """Build one stored workflow row."""
    now = datetime(2026, 4, 28, tzinfo=timezone.utc)
    spec = _workflow_payload()
    spec["workflow_id"] = str(workflow_id)
    return {
        "id": workflow_id,
        "name": "Business Travel Booking",
        "slug": slug,
        "description": "Book a business trip with flight and lodging steps.",
        "ontology_domain": "TRAVEL",
        "tags": ["travel.air.book", "travel.lodging.book"],
        "spec": spec,
        "spec_version": "1.0",
        "spec_hash": None,
        "author_did": AUTHOR_DID,
        "status": "draft",
        "quality_score": 0.0,
        "execution_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "parent_workflow_id": None,
        "published_at": None,
        "deprecated_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _step_rows(workflow_id) -> list[dict]:
    """Build stored workflow steps deliberately out of order."""
    now = datetime(2026, 4, 28, tzinfo=timezone.utc)
    return [
        {
            "id": uuid4(),
            "workflow_id": workflow_id,
            "step_number": 2,
            "name": "Book hotel",
            "ontology_tag": "travel.lodging.book",
            "service_id": None,
            "is_required": True,
            "fallback_step_number": None,
            "context_fields_required": ["user.name", "user.email"],
            "context_fields_optional": [],
            "min_trust_tier": 2,
            "min_trust_score": 60.0,
            "timeout_seconds": 30,
            "created_at": now,
        },
        {
            "id": uuid4(),
            "workflow_id": workflow_id,
            "step_number": 1,
            "name": "Book flight",
            "ontology_tag": "travel.air.book",
            "service_id": None,
            "is_required": True,
            "fallback_step_number": None,
            "context_fields_required": ["user.name", "user.email"],
            "context_fields_optional": ["user.frequent_flyer_id"],
            "min_trust_tier": 3,
            "min_trust_score": 75.0,
            "timeout_seconds": 30,
            "created_at": now,
        },
    ]


def _workflow_record(workflow_id) -> WorkflowRecord:
    """Build a workflow detail response for route tests."""
    row = _workflow_row(workflow_id)
    return WorkflowRecord(
        workflow_id=workflow_id,
        name=row["name"],
        slug=row["slug"],
        description=row["description"],
        ontology_domain=row["ontology_domain"],
        tags=row["tags"],
        spec=row["spec"],
        spec_version=row["spec_version"],
        spec_hash=None,
        author_did=row["author_did"],
        status=row["status"],
        quality_score=0.0,
        execution_count=0,
        success_count=0,
        failure_count=0,
        parent_workflow_id=None,
        published_at=None,
        deprecated_at=None,
        steps=[
            WorkflowStepRecord(
                step_id=step["id"],
                step_number=step["step_number"],
                name=step["name"],
                ontology_tag=step["ontology_tag"],
                service_id=None,
                is_required=step["is_required"],
                fallback_step_number=None,
                context_fields_required=step["context_fields_required"],
                context_fields_optional=step["context_fields_optional"],
                min_trust_tier=step["min_trust_tier"],
                min_trust_score=step["min_trust_score"],
                timeout_seconds=step["timeout_seconds"],
                created_at=step["created_at"],
            )
            for step in sorted(_step_rows(workflow_id), key=lambda item: item["step_number"])
        ],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def test_post_workflows_returns_created(client, api_key_headers, monkeypatch):
    """POST /v1/workflows should submit a workflow for review."""
    workflow_id = uuid4()
    validation_id = uuid4()

    async def fake_create_workflow(db, request):
        assert request.slug == "business-travel-booking"
        assert len(request.steps) == 2
        return WorkflowCreateResponse(
            workflow_id=workflow_id,
            slug=request.slug,
            status="draft",
            validation_id=validation_id,
            estimated_review_hours=48,
        )

    monkeypatch.setattr(
        workflows_router.workflow_registry,
        "create_workflow",
        fake_create_workflow,
    )

    response = client.post(
        "/v1/workflows",
        json=_workflow_payload(),
        headers=api_key_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["workflow_id"] == str(workflow_id)
    assert body["status"] == "draft"
    assert body["validation_id"] == str(validation_id)
    assert body["estimated_review_hours"] == 48


def test_create_workflow_inserts_workflow_steps_and_validation():
    """Registry create should validate ontology tags and write Phase 1 rows."""
    workflow_id = uuid4()
    validation_id = uuid4()
    payload = _workflow_payload()
    payload["workflow_id"] = str(workflow_id)
    db = _InspectableSession(
        rows=[
            [{"did": AUTHOR_DID}],
            [
                {"tag": "travel.air.book", "domain": "TRAVEL", "sensitivity_tier": 2},
                {
                    "tag": "travel.lodging.book",
                    "domain": "TRAVEL",
                    "sensitivity_tier": 2,
                },
            ],
            [],
            [],
            [{"id": validation_id}],
        ]
    )

    response = asyncio.run(
        workflow_registry.create_workflow(
            db=db,
            request=WorkflowCreateRequest(**payload),
        )
    )

    assert response.workflow_id == workflow_id
    assert response.status == "draft"
    assert response.validation_id == validation_id
    assert response.estimated_review_hours == 48
    assert db.commit_count == 1
    assert db.rollback_count == 0
    assert any("INSERT INTO workflows" in sql for sql, _ in db.executed)
    assert any("INSERT INTO workflow_steps" in sql for sql, _ in db.executed)
    assert any("INSERT INTO workflow_validations" in sql for sql, _ in db.executed)


def test_create_workflow_rejects_unknown_ontology_tag():
    """Spec validation rule 4 should reject unknown ontology tags."""
    db = _InspectableSession(rows=[[{"did": AUTHOR_DID}], []])

    try:
        asyncio.run(
            workflow_registry.create_workflow(
                db=db,
                request=WorkflowCreateRequest(**_workflow_payload()),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected unknown ontology tag rejection")

    assert response.status_code == 422
    assert "unknown ontology tags" in response.detail
    assert db.rollback_count == 1


def test_post_workflows_rejects_invalid_step_graph(client, api_key_headers):
    """Spec validation rules 3 and 5 should run before service logic."""
    payload = deepcopy(_workflow_payload())
    payload["steps"][0]["fallback_step_number"] = 1

    response = client.post(
        "/v1/workflows",
        json=payload,
        headers=api_key_headers,
    )

    assert response.status_code == 422


def test_get_workflow_by_slug_returns_steps_sorted():
    """Slug lookup should return full detail with steps sorted by step_number."""
    workflow_id = uuid4()
    db = _InspectableSession(rows=[[_workflow_row(workflow_id)], _step_rows(workflow_id)])

    response = asyncio.run(
        workflow_registry.get_workflow_by_slug(
            db=db,
            slug="business-travel-booking",
        )
    )

    assert response.workflow_id == workflow_id
    assert response.slug == "business-travel-booking"
    assert [step.step_number for step in response.steps] == [1, 2]
    assert response.steps[0].ontology_tag == "travel.air.book"


def test_get_workflows_slug_route_returns_full_detail(client, api_key_headers, monkeypatch):
    """GET /v1/workflows/slug/{slug} should return the workflow detail model."""
    workflow_id = uuid4()

    async def fake_get_by_slug(db, slug, redis=None):
        assert slug == "business-travel-booking"
        return _workflow_record(workflow_id)

    monkeypatch.setattr(
        workflows_router.workflow_registry,
        "get_workflow_by_slug",
        fake_get_by_slug,
    )

    response = client.get(
        "/v1/workflows/slug/business-travel-booking",
        headers=api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_id"] == str(workflow_id)
    assert [step["step_number"] for step in body["steps"]] == [1, 2]


def test_list_workflows_route_returns_paginated_summaries(
    client,
    api_key_headers,
    monkeypatch,
):
    """GET /v1/workflows should expose paginated workflow summaries."""
    workflow_id = uuid4()
    now = datetime(2026, 4, 28, tzinfo=timezone.utc)

    async def fake_list_workflows(
        db,
        *,
        domain=None,
        tags=None,
        status_filter="published",
        quality_min=None,
        limit=50,
        offset=0,
        redis=None,
    ):
        assert domain == "TRAVEL"
        assert tags == ["travel.air.book", "travel.lodging.book"]
        assert status_filter == "published"
        return WorkflowListResponse(
            total=1,
            limit=limit,
            offset=offset,
            workflows=[
                WorkflowSummary(
                    workflow_id=workflow_id,
                    name="Business Travel Booking",
                    slug="business-travel-booking",
                    description="Book a business trip.",
                    ontology_domain="TRAVEL",
                    tags=["travel.air.book", "travel.lodging.book"],
                    status="published",
                    quality_score=82.5,
                    execution_count=100,
                    step_count=2,
                    published_at=now,
                    created_at=now,
                    updated_at=now,
                )
            ],
        )

    monkeypatch.setattr(
        workflows_router.workflow_registry,
        "list_workflows",
        fake_list_workflows,
    )

    response = client.get(
        "/v1/workflows?domain=TRAVEL&tags=travel.air.book,travel.lodging.book",
        headers=api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["workflows"][0]["step_count"] == 2


def _execution_payload(outcome: str = "success") -> dict:
    """Build a valid execution report payload."""
    return {
        "agent_did": AUTHOR_DID,
        "context_bundle_id": None,
        "outcome": outcome,
        "steps_completed": 2,
        "steps_total": 2,
        "failure_step_number": None,
        "failure_reason": None,
        "duration_ms": 4200,
    }


def test_report_execution_returns_201_unverified(client, api_key_headers, monkeypatch):
    """POST /v1/workflows/{id}/executions should return 201 with verified=false."""
    workflow_id = uuid4()
    execution_id = uuid4()

    async def fake_report_execution(
        *,
        workflow_id,
        request,
        db,
        redis=None,
        background_tasks=None,
        verify_sync=None,
    ):
        assert request.outcome == "success"
        assert request.agent_did == AUTHOR_DID
        return ExecutionReportResponse(
            execution_id=execution_id,
            verified=False,
            quality_score=35.0,
        )

    monkeypatch.setattr(
        workflows_router.workflow_executor,
        "report_execution_from_request",
        fake_report_execution,
    )

    response = client.post(
        f"/v1/workflows/{workflow_id}/executions",
        json=_execution_payload("success"),
        headers=api_key_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["execution_id"] == str(execution_id)
    assert body["verified"] is False
    assert body["quality_score"] == 35.0


def test_report_execution_rejects_unpublished_workflow():
    """Execution reporting must reject missing or non-published workflows."""
    workflow_id = uuid4()
    workflow_row = _workflow_row(workflow_id)

    db = _InspectableSession(
        rows=[
            [workflow_row],
        ]
    )

    try:
        asyncio.run(
            workflow_registry.report_execution(
                db=db,
                workflow_id=workflow_id,
                request=ExecutionReportRequest(**_execution_payload()),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected 409 for non-published workflow")

    assert response.status_code == 404
    assert response.detail == "workflow not found or not published"


def test_report_execution_inserts_row_and_increments_counters():
    """Service should write execution row and update success_count for success."""
    workflow_id = uuid4()
    execution_id = uuid4()
    published_row = dict(_workflow_row(workflow_id))
    published_row["status"] = "published"

    db = _InspectableSession(
        rows=[
            [{"id": workflow_id, "status": "published"}],
            [{"did": AUTHOR_DID}],
            # no DB call for _verify_context_bundle (bundle_id is None)
            [{"id": execution_id}],        # INSERT RETURNING id
            [],                            # UPDATE workflows counters
            [{"status": "published", "execution_count": 1, "success_count": 1}],
            [{"verified_count": 0}],
            [],                            # _avg_step_trust (no pinned services)
            [],                            # UPDATE workflows quality_score
        ]
    )

    response = asyncio.run(
        workflow_registry.report_execution(
            db=db,
            workflow_id=workflow_id,
            request=ExecutionReportRequest(**_execution_payload("success")),
        )
    )

    assert response.execution_id == execution_id
    assert response.verified is False
    assert db.commit_count == 2
    assert any("INSERT INTO workflow_executions" in sql for sql, _ in db.executed)
    assert any("CASE WHEN :outcome = 'success'" in sql for sql, _ in db.executed)


def test_unverified_executions_cap_quality_score():
    """compute_workflow_quality_score should cap at 70.0 when verification_rate < 0.5."""
    from api.services.workflow_ranker import compute_workflow_quality_score

    workflow_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"status": "published", "execution_count": 10, "success_count": 9}],
            [{"verified_count": 0}],
            [],
        ]
    )

    score = asyncio.run(
        compute_workflow_quality_score(workflow_id=workflow_id, db=db)
    )

    assert score <= 70.0
