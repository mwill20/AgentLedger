"""Tests for Layer 6 liability attribution engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.models.liability import DeterminationResponse
from api.routers import liability as liability_router
from api.services import liability_attribution, liability_claims

AGENT_DID = "did:key:z6MkAttributionAgent"
AUTHOR_DID = "did:key:z6MkAttributionAuthor"
VALIDATOR_DID = "did:key:z6MkAttributionValidator"


class _FakeMappings:
    """Minimal mappings wrapper for attribution tests."""

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


class _FakeRedis:
    """Small async Redis double for determination status cache tests."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.deleted = []

    async def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    async def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)
        return 1


class _DetermineSession:
    """SQL-aware fake DB for determine_claim tests."""

    def __init__(self, *, claim_status: str = "evidence_gathered"):
        self.workflow_id = uuid4()
        self.execution_id = uuid4()
        self.snapshot_id = uuid4()
        self.claim_id = uuid4()
        self.service_id = uuid4()
        self.determination_id = uuid4()
        self.reported_at = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        self.claim_status = claim_status
        self.executed = []
        self.commit_count = 0
        self.rollback_count = 0

    def _claim(self):
        return {
            "id": self.claim_id,
            "execution_id": self.execution_id,
            "snapshot_id": self.snapshot_id,
            "claimant_did": AGENT_DID,
            "claim_type": "service_failure",
            "description": "No ticket issued.",
            "harm_value_usd": 450.0,
            "status": self.claim_status,
            "reviewer_did": None,
            "resolution_note": None,
            "filed_at": self.reported_at,
            "evidence_gathered_at": self.reported_at,
            "determined_at": None,
            "resolved_at": None,
            "created_at": self.reported_at,
            "updated_at": self.reported_at,
        }

    def _snapshot(self):
        return {
            "id": self.snapshot_id,
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "agent_did": AGENT_DID,
            "captured_at": self.reported_at,
            "workflow_quality_score": 88.0,
            "workflow_author_did": AUTHOR_DID,
            "workflow_validator_did": VALIDATOR_DID,
            "workflow_validation_checklist": {
                "context_minimal": True,
                "trust_thresholds_appropriate": True,
            },
            "step_trust_states": [
                {
                    "step_number": 1,
                    "ontology_tag": "travel.air.book",
                    "service_id": self.service_id,
                    "service_name": "FlightBookerPro",
                    "min_trust_tier": 3,
                    "min_trust_score": 75.0,
                    "trust_score": 60.0,
                    "trust_tier": 3,
                    "trust_score_source": "services.trust_score_at_snapshot",
                }
            ],
            "context_summary": {"fields_disclosed": ["user.name"]},
            "critical_mismatch_count": 0,
            "agent_profile_default_policy": "deny",
            "created_at": self.reported_at,
        }

    def _execution(self):
        return {
            "id": self.execution_id,
            "workflow_id": self.workflow_id,
            "agent_did": AGENT_DID,
            "outcome": "failure",
            "steps_completed": 1,
            "steps_total": 1,
            "failure_step_number": 1,
            "failure_reason": "No ticket.",
            "duration_ms": 1000,
            "reported_at": self.reported_at,
            "verified": False,
        }

    def _workflow_steps(self):
        return [
            {
                "id": uuid4(),
                "step_number": 1,
                "ontology_tag": "travel.air.book",
                "service_id": self.service_id,
                "is_required": True,
                "fallback_step_number": 2,
                "min_trust_tier": 3,
                "min_trust_score": 75.0,
                "sensitivity_tier": 1,
            }
        ]

    def _evidence(self):
        return [
            {
                "id": uuid4(),
                "claim_id": self.claim_id,
                "evidence_type": "service_capability",
                "source_table": "service_capabilities",
                "source_id": uuid4(),
                "source_layer": 1,
                "summary": "Capability not verified",
                "raw_data": {"is_verified": False},
                "gathered_at": self.reported_at,
                "created_at": self.reported_at,
            }
        ]

    async def execute(self, statement, params=None):
        sql = statement.text if hasattr(statement, "text") else str(statement)
        params = params or {}
        self.executed.append((sql, params))
        if "FROM liability_claims" in sql and "WHERE id = :claim_id" in sql:
            return _FakeResult([self._claim()])
        if "FROM liability_snapshots" in sql:
            return _FakeResult([self._snapshot()])
        if "FROM liability_evidence" in sql and "ORDER BY gathered_at" in sql:
            return _FakeResult(self._evidence())
        if "FROM workflows" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.workflow_id,
                        "quality_score": 88.0,
                        "author_did": AUTHOR_DID,
                        "status": "published",
                    }
                ]
            )
        if "FROM workflow_steps" in sql:
            return _FakeResult(self._workflow_steps())
        if "FROM workflow_executions" in sql:
            return _FakeResult([self._execution()])
        if "COUNT(*) AS determination_count" in sql:
            return _FakeResult([{"determination_count": 0}])
        if "INSERT INTO liability_determinations" in sql:
            return _FakeResult(
                [
                    {
                        "id": self.determination_id,
                        "claim_id": self.claim_id,
                        "determination_version": params["determination_version"],
                        "agent_weight": params["agent_weight"],
                        "service_weight": params["service_weight"],
                        "workflow_author_weight": params["workflow_author_weight"],
                        "validator_weight": params["validator_weight"],
                        "confidence": params["confidence"],
                        "determined_by": params["determined_by"],
                        "determined_at": self.reported_at,
                    }
                ]
            )
        if "UPDATE liability_claims" in sql and "status = 'determined'" in sql:
            self.claim_status = "determined"
            return _FakeResult([])
        return _FakeResult([])

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _claim(claim_type: str = "service_failure"):
    return {
        "id": uuid4(),
        "claim_type": claim_type,
        "claimant_did": AGENT_DID,
    }


