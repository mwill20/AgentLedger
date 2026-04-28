"""Tests for Layer 5 human validation queue."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.workflow import (
    ValidationAssignRequest,
    ValidationResponse,
    ValidatorDecisionRequest,
    WorkflowRecord,
)
from api.routers import workflows as workflows_router
from api.services import workflow_registry, workflow_validator
from tests.test_api.test_workflow_registry import (
    AUTHOR_DID,
    _InspectableSession,
    _step_rows,
    _workflow_payload,
    _workflow_row,
)

VALIDATOR_DID = "did:key:z6MkTravelValidator"


def _validation_row(
    workflow_id,
    validator_did=VALIDATOR_DID,
    validator_domain="TRAVEL",
):
    """Build one pending validation row."""
    now = datetime(2026, 4, 28, tzinfo=timezone.utc)
    return {
        "id": uuid4(),
        "workflow_id": workflow_id,
        "validator_did": validator_did,
        "validator_domain": validator_domain,
        "assigned_at": now,
        "decision": None,
        "decision_at": None,
        "rejection_reason": None,
        "revision_notes": None,
        "checklist": {},
        "created_at": now,
    }


def _workflow_validation_row(workflow_id, status="draft"):
    """Build the narrow workflow row loaded by validation service."""
    row = _workflow_row(workflow_id)
    return {
        "id": row["id"],
        "status": status,
        "spec": row["spec"],
    }


def _approved_checklist() -> dict[str, bool]:
    """Build a passing validation checklist."""
    return {
        "steps_achievable": True,
        "context_minimal": True,
        "trust_thresholds_appropriate": True,
        "no_sensitive_tag_without_domain_review": True,
        "fallback_logic_sound": True,
    }


def test_assign_workflow_to_validator_sets_in_review():
    """POST-style assignment should claim the queue placeholder and mark review active."""
    workflow_id = uuid4()
    queue_validation = _validation_row(
        workflow_id,
        validator_did=workflow_registry.VALIDATION_QUEUE_DID,
    )
    assigned = {**queue_validation, "validator_did": VALIDATOR_DID}
    db = _InspectableSession(
        rows=[
            [_workflow_validation_row(workflow_id, status="draft")],
            [queue_validation],
            [assigned],
            [],
        ]
    )

    response = asyncio.run(
        workflow_validator.assign_workflow_to_validator(
            db=db,
            workflow_id=workflow_id,
            request=ValidationAssignRequest(
                validator_did=VALIDATOR_DID,
                validator_domain="TRAVEL",
            ),
        )
    )

    assert response.validation_id == queue_validation["id"]
    assert response.validator_did == VALIDATOR_DID
    assert response.validator_domain == "TRAVEL"
    assert db.commit_count == 1
    assert any("status = 'in_review'" in sql for sql, _ in db.executed)


def test_assign_workflow_to_validator_rejects_active_assignment():
    """Only one active real validator assignment is allowed per workflow."""
    workflow_id = uuid4()
    db = _InspectableSession(
        rows=[
            [_workflow_validation_row(workflow_id, status="draft")],
            [_validation_row(workflow_id, validator_did=VALIDATOR_DID)],
        ]
    )

    try:
        asyncio.run(
            workflow_validator.assign_workflow_to_validator(
                db=db,
                workflow_id=workflow_id,
                request=ValidationAssignRequest(
                    validator_did="did:key:z6MkSecondValidator",
                    validator_domain="TRAVEL",
                ),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected active validation conflict")

    assert response.status_code == 409
    assert response.detail == "active validation already assigned"
    assert db.rollback_count == 1


def test_record_approved_decision_publishes_hashes_and_scores():
    """Approval should publish the workflow and store the deterministic spec hash."""
    workflow_id = uuid4()
    validation = _validation_row(workflow_id)
    workflow_select = _workflow_validation_row(workflow_id, status="in_review")
    spec_hash = workflow_validator.compute_spec_hash(workflow_select["spec"])
    published_at = datetime(2026, 4, 28, tzinfo=timezone.utc)
    published_row = {
        **_workflow_row(workflow_id),
        "status": "published",
        "spec_hash": spec_hash,
        "quality_score": 42.5,
        "published_at": published_at,
    }
    db = _InspectableSession(
        rows=[
            [validation],
            [workflow_select],
            [],
            [],
            [],
            [published_row],
            _step_rows(workflow_id),
        ]
    )

    response = asyncio.run(
        workflow_validator.record_validator_decision(
            db=db,
            workflow_id=workflow_id,
            request=ValidatorDecisionRequest(
                validator_did=VALIDATOR_DID,
                decision="approved",
                checklist=_approved_checklist(),
            ),
        )
    )

    workflow_update = next(
        params for sql, params in db.executed if "SET status = 'published'" in sql
    )
    assert response.status == "published"
    assert response.spec_hash == spec_hash
    assert response.published_at == published_at
    assert response.quality_score == 42.5
    assert workflow_update["spec_hash"] == spec_hash
    assert workflow_update["quality_score"] == 42.5
    assert db.commit_count == 1


def test_record_revision_requested_returns_workflow_to_draft():
    """Revision requests should return the workflow to draft with notes stored."""
    workflow_id = uuid4()
    draft_row = {
        **_workflow_row(workflow_id),
        "status": "draft",
        "quality_score": 0.0,
    }
    db = _InspectableSession(
        rows=[
            [_validation_row(workflow_id)],
            [_workflow_validation_row(workflow_id, status="in_review")],
            [],
            [],
            [draft_row],
            _step_rows(workflow_id),
        ]
    )

    response = asyncio.run(
        workflow_validator.record_validator_decision(
            db=db,
            workflow_id=workflow_id,
            request=ValidatorDecisionRequest(
                validator_did=VALIDATOR_DID,
                decision="revision_requested",
                checklist={},
                revision_notes="Tighten fallback behavior before publication.",
            ),
        )
    )

    validation_update = next(
        params for sql, params in db.executed if "UPDATE workflow_validations" in sql
    )
    assert response.status == "draft"
    assert validation_update["decision"] == "revision_requested"
    assert validation_update["revision_notes"] == (
        "Tighten fallback behavior before publication."
    )


def test_record_validation_rejects_wrong_validator():
    """Only the assigned validator can submit a validation decision."""
    workflow_id = uuid4()
    db = _InspectableSession(rows=[[_validation_row(workflow_id)]])

    try:
        asyncio.run(
            workflow_validator.record_validator_decision(
                db=db,
                workflow_id=workflow_id,
                request=ValidatorDecisionRequest(
                    validator_did="did:key:z6MkWrongValidator",
                    decision="approved",
                    checklist=_approved_checklist(),
                ),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected validator mismatch")

    assert response.status_code == 403
    assert response.detail == "validator_did does not match assigned validator"
    assert db.rollback_count == 1


def test_update_published_workflow_spec_returns_409():
    """Published workflow specs should be immutable."""
    workflow_id = uuid4()
    db = _InspectableSession(
        rows=[
            [
                {
                    **_workflow_row(workflow_id),
                    "status": "published",
                    "spec_hash": "existing-hash",
                }
            ]
        ]
    )

    try:
        asyncio.run(
            workflow_registry.update_workflow_spec(
                db=db,
                workflow_id=workflow_id,
                request=workflow_registry.WorkflowCreateRequest(**_workflow_payload()),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected immutable spec rejection")

    assert response.status_code == 409
    assert response.detail == (
        "published workflow spec is immutable; submit a new workflow "
        "to create an updated version"
    )


def test_assign_validation_route_returns_assignment(client, admin_api_key_headers, monkeypatch):
    """POST /workflows/{id}/validate should expose validation assignment."""
    workflow_id = uuid4()
    validation = _validation_row(workflow_id)

    async def fake_assign(db, workflow_id, request):
        assert request.validator_did == VALIDATOR_DID
        return ValidationResponse(
            validation_id=validation["id"],
            workflow_id=workflow_id,
            validator_did=request.validator_did,
            validator_domain=request.validator_domain,
            assigned_at=validation["assigned_at"],
            decision=None,
            decision_at=None,
            rejection_reason=None,
            revision_notes=None,
            checklist={},
        )

    monkeypatch.setattr(
        workflows_router.workflow_validator,
        "assign_workflow_to_validator",
        fake_assign,
    )

    response = client.post(
        f"/v1/workflows/{workflow_id}/validate",
        json={"validator_did": VALIDATOR_DID, "validator_domain": "TRAVEL"},
        headers=admin_api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["validation_id"] == str(validation["id"])


def test_validation_decision_route_returns_updated_workflow(
    client,
    api_key_headers,
    monkeypatch,
):
    """PUT /workflows/{id}/validation should expose the updated workflow."""
    workflow_id = uuid4()

    async def fake_decision(db, workflow_id, request):
        row = {
            **_workflow_row(workflow_id),
            "status": "published",
            "spec_hash": "hash",
            "quality_score": 42.5,
        }
        return WorkflowRecord(
            workflow_id=workflow_id,
            name=row["name"],
            slug=row["slug"],
            description=row["description"],
            ontology_domain=row["ontology_domain"],
            tags=row["tags"],
            spec=row["spec"],
            spec_version=row["spec_version"],
            spec_hash=row["spec_hash"],
            author_did=AUTHOR_DID,
            status=row["status"],
            quality_score=row["quality_score"],
            execution_count=0,
            success_count=0,
            failure_count=0,
            parent_workflow_id=None,
            published_at=None,
            deprecated_at=None,
            steps=[],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    monkeypatch.setattr(
        workflows_router.workflow_validator,
        "record_validator_decision",
        fake_decision,
    )

    response = client.put(
        f"/v1/workflows/{workflow_id}/validation",
        json={
            "validator_did": VALIDATOR_DID,
            "decision": "approved",
            "checklist": _approved_checklist(),
        },
        headers=api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "published"
