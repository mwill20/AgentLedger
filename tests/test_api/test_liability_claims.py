"""Tests for Layer 6 liability dispute protocol."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.models.liability import ClaimResponse
from api.routers import liability as liability_router
from api.services import liability_claims

AGENT_DID = "did:key:z6MkLiabilityClaimAgent"
REVIEWER_DID = "did:key:z6MkLiabilityReviewer"


class _FakeMappings:
    """Minimal mappings wrapper for liability claim tests."""

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


class _FakeBackgroundTasks:
    """Background task collector for create-claim tests."""

    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


class _FakeRedis:
    """Small async Redis double for claim hardening tests."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.deleted = []

    async def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1


class _ClaimsSession:
    """SQL-aware fake DB for liability claim service tests."""

    def __init__(
        self,
        *,
        claim_status: str = "filed",
        duplicate_claim: bool = False,
        disclosures: list[dict] | None = None,
        mismatches: list[dict] | None = None,
        revocations: list[dict] | None = None,
    ):
        self.workflow_id = uuid4()
        self.execution_id = uuid4()
        self.snapshot_id = uuid4()
        self.claim_id = uuid4()
        self.validation_id = uuid4()
        self.service_id = uuid4()
        self.manifest_id = uuid4()
        self.capability_id = uuid4()
        self.reported_at = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        self.claim_status = claim_status
        self.duplicate_claim = duplicate_claim
        self.disclosures = disclosures if disclosures is not None else [self._disclosure()]
        self.mismatches = mismatches if mismatches is not None else [self._mismatch()]
        self.revocations = revocations if revocations is not None else []
        self.evidence_by_key = {}
        self.evidence_inserts = []
        self.executed = []
        self.commit_count = 0
        self.rollback_count = 0

    def _claim_row(self):
        return {
            "id": self.claim_id,
            "execution_id": self.execution_id,
            "snapshot_id": self.snapshot_id,
            "claimant_did": AGENT_DID,
            "claim_type": "service_failure",
            "description": "Flight booking confirmed but no ticket was issued.",
            "harm_value_usd": 450.0,
            "status": self.claim_status,
            "reviewer_did": None,
            "resolution_note": None,
            "filed_at": self.reported_at,
            "evidence_gathered_at": None,
            "determined_at": None,
            "resolved_at": None,
            "created_at": self.reported_at,
            "updated_at": self.reported_at,
        }

    def _execution_row(self):
        return {
            "id": self.execution_id,
            "workflow_id": self.workflow_id,
            "agent_did": AGENT_DID,
            "context_bundle_id": uuid4(),
            "outcome": "failure",
            "steps_completed": 1,
            "steps_total": 2,
            "failure_step_number": 2,
            "failure_reason": "No ticket issued.",
            "duration_ms": 180000,
            "reported_at": self.reported_at,
            "verified": False,
            "created_at": self.reported_at,
        }

    def _snapshot_row(self):
        return {
            "id": self.snapshot_id,
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "agent_did": AGENT_DID,
            "captured_at": self.reported_at,
            "workflow_quality_score": 88.4,
            "workflow_author_did": "did:key:z6MkWorkflowAuthor",
            "workflow_validator_did": "did:key:z6MkWorkflowValidator",
            "workflow_validation_checklist": {"steps_achievable": True},
            "step_trust_states": [
                {
                    "step_number": 1,
                    "ontology_tag": "travel.air.book",
                    "service_id": self.service_id,
                    "service_name": "FlightBookerPro",
                    "min_trust_tier": 3,
                    "min_trust_score": 75.0,
                    "trust_score": 91.2,
                    "trust_tier": 4,
                    "trust_score_source": "services.trust_score_at_snapshot",
                }
            ],
            "context_summary": {"fields_disclosed": ["user.name"]},
            "critical_mismatch_count": 1,
            "agent_profile_default_policy": "deny",
            "created_at": self.reported_at,
        }

    def _disclosure(self, erased: bool = False):
        return {
            "id": uuid4(),
            "service_id": self.service_id,
            "ontology_tag": "travel.air.book",
            "fields_disclosed": ["user.name", "user.email"],
            "fields_withheld": ["user.ssn"],
            "fields_committed": ["user.passport_number"],
            "disclosure_method": "direct",
            "trust_score_at_disclosure": 91.2,
            "trust_tier_at_disclosure": 4,
            "erased": erased,
            "created_at": self.reported_at,
        }

    def _mismatch(self):
        return {
            "id": uuid4(),
            "service_id": self.service_id,
            "declared_fields": ["user.name"],
            "requested_fields": ["user.name", "user.ssn"],
            "over_requested_fields": ["user.ssn"],
            "severity": "critical",
            "resolved": False,
            "created_at": self.reported_at,
        }

    async def execute(self, statement, params=None):
        sql = statement.text if hasattr(statement, "text") else str(statement)
        params = params or {}
        self.executed.append((sql, params))

        if "FROM liability_claims" in sql and "WHERE execution_id = :execution_id" in sql:
            return _FakeResult([{"id": self.claim_id}] if self.duplicate_claim else [])
        if "INSERT INTO liability_claims" in sql:
            return _FakeResult([self._claim_row()])
        if "FROM liability_claims" in sql and "WHERE id = :claim_id" in sql:
            return _FakeResult([self._claim_row()])
        if "UPDATE liability_claims" in sql and "status = 'evidence_gathered'" in sql:
            self.claim_status = "evidence_gathered"
            return _FakeResult([])
        if "UPDATE liability_claims" in sql and "status = 'resolved'" in sql:
            self.claim_status = "resolved"
            row = self._claim_row()
            row["status"] = "resolved"
            row["reviewer_did"] = params["reviewer_did"]
            row["resolution_note"] = params["resolution_note"]
            row["resolved_at"] = self.reported_at
            return _FakeResult([row])
        if "UPDATE liability_claims" in sql and "status = 'under_review'" in sql:
            self.claim_status = "under_review"
            row = self._claim_row()
            row["status"] = "under_review"
            row["resolution_note"] = params["appeal_reason"]
            return _FakeResult([row])

        if "FROM workflow_executions" in sql:
            return _FakeResult([self._execution_row()])
        if "FROM liability_snapshots" in sql and "WHERE execution_id" in sql:
            return _FakeResult([self._snapshot_row()])
        if "FROM workflow_validations" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.validation_id,
                        "validator_did": "did:key:z6MkWorkflowValidator",
                        "validator_domain": "TRAVEL",
                        "decision": "approved",
                        "decision_at": self.reported_at,
                        "checklist": {"steps_achievable": True},
                        "rejection_reason": None,
                    }
                ]
            )
        if "FROM context_disclosures" in sql:
            return _FakeResult(self.disclosures)
        if "FROM context_mismatch_events" in sql:
            return _FakeResult(self.mismatches)
        if "FROM manifests" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.manifest_id,
                        "manifest_hash": "manifest-hash",
                        "manifest_version": "1.0",
                        "raw_json": {
                            "capabilities": [
                                {"ontology_tag": "travel.air.book"},
                                {"ontology_tag": "travel.lodging.book"},
                            ]
                        },
                        "crawled_at": self.reported_at,
                        "service_name": "FlightBookerPro",
                    }
                ]
            )
        if "FROM service_capabilities" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.capability_id,
                        "ontology_tag": "travel.air.book",
                        "is_verified": True,
                        "verified_at": self.reported_at,
                        "success_rate_30d": 0.97,
                    }
                ]
            )
        if "FROM revocation_events" in sql:
            return _FakeResult(self.revocations)

        if "FROM liability_evidence" in sql and "SELECT id" in sql:
            key = (params["claim_id"], params["source_table"], params["source_id"])
            existing = self.evidence_by_key.get(key)
            return _FakeResult([{"id": existing["id"]}] if existing else [])
        if "INSERT INTO liability_evidence" in sql:
            key = (params["claim_id"], params["source_table"], params["source_id"])
            if key not in self.evidence_by_key:
                stored = dict(params)
                stored["id"] = uuid4()
                self.evidence_by_key[key] = stored
                self.evidence_inserts.append(stored)
            return _FakeResult([])
        if "COUNT(*) AS evidence_count" in sql:
            return _FakeResult([{"evidence_count": len(self.evidence_by_key)}])
        if "FROM liability_evidence" in sql and "ORDER BY gathered_at" in sql:
            rows = []
            for stored in self.evidence_by_key.values():
                rows.append(
                    {
                        "id": stored["id"],
                        "claim_id": stored["claim_id"],
                        "evidence_type": stored["evidence_type"],
                        "source_table": stored["source_table"],
                        "source_id": stored["source_id"],
                        "source_layer": stored["source_layer"],
                        "summary": stored["summary"],
                        "raw_data": stored["raw_data"],
                        "gathered_at": self.reported_at,
                        "created_at": self.reported_at,
                    }
                )
            return _FakeResult(rows)
        if "FROM liability_determinations" in sql:
            return _FakeResult([])

        return _FakeResult([])

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _claim_payload(execution_id):
    """Build a valid claim request payload."""
    return {
        "execution_id": str(execution_id),
        "claimant_did": AGENT_DID,
        "claim_type": "service_failure",
        "description": "Flight booking confirmed but no ticket was issued.",
        "harm_value_usd": 450.0,
    }