def _snapshot(
    *,
    trust_score: float = 91.0,
    trust_tier: int = 4,
    quality_score: float = 88.0,
    checklist: dict | None = None,
):
    return {
        "id": uuid4(),
        "workflow_quality_score": quality_score,
        "workflow_author_did": AUTHOR_DID,
        "workflow_validator_did": VALIDATOR_DID,
        "workflow_validation_checklist": checklist or {},
        "step_trust_states": [
            {
                "step_number": 1,
                "ontology_tag": "travel.air.book",
                "service_id": uuid4(),
                "service_name": "FlightBookerPro",
                "trust_score": trust_score,
                "trust_tier": trust_tier,
            }
        ],
    }


def _workflow_steps(
    *,
    min_score: float = 75.0,
    min_tier: int = 3,
    sensitivity_tier: int = 1,
    fallback_step_number: int | None = 2,
):
    return [
        {
            "id": uuid4(),
            "step_number": 1,
            "ontology_tag": "travel.air.book",
            "is_required": True,
            "fallback_step_number": fallback_step_number,
            "min_trust_tier": min_tier,
            "min_trust_score": min_score,
            "sensitivity_tier": sensitivity_tier,
        }
    ]


def _execution(reported_at: datetime | None = None):
    return {
        "id": uuid4(),
        "reported_at": reported_at
        or datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    }


def _evidence(evidence_type: str, raw_data: dict):
    return {
        "id": uuid4(),
        "evidence_type": evidence_type,
        "raw_data": raw_data,
    }


def _compute(**kwargs):
    defaults = {
        "claim": _claim(),
        "snapshot": _snapshot(),
        "evidence": [],
        "workflow": {"id": uuid4()},
        "workflow_steps": _workflow_steps(),
        "execution": _execution(),
        "db": None,
    }
    defaults.update(kwargs)
    return liability_attribution.compute_attribution(**defaults)


def test_determine_claim_route_returns_weights_summing_to_one(
    client,
    api_key_headers,
    monkeypatch,
):
    """POST /liability/claims/{id}/determine should expose attribution output."""
    claim_id = uuid4()
    determination_id = uuid4()
    determined_at = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)

    async def fake_determine_claim(*, claim_id, reviewer_did, db, redis=None):
        del db
        del redis
        assert reviewer_did == VALIDATOR_DID
        return DeterminationResponse(
            determination_id=determination_id,
            claim_id=claim_id,
            determination_version=1,
            attribution={
                "agent": 0.35,
                "service": 0.35,
                "workflow_author": 0.15,
                "validator": 0.15,
            },
            applied_factors=[],
            confidence=0.5,
            determined_by="reviewer",
            determined_at=determined_at,
        )

    monkeypatch.setattr(
        liability_router.liability_attribution,
        "determine_claim",
        fake_determine_claim,
    )

    response = client.post(
        f"/v1/liability/claims/{claim_id}/determine",
        json={"reviewer_did": VALIDATOR_DID},
        headers=api_key_headers,
    )

    assert response.status_code == 200
    attribution = response.json()["attribution"]
    assert round(sum(attribution.values()), 4) == 1.0


def test_scenario_a_undertrusted_unverified_service_matches_manual_math():
    """Scenario A should split leading attribution between agent and service."""
    result = _compute(
        snapshot=_snapshot(trust_score=60.0, trust_tier=3),
        evidence=[
            _evidence("service_capability", {"is_verified": False}),
        ],
        workflow_steps=_workflow_steps(min_score=75.0, min_tier=3),
    )

    assert result.weights == {
        "agent": 0.35,
        "service": 0.35,
        "workflow_author": 0.15,
        "validator": 0.15,
    }
    assert result.confidence == 0.5
    assert [factor.factor for factor in result.applied_factors] == [
        "service_trust_below_step_minimum",
        "service_capability_not_verified",
    ]


def test_scenario_b_mismatch_and_validator_factor_matches_manual_math():
    """Scenario B should match the specified data-misuse attribution math."""
    result = _compute(
        claim=_claim("data_misuse"),
        snapshot=_snapshot(
            trust_score=91.0,
            trust_tier=4,
            checklist={"context_minimal": True},
        ),
        evidence=[
            _evidence(
                "context_mismatch",
                {"severity": "critical", "over_requested_fields": ["user.ssn"]},
            )
        ],
        workflow_steps=_workflow_steps(min_score=75.0, min_tier=3),
    )

    assert result.weights == {
        "agent": 0.35,
        "service": 0.35,
        "workflow_author": 0.0833,
        "validator": 0.2167,
    }
    assert result.confidence == 0.6
    assert [factor.factor for factor in result.applied_factors] == [
        "critical_context_mismatch_ignored",
        "service_context_over_request",
        "validator_approved_non_minimal_context",
    ]


