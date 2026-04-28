"""Tests for Layer 5 Phase 6 workflow hardening."""

from __future__ import annotations

import asyncio
import fnmatch
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.layer3 import RevocationCreateRequest
from api.models.workflow import (
    ValidationAssignRequest,
    ValidatorDecisionRequest,
    WorkflowCreateRequest,
)
from api.services import attestation, workflow_executor, workflow_registry
from api.services import workflow_validator
from tests.test_api.test_workflow_registry import (
    AUTHOR_DID,
    _InspectableSession,
    _step_rows,
    _workflow_payload,
    _workflow_row,
)
from tests.test_api.test_workflow_validator import (
    VALIDATOR_DID,
    _approved_checklist,
    _validation_row,
    _workflow_validation_row,
)


class _FakeRedis:
    """Redis test double for workflow cache and rate-limit tests."""

    def __init__(self, store=None, incr_value: int = 1, ttl_value: int = 60):
        self.store = dict(store or {})
        self.incr_value = incr_value
        self.ttl_value = ttl_value
        self.set_calls = []
        self.delete_calls = []
        self.expire_calls = []
        self.incr_calls = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))
        return True

    async def delete(self, *keys):
        self.delete_calls.append(keys)
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    async def scan_iter(self, match=None):
        pattern = match or "*"
        for key in list(self.store):
            if fnmatch.fnmatch(str(key), pattern):
                yield key

    async def incr(self, key):
        self.incr_calls.append(key)
        return self.incr_value

    async def expire(self, key, seconds):
        self.expire_calls.append((key, seconds))
        return True

    async def ttl(self, key):
        return self.ttl_value


def _published_workflow_row(workflow_id):
    """Build a stored published workflow row."""
    return {
        **_workflow_row(workflow_id),
        "status": "published",
        "spec_hash": "published-hash",
        "quality_score": 42.5,
        "published_at": datetime(2026, 4, 28, tzinfo=timezone.utc),
    }


def test_workflow_detail_read_uses_redis_cache_on_second_call():
    """Individual workflow reads should cache for 60 seconds."""
    workflow_id = uuid4()
    db = _InspectableSession(rows=[[_workflow_row(workflow_id)], _step_rows(workflow_id)])
    redis = _FakeRedis()

    first = asyncio.run(
        workflow_registry.get_workflow(
            db=db,
            workflow_id=workflow_id,
            redis=redis,
        )
    )
    execute_count = len(db.executed)
    second = asyncio.run(
        workflow_registry.get_workflow(
            db=db,
            workflow_id=workflow_id,
            redis=redis,
        )
    )

    assert second == first
    assert len(db.executed) == execute_count
    assert redis.set_calls[0][0] == workflow_registry.workflow_detail_cache_key(workflow_id)
    assert redis.set_calls[0][2] == workflow_registry.WORKFLOW_CACHE_TTL_SECONDS


def test_workflow_list_read_uses_redis_cache_on_second_call():
    """Workflow list queries should cache the exact filter variant."""
    workflow_id = uuid4()
    row = {
        **_workflow_row(workflow_id),
        "status": "published",
        "quality_score": 82.5,
        "execution_count": 100,
        "step_count": 2,
        "published_at": datetime(2026, 4, 28, tzinfo=timezone.utc),
    }
    db = _InspectableSession(rows=[[{"total": 1}], [row]])
    redis = _FakeRedis()

    first = asyncio.run(
        workflow_registry.list_workflows(
            db=db,
            domain="TRAVEL",
            tags=["travel.air.book"],
            redis=redis,
        )
    )
    execute_count = len(db.executed)
    second = asyncio.run(
        workflow_registry.list_workflows(
            db=db,
            domain="TRAVEL",
            tags=["travel.air.book"],
            redis=redis,
        )
    )

    assert second == first
    assert len(db.executed) == execute_count
    assert redis.set_calls[0][0].startswith("workflow:list:")
    assert redis.set_calls[0][2] == workflow_registry.WORKFLOW_CACHE_TTL_SECONDS


