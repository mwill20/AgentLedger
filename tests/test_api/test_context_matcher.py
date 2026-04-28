"""Tests for Layer 4 context matching engine."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.context import ContextMatchRequest, ContextProfileRecord, ContextProfileRuleRecord
from api.services import context_matcher


class _FakeMappings:
    """Minimal mappings wrapper for context matcher tests."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    """Minimal result wrapper for mappings."""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _InspectableSession:
    """Async DB double that records calls and transaction state."""

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
    """Small async Redis double for match cache checks."""

    def __init__(self):
        self.calls = []

    async def set(self, key, value, ex=None):
        self.calls.append((key, value, ex))


def _request(service_id) -> ContextMatchRequest:
    """Build one valid context match request."""
    return ContextMatchRequest(
        agent_did="did:key:z6MkContextHealthAgent",
        service_id=service_id,
        session_assertion="header.payload.signature.with-enough-length",
        requested_fields=["user.name", "user.email", "user.insurance_id"],
    )


def _profile() -> ContextProfileRecord:
    """Build a profile with domain and trust-tier rules."""
    now = datetime(2026, 4, 27, tzinfo=timezone.utc)
    return ContextProfileRecord(
        profile_id=uuid4(),
        agent_did="did:key:z6MkContextHealthAgent",
        profile_name="default",
        is_active=True,
        default_policy="deny",
        rules=[
            ContextProfileRuleRecord(
                rule_id=uuid4(),
                priority=10,
                scope_type="domain",
                scope_value="HEALTH",
                permitted_fields=["user.name", "user.insurance_id"],
                denied_fields=["user.ssn"],
                action="permit",
                created_at=now,
            ),
            ContextProfileRuleRecord(
                rule_id=uuid4(),
                priority=20,
                scope_type="trust_tier",
                scope_value="3",
                permitted_fields=["user.email"],
                denied_fields=[],
                action="permit",
                created_at=now,
            ),
        ],
        created_at=now,
        updated_at=now,
    )


def test_match_context_returns_permit_withhold_commit_classification(monkeypatch):
    """A valid match should classify low-risk and sensitive fields correctly."""
    service_id = uuid4()
    session_id = uuid4()
    commitment_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"id": session_id, "ontology_tag": "health.pharmacy.order"}],
            [
                {
                    "id": service_id,
                    "domain": "pharmacy.example",
                    "ontology_tag": "health.pharmacy.order",
                    "trust_tier": 3,
                    "trust_score": 82.4,
                    "ontology_domain": "HEALTH",
                }
            ],
            [
                {"field_name": "user.name", "is_required": True, "sensitivity": "low"},
                {"field_name": "user.email", "is_required": True, "sensitivity": "low"},
                {
                    "field_name": "user.insurance_id",
                    "is_required": False,
                    "sensitivity": "high",
                },
            ],
        ]
    )
    redis = _FakeRedis()

    monkeypatch.setattr(
        context_matcher.credentials,
        "verify_session_assertion",
        lambda token: {
            "jti": "session-jti",
            "sub": "did:key:z6MkContextHealthAgent",
            "service_id": str(service_id),
            "ontology_tag": "health.pharmacy.order",
        },
    )

    async def fake_get_active_profile(db, agent_did, redis=None):
        return _profile()

    async def fake_create_commitments(**kwargs):
        assert kwargs["field_names"] == ["user.insurance_id"]
        return [commitment_id]

    monkeypatch.setattr(
        context_matcher.context_profiles,
        "get_active_profile",
        fake_get_active_profile,
    )
    monkeypatch.setattr(
        context_matcher.context_disclosure,
        "create_commitments",
        fake_create_commitments,
    )

    response = asyncio.run(
        context_matcher.match_context_request(
            db=db,
            request=_request(service_id),
            redis=redis,
        )
    )

    assert response.permitted_fields == ["user.name", "user.email"]
    assert response.withheld_fields == []
    assert response.committed_fields == ["user.insurance_id"]
    assert response.commitment_ids == [commitment_id]
    assert response.trust_tier_at_match == 3
    assert response.trust_score_at_match == 82.4
    assert response.can_disclose is True
    assert redis.calls[0][0] == f"context:match:{response.match_id}"
    assert redis.calls[0][2] == context_matcher.MATCH_TTL_SECONDS
    assert db.commit_count == 1


def test_match_context_blocks_insufficient_trust(monkeypatch):
    """Service trust tier must satisfy requested field sensitivity."""
    service_id = uuid4()
    session_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"id": session_id, "ontology_tag": "health.pharmacy.order"}],
            [
                {
                    "id": service_id,
                    "domain": "pharmacy.example",
                    "ontology_tag": "health.pharmacy.order",
                    "trust_tier": 2,
                    "trust_score": 55.0,
                    "ontology_domain": "HEALTH",
                }
            ],
            [
                {"field_name": "user.name", "is_required": True, "sensitivity": "low"},
                {
                    "field_name": "user.insurance_id",
                    "is_required": True,
                    "sensitivity": "high",
                },
                {"field_name": "user.email", "is_required": True, "sensitivity": "low"},
            ],
        ]
    )
    monkeypatch.setattr(
        context_matcher.credentials,
        "verify_session_assertion",
        lambda token: {
            "jti": "session-jti",
            "sub": "did:key:z6MkContextHealthAgent",
            "service_id": str(service_id),
            "ontology_tag": "health.pharmacy.order",
        },
    )

    try:
        asyncio.run(context_matcher.match_context_request(db=db, request=_request(service_id)))
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected trust threshold failure")

    assert response.status_code == 403
    assert response.detail["trust_threshold_failed"] is True
    assert response.detail["fields"][0]["field"] == "user.insurance_id"


def test_match_context_withholds_optional_field_on_insufficient_trust(monkeypatch):
    """Optional sensitive fields are withheld instead of blocking the match."""
    service_id = uuid4()
    session_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"id": session_id, "ontology_tag": "health.pharmacy.order"}],
            [
                {
                    "id": service_id,
                    "domain": "pharmacy.example",
                    "ontology_tag": "health.pharmacy.order",
                    "trust_tier": 2,
                    "trust_score": 55.0,
                    "ontology_domain": "HEALTH",
                }
            ],
            [
                {"field_name": "user.name", "is_required": True, "sensitivity": "low"},
                {
                    "field_name": "user.insurance_id",
                    "is_required": False,
                    "sensitivity": "high",
                },
            ],
        ]
    )
    request = ContextMatchRequest(
        agent_did="did:key:z6MkContextHealthAgent",
        service_id=service_id,
        session_assertion="header.payload.signature.with-enough-length",
        requested_fields=["user.insurance_id"],
    )

    monkeypatch.setattr(
        context_matcher.credentials,
        "verify_session_assertion",
        lambda token: {
            "jti": "session-jti",
            "sub": "did:key:z6MkContextHealthAgent",
            "service_id": str(service_id),
            "ontology_tag": "health.pharmacy.order",
        },
    )

    async def fake_get_active_profile(db, agent_did, redis=None):
        return _profile()

    async def fake_create_commitments(**kwargs):
        assert kwargs["field_names"] == []
        return []

    monkeypatch.setattr(
        context_matcher.context_profiles,
        "get_active_profile",
        fake_get_active_profile,
    )
    monkeypatch.setattr(
        context_matcher.context_disclosure,
        "create_commitments",
        fake_create_commitments,
    )

    response = asyncio.run(
        context_matcher.match_context_request(
            db=db,
            request=request,
        )
    )

    assert response.permitted_fields == []
    assert response.withheld_fields == ["user.insurance_id"]
    assert response.committed_fields == []
    assert response.commitment_ids == []
    assert response.can_disclose is False
    assert db.commit_count == 1