@pytest.mark.parametrize(
    "result",
    [
        _compute(),
        _compute(
            snapshot=_snapshot(trust_score=60.0),
            evidence=[_evidence("service_capability", {"is_verified": False})],
        ),
        _compute(
            claim=_claim("data_misuse"),
            snapshot=_snapshot(checklist={"context_minimal": True}),
            evidence=[_evidence("context_mismatch", {"severity": "critical"})],
        ),
        _compute(
            snapshot=_snapshot(
                trust_score=60.0,
                trust_tier=2,
                quality_score=50.0,
                checklist={
                    "context_minimal": True,
                    "trust_thresholds_appropriate": True,
                },
            ),
            evidence=[
                _evidence("context_mismatch", {"severity": "critical"}),
                _evidence("service_capability", {"is_verified": False}),
            ],
            workflow_steps=_workflow_steps(
                min_score=80.0,
                min_tier=3,
                sensitivity_tier=3,
                fallback_step_number=None,
            ),
        ),
        _compute(
            claim=_claim("service_failure"),
            snapshot=_snapshot(trust_score=60.0, trust_tier=2, quality_score=50.0),
            evidence=[
                _evidence(
                    "trust_revocation",
                    {
                        "reason_code": "security_incident",
                        "revoked_at": (
                            _execution()["reported_at"] - timedelta(minutes=1)
                        ).isoformat(),
                    },
                ),
                _evidence(
                    "trust_revocation",
                    {
                        "reason_code": "capability_failure",
                        "revoked_at": (
                            _execution()["reported_at"] + timedelta(minutes=1)
                        ).isoformat(),
                    },
                ),
            ],
            workflow_steps=_workflow_steps(
                min_score=80.0,
                min_tier=3,
                sensitivity_tier=3,
                fallback_step_number=None,
            ),
        ),
    ],
)
def test_weights_sum_to_one_across_factor_combinations(result):
    """Attribution weights should always normalize to 1.0."""
    assert round(sum(result.weights.values()), 4) == 1.0


def test_actor_weights_never_go_below_zero_with_many_factors():
    """The per-actor floor should prevent negative attribution weights."""
    result = _compute(
        claim=_claim("service_failure"),
        snapshot=_snapshot(
            trust_score=40.0,
            trust_tier=1,
            quality_score=40.0,
            checklist={"context_minimal": True, "trust_thresholds_appropriate": True},
        ),
        evidence=[
            _evidence("context_mismatch", {"severity": "critical"}),
            _evidence("service_capability", {"is_verified": False}),
            _evidence(
                "trust_revocation",
                {
                    "reason_code": "security_incident",
                    "revoked_at": (
                        _execution()["reported_at"] - timedelta(minutes=1)
                    ).isoformat(),
                },
            ),
            _evidence(
                "trust_revocation",
                {
                    "reason_code": "capability_failure",
                    "revoked_at": (
                        _execution()["reported_at"] + timedelta(minutes=1)
                    ).isoformat(),
                },
            ),
        ],
        workflow_steps=_workflow_steps(
            min_score=90.0,
            min_tier=4,
            sensitivity_tier=4,
            fallback_step_number=None,
        ),
    )

    assert min(result.weights.values()) >= 0.0
    assert round(sum(result.weights.values()), 4) == 1.0


def test_determine_claim_persists_determination_and_marks_claim_determined():
    """determine_claim should create a determination and transition claim status."""
    db = _DetermineSession()
    redis = _FakeRedis()

    response = asyncio.run(
        liability_attribution.determine_claim(
            claim_id=db.claim_id,
            reviewer_did=None,
            db=db,
            redis=redis,
        )
    )

    assert response.determination_id == db.determination_id
    assert response.attribution == {
        "agent": 0.35,
        "service": 0.35,
        "workflow_author": 0.15,
        "validator": 0.15,
    }
    assert db.claim_status == "determined"
    assert db.commit_count == 1
    update_sql = next(
        sql for sql, _ in db.executed if "UPDATE liability_claims" in sql
    )
    assert "status = 'determined'" in update_sql
    cache_key = liability_claims.claim_status_cache_key(db.claim_id)
    assert cache_key in redis.deleted
    assert redis.store[cache_key] == "determined"
    assert redis.ttls[cache_key] == 60


def test_determine_claim_rejects_claim_not_ready_for_determination():
    """Claims must be evidence_gathered or under_review before determination."""
    db = _DetermineSession(claim_status="filed")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            liability_attribution.determine_claim(
                claim_id=db.claim_id,
                reviewer_did=None,
                db=db,
            )
        )

    assert exc_info.value.status_code == 409
    assert db.rollback_count == 1