def test_workflow_cache_invalidation_removes_detail_slug_and_list_keys():
    """Workflow invalidation should clear stale list and detail cache entries."""
    workflow_id = uuid4()
    redis = _FakeRedis(
        {
            workflow_registry.workflow_detail_cache_key(workflow_id): "detail",
            workflow_registry.workflow_slug_cache_key("business-travel-booking"): "slug",
            "workflow:list:abc": "list",
            f"workflow:rank:{workflow_id}:any:any:anonymous": "rank",
        }
    )

    asyncio.run(
        workflow_registry.invalidate_workflow_caches(
            redis,
            workflow_id=workflow_id,
            slug="business-travel-booking",
        )
    )

    assert workflow_registry.workflow_detail_cache_key(workflow_id) not in redis.store
    assert workflow_registry.workflow_slug_cache_key("business-travel-booking") not in redis.store
    assert "workflow:list:abc" not in redis.store
    assert f"workflow:rank:{workflow_id}:any:any:anonymous" in redis.store


def test_workflow_query_rate_limit_returns_429_after_200_requests():
    """Workflow queries should be limited to 200 per API key per minute."""
    redis = _FakeRedis(incr_value=201, ttl_value=17)

    try:
        asyncio.run(
            workflow_registry.enforce_workflow_query_rate_limit(
                redis,
                "test-api-key",
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected workflow query rate limit")

    assert response.status_code == 429
    assert response.detail["limit"] == 200
    assert response.detail["retry_after_seconds"] == 17


def test_draft_workflow_spec_update_rewrites_steps_and_returns_fresh_record():
    """Draft workflow spec updates should rewrite stored spec and step rows."""
    workflow_id = uuid4()
    payload = _workflow_payload()
    payload["workflow_id"] = str(workflow_id)
    payload["description"] = "Updated business travel workflow."
    updated_row = {
        **_workflow_row(workflow_id),
        "description": "Updated business travel workflow.",
        "status": "draft",
    }
    updated_row["spec"]["description"] = "Updated business travel workflow."
    db = _InspectableSession(
        rows=[
            [_workflow_row(workflow_id)],
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
            [],
            [updated_row],
            _step_rows(workflow_id),
        ]
    )

    response = asyncio.run(
        workflow_registry.update_workflow_spec(
            db=db,
            workflow_id=workflow_id,
            request=WorkflowCreateRequest(**payload),
        )
    )

    assert response.description == "Updated business travel workflow."
    assert response.status == "draft"
    assert db.commit_count == 1
    assert any("DELETE FROM workflow_steps" in sql for sql, _ in db.executed)
    assert any("INSERT INTO workflow_steps" in sql for sql, _ in db.executed)


def test_pinned_service_without_declared_required_fields_is_rejected():
    """Pinned services must declare required context fields from workflow steps."""
    service_id = uuid4()
    payload = _workflow_payload()
    payload["steps"][0]["service_id"] = str(service_id)
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
            [{"service_id": service_id}],
            [{"field_name": "user.name"}],
        ]
    )

    try:
        asyncio.run(
            workflow_registry.create_workflow(
                db=db,
                request=WorkflowCreateRequest(**payload),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected missing pinned context field rejection")

    assert response.status_code == 422
    assert "does not declare required context fields" in response.detail


def test_publication_invalidates_workflow_read_caches():
    """Publishing a workflow should clear stale list/detail caches before returning."""
    workflow_id = uuid4()
    workflow_select = _workflow_validation_row(workflow_id, status="in_review")
    published_row = _published_workflow_row(workflow_id)
    db = _InspectableSession(
        rows=[
            [_validation_row(workflow_id)],
            [workflow_select],
            [],
            [],
            [],
            [published_row],
            _step_rows(workflow_id),
        ]
    )
    redis = _FakeRedis(
        {
            workflow_registry.workflow_detail_cache_key(workflow_id): "stale",
            "workflow:list:old": "stale-list",
        }
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
            redis=redis,
        )
    )

    assert response.status == "published"
    assert "workflow:list:old" not in redis.store
    cached = redis.store[workflow_registry.workflow_detail_cache_key(workflow_id)]
    assert '"status":"published"' in cached


def test_assign_workflow_to_validator_creates_record_when_queue_is_empty():
    """Validation assignment should create a pending record when none exists."""
    workflow_id = uuid4()
    validation = _validation_row(workflow_id, validator_did=VALIDATOR_DID)
    db = _InspectableSession(
        rows=[
            [_workflow_validation_row(workflow_id, status="draft")],
            [],
            [validation],
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

    assert response.validation_id == validation["id"]
    assert response.validator_did == VALIDATOR_DID
    assert any("INSERT INTO workflow_validations" in sql for sql, _ in db.executed)


def test_quality_score_update_invalidates_rank_and_workflow_read_caches():
    """Quality changes should invalidate rank, list, and detail cache entries."""
    workflow_id = uuid4()
    redis = _FakeRedis(
        {
            workflow_registry.workflow_detail_cache_key(workflow_id): "stale-detail",
            workflow_registry.workflow_slug_cache_key("business-travel-booking"): "stale-slug",
            "workflow:list:old": "stale-list",
            f"workflow:rank:{workflow_id}:any:any:anonymous": "stale-rank",
        }
    )
    db = _InspectableSession(
        rows=[
            [{"status": "published", "execution_count": 1, "success_count": 1}],
            [{"verified_count": 0}],
            [],
            [],
        ]
    )

    score = asyncio.run(
        workflow_executor._recompute_and_store_quality(  # noqa: SLF001
            db=db,
            workflow_id=workflow_id,
            redis=redis,
        )
    )

    assert score == 42.8
    assert workflow_registry.workflow_detail_cache_key(workflow_id) not in redis.store
    assert workflow_registry.workflow_slug_cache_key("business-travel-booking") not in redis.store
    assert "workflow:list:old" not in redis.store
    assert f"workflow:rank:{workflow_id}:any:any:anonymous" not in redis.store


def test_revoked_pinned_service_flags_published_workflow_for_revalidation():
    """Required pinned-service revocation should move published workflows to review."""
    workflow_id = uuid4()
    service_id = uuid4()
    db = _InspectableSession(
        rows=[
            [
                {
                    "id": workflow_id,
                    "slug": "business-travel-booking",
                    "ontology_domain": "TRAVEL",
                }
            ],
            [],
            [],
        ]
    )
    redis = _FakeRedis({"workflow:list:old": "stale"})

    flagged = asyncio.run(
        workflow_registry.flag_workflows_for_revoked_service(
            db=db,
            service_id=service_id,
            redis=redis,
        )
    )

    assert flagged == [workflow_id]
    assert db.commit_count == 1
    assert any("status = 'in_review'" in sql for sql, _ in db.executed)
    validation_params = next(
        params for sql, params in db.executed if "INSERT INTO workflow_validations" in sql
    )
    assert validation_params["validator_did"] == workflow_registry.VALIDATION_QUEUE_DID
    assert "workflow:list:old" not in redis.store


def test_service_revocation_calls_workflow_revalidation_hook(monkeypatch):
    """Layer 3 revocation should trigger the Layer 5 pinned workflow check."""
    service_id = uuid4()
    revocation_id = uuid4()
    redis = _FakeRedis()
    db = _InspectableSession(
        rows=[
            [{"id": uuid4(), "is_active": True}],
            [{"id": service_id, "domain": "skybridge.example"}],
            [{"id": revocation_id}],
        ]
    )
    calls = []

    async def fake_record_chain_event(**kwargs):
        return "0xrevoked", 42

    async def fake_flag_workflows(*, db, service_id, redis=None):
        calls.append({"service_id": service_id, "redis": redis})
        return []

    monkeypatch.setattr(attestation.chain, "record_chain_event", fake_record_chain_event)
    monkeypatch.setattr(
        attestation.workflow_registry,
        "flag_workflows_for_revoked_service",
        fake_flag_workflows,
    )

    response = asyncio.run(
        attestation.submit_revocation(
            db=db,
            request=RevocationCreateRequest(
                auditor_did="did:web:auditfirm.example",
                service_domain="skybridge.example",
                reason_code="security_incident",
                evidence_package={"report": "IR-123"},
            ),
            redis=redis,
        )
    )

    assert response.revocation_id == revocation_id
    assert calls == [{"service_id": service_id, "redis": redis}]