def test_create_claim_route_returns_201(client, api_key_headers, monkeypatch):
    """POST /liability/claims should expose claim filing."""
    db = _ClaimsSession()

    async def fake_create_claim(**kwargs):
        assert kwargs["claimant_did"] == AGENT_DID
        row = db._claim_row()
        return ClaimResponse(
            claim_id=row["id"],
            execution_id=row["execution_id"],
            snapshot_id=row["snapshot_id"],
            claimant_did=row["claimant_did"],
            claim_type=row["claim_type"],
            description=row["description"],
            harm_value_usd=row["harm_value_usd"],
            status=row["status"],
            reviewer_did=row["reviewer_did"],
            resolution_note=row["resolution_note"],
            filed_at=row["filed_at"],
            evidence_gathered_at=row["evidence_gathered_at"],
            determined_at=row["determined_at"],
            resolved_at=row["resolved_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    monkeypatch.setattr(
        liability_router.liability_claims,
        "create_claim",
        fake_create_claim,
    )

    response = client.post(
        "/v1/liability/claims",
        json=_claim_payload(db.execution_id),
        headers=api_key_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["execution_id"] == str(db.execution_id)
    assert body["status"] == "filed"


def test_create_claim_duplicate_returns_409():
    """Duplicate claim filing should be rejected for execution_id + claimant_did."""
    db = _ClaimsSession(duplicate_claim=True)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            liability_claims.create_claim(
                execution_id=db.execution_id,
                claimant_did=AGENT_DID,
                claim_type="service_failure",
                description="Duplicate.",
                harm_value_usd=10.0,
                db=db,
                background_tasks=_FakeBackgroundTasks(),
                verify_sync=False,
            )
        )

    assert exc_info.value.status_code == 409


def test_gather_evidence_inserts_sources_1_through_7_and_is_idempotent():
    """Evidence gathering should attach all non-revocation sources and skip duplicates."""
    db = _ClaimsSession()

    first = asyncio.run(liability_claims.gather_evidence(db.claim_id, db=db))
    second = asyncio.run(liability_claims.gather_evidence(db.claim_id, db=db))

    evidence_types = {row["evidence_type"] for row in db.evidence_inserts}
    assert first.evidence_count == 7
    assert second.evidence_count == 7
    assert len(db.evidence_inserts) == 7
    assert evidence_types == {
        "workflow_execution",
        "validation_record",
        "liability_snapshot",
        "context_disclosure",
        "context_mismatch",
        "manifest_version",
        "service_capability",
    }
    assert "trust_revocation" not in evidence_types


def test_get_claim_detail_returns_evidence_count_at_least_three():
    """GET claim detail service response should include gathered evidence and count."""
    db = _ClaimsSession()
    asyncio.run(liability_claims.gather_evidence(db.claim_id, db=db))

    detail = asyncio.run(
        liability_claims.get_claim_detail(
            claim_id=db.claim_id,
            db=db,
        )
    )

    assert detail.claim_id == db.claim_id
    assert detail.evidence_count >= 3
    assert detail.determination is None


def test_erased_context_disclosure_evidence_uses_empty_raw_data():
    """Erased disclosures should remain as evidence without field metadata."""
    db = _ClaimsSession(disclosures=[])
    db.disclosures = [db._disclosure(erased=True)]

    asyncio.run(liability_claims.gather_evidence(db.claim_id, db=db))

    disclosure = next(
        row
        for row in db.evidence_inserts
        if row["evidence_type"] == "context_disclosure"
    )
    assert disclosure["summary"] == "[ERASED - field data unavailable]"
    assert disclosure["raw_data"] == "{}"


def test_resolve_claim_requires_determined_status():
    """Resolving before attribution determination should return 409."""
    db = _ClaimsSession(claim_status="evidence_gathered")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            liability_claims.resolve_claim(
                claim_id=db.claim_id,
                resolution_note="Refund issued.",
                reviewer_did=REVIEWER_DID,
                db=db,
            )
        )

    assert exc_info.value.status_code == 409


def test_claim_filing_rate_limit_blocks_eleventh_claim_per_hour():
    """Claim filing should be limited to 10 per claimant per hour."""
    redis = _FakeRedis()

    for _ in range(10):
        asyncio.run(liability_claims.enforce_claim_filing_rate_limit(redis, AGENT_DID))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(liability_claims.enforce_claim_filing_rate_limit(redis, AGENT_DID))

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "claim filing rate limit exceeded"
    assert redis.ttls[liability_claims.claim_rate_limit_key(AGENT_DID)] == 3600


def test_claim_status_cache_refreshes_on_status_transitions():
    """Claim status transitions should replace the short-lived Redis status cache."""
    redis = _FakeRedis()
    cache_key = None

    gather_db = _ClaimsSession()
    asyncio.run(
        liability_claims.gather_evidence(
            gather_db.claim_id,
            db=gather_db,
            redis=redis,
        )
    )
    cache_key = liability_claims.claim_status_cache_key(gather_db.claim_id)
    assert cache_key in redis.deleted
    assert redis.store[cache_key] == "evidence_gathered"
    assert redis.ttls[cache_key] == 60

    resolve_db = _ClaimsSession(claim_status="determined")
    asyncio.run(
        liability_claims.resolve_claim(
            claim_id=resolve_db.claim_id,
            resolution_note="Refund issued.",
            reviewer_did=REVIEWER_DID,
            db=resolve_db,
            redis=redis,
        )
    )
    cache_key = liability_claims.claim_status_cache_key(resolve_db.claim_id)
    assert cache_key in redis.deleted
    assert redis.store[cache_key] == "resolved"
    assert redis.ttls[cache_key] == 60

    appeal_db = _ClaimsSession(claim_status="determined")
    asyncio.run(
        liability_claims.appeal_claim(
            claim_id=appeal_db.claim_id,
            appeal_reason="Evidence omitted.",
            claimant_did=AGENT_DID,
            db=appeal_db,
            redis=redis,
        )
    )
    cache_key = liability_claims.claim_status_cache_key(appeal_db.claim_id)
    assert cache_key in redis.deleted
    assert redis.store[cache_key] == "under_review"
    assert redis.ttls[cache_key] == 60
