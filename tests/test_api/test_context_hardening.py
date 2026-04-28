"""Tests for Layer 4 hardening behavior."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.context import (
    ContextMatchRequest,
    ContextProfileCreateRequest,
    ContextProfileRuleInput,
    ContextProfileRuleRecord,
    ContextProfileUpdateRequest,
    DisclosureRequest,
)
from api.services import context_disclosure, context_matcher, context_profiles, trust


AGENT_DID = "did:key:z6MkContextHealthAgent"


class _FakeMappings:
    """Minimal mappings wrapper for hardening tests."""

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


class _FakeRedis:
    """Async Redis double with get/set/delete and rate-limit primitives."""

    def __init__(self):
        self.store = {}
        self.get_calls = []
        self.set_calls = []
        self.delete_calls = []
        self.incr_values = []
        self.expire_calls = []

    async def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))
        return True

    async def delete(self, key):
        self.store.pop(key, None)
        self.delete_calls.append(key)
        return 1

    async def incr(self, key):
        if self.incr_values:
            return self.incr_values.pop(0)
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = value
        return value

    async def expire(self, key, seconds):
        self.expire_calls.append((key, seconds))
        return True

    async def ttl(self, key):
        return 42


def _profile_rows():
    """Build stored profile and rule rows."""
    profile_id = uuid4()
    now = datetime(2026, 4, 27, tzinfo=timezone.utc)
    profile_row = {
        "id": profile_id,
        "agent_did": AGENT_DID,
        "profile_name": "default",
        "is_active": True,
        "default_policy": "deny",
        "created_at": now,
        "updated_at": now,
    }
    rule_row = {
        "id": uuid4(),
        "priority": 10,
        "scope_type": "domain",
        "scope_value": "HEALTH",
        "permitted_fields": ["user.name"],
        "denied_fields": ["user.ssn"],
        "action": "permit",
        "created_at": now,
    }
    return profile_row, rule_row


def _commitment_row(match_id, commitment_id, service_id):
    """Build one unexpired commitment row for disclosure hardening tests."""
    return {
        "id": commitment_id,
        "match_id": match_id,
        "agent_did": AGENT_DID,
        "service_id": service_id,
        "session_assertion_id": None,
        "field_name": "user.insurance_id",
        "nonce": "nonce-for-insurance",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=4),
        "fields_requested": ["user.name", "user.insurance_id"],
        "fields_permitted": ["user.name"],
        "fields_withheld": [],
        "fields_committed": ["user.insurance_id"],
    }


def _domain_rule() -> ContextProfileRuleInput:
    """Build one domain-scoped profile rule."""
    return ContextProfileRuleInput(
        priority=10,
        scope_type="domain",
        scope_value="HEALTH",
        permitted_fields=["user.name"],
        denied_fields=["user.ssn"],
        action="permit",
    )


def _jwt(claims: dict) -> str:
    """Build a JWT-shaped token for Phase 3 fallback verification tests."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def test_get_active_profile_uses_redis_cache_with_60_second_ttl():
    """Active profiles should be cached and served from Redis on later reads."""
    profile_row, rule_row = _profile_rows()
    db = _InspectableSession(rows=[[profile_row], [rule_row]])
    redis = _FakeRedis()

    first = asyncio.run(
        context_profiles.get_active_profile(
            db=db,
            agent_did=AGENT_DID,
            redis=redis,
        )
    )
    cached_db = _InspectableSession(rows=[])
    second = asyncio.run(
        context_profiles.get_active_profile(
            db=cached_db,
            agent_did=AGENT_DID,
            redis=redis,
        )
    )

    assert first.profile_id == second.profile_id
    assert len(db.executed) == 2
    assert cached_db.executed == []
    assert redis.set_calls[0][0] == context_profiles._profile_cache_key(AGENT_DID)
    assert redis.set_calls[0][2] == context_profiles.PROFILE_CACHE_TTL_SECONDS


def test_profile_cache_helpers_fail_open_on_invalid_values_and_redis_errors():
    """Profile cache helpers should never make profile reads or writes fail."""
    profile_row, rule_row = _profile_rows()
    profile = context_profiles._build_profile_record(profile_row, [rule_row])

    class FailingRedis:
        async def get(self, key):  # pragma: no cover - exercised through helper
            raise RuntimeError("redis unavailable")

        async def set(self, key, value, ex=None):  # pragma: no cover
            raise RuntimeError("redis unavailable")

        async def delete(self, key):  # pragma: no cover
            raise RuntimeError("redis unavailable")

    redis = _FakeRedis()
    redis.store[context_profiles._profile_cache_key(AGENT_DID)] = "not-json"

    assert asyncio.run(context_profiles._cache_get_profile(None, AGENT_DID)) is None
    assert asyncio.run(context_profiles._cache_get_profile(FailingRedis(), AGENT_DID)) is None
    assert asyncio.run(context_profiles._cache_get_profile(redis, AGENT_DID)) is None
    asyncio.run(context_profiles._cache_set_profile(FailingRedis(), profile))
    asyncio.run(context_profiles._cache_invalidate_profile(FailingRedis(), AGENT_DID))


def test_create_profile_validates_agent_domain_and_inserts_rules():
    """Profile creation should require active agents, known domains, and rule writes."""
    profile_id = uuid4()
    created_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    db = _InspectableSession(
        rows=[
            [{"did": AGENT_DID}],
            [{"domain": "HEALTH"}],
            [{"id": profile_id, "created_at": created_at}],
            [],
        ]
    )

    response = asyncio.run(
        context_profiles.create_profile(
            db=db,
            request=ContextProfileCreateRequest(
                agent_did=AGENT_DID,
                profile_name="health-default",
                default_policy="deny",
                rules=[_domain_rule()],
            ),
        )
    )

    assert response.profile_id == profile_id
    assert response.agent_did == AGENT_DID
    assert response.rule_count == 1
    assert db.commit_count == 1
    assert db.rollback_count == 0
    assert any("INSERT INTO context_profile_rules" in sql for sql, _ in db.executed)


def test_create_profile_rejects_unknown_domain_scope():
    """Domain-scoped rules should only reference domains known to the ontology."""
    db = _InspectableSession(rows=[[{"did": AGENT_DID}], []])

    try:
        asyncio.run(
            context_profiles.create_profile(
                db=db,
                request=ContextProfileCreateRequest(
                    agent_did=AGENT_DID,
                    profile_name="health-default",
                    rules=[_domain_rule()],
                ),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected unknown domain rejection")

    assert response.status_code == 422
    assert "unknown ontology domains" in response.detail
    assert db.rollback_count == 1


def test_get_active_profile_missing_profile_returns_404():
    """Missing active profiles should return a route-friendly 404."""
    db = _InspectableSession(rows=[[]])

    try:
        asyncio.run(context_profiles.get_active_profile(db, AGENT_DID))
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected missing profile")

    assert response.status_code == 404
    assert response.detail == "active context profile not found"


def test_update_active_profile_invalidates_profile_cache():
    """PUT-style profile updates should invalidate cached active profiles."""
    profile_row, _rule_row = _profile_rows()
    profile_row = {**profile_row, "default_policy": "allow"}
    redis = _FakeRedis()
    cache_key = context_profiles._profile_cache_key(AGENT_DID)
    redis.store[cache_key] = "cached-profile"
    db = _InspectableSession(rows=[[profile_row], [], []])

    response = asyncio.run(
        context_profiles.update_active_profile(
            db=db,
            agent_did=AGENT_DID,
            request=ContextProfileUpdateRequest(
                profile_name="default",
                default_policy="allow",
                rules=[],
            ),
            redis=redis,
        )
    )

    assert response.default_policy == "allow"
    assert redis.delete_calls == [cache_key]
    assert cache_key not in redis.store
    assert db.commit_count == 1


def test_update_active_profile_missing_profile_rolls_back():
    """PUT-style updates should roll back when no active profile exists."""
    db = _InspectableSession(rows=[[]])

    try:
        asyncio.run(
            context_profiles.update_active_profile(
                db=db,
                agent_did=AGENT_DID,
                request=ContextProfileUpdateRequest(
                    profile_name="default",
                    default_policy="allow",
                    rules=[],
                ),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected missing profile")

    assert response.status_code == 404
    assert db.rollback_count == 1


def test_match_rate_limit_allows_first_request_and_sets_window():
    """The first match request in a window should set the Redis TTL."""
    redis = _FakeRedis()

    asyncio.run(context_matcher._enforce_match_rate_limit(redis, AGENT_DID))

    key = f"context:match:rate:{AGENT_DID}"
    assert redis.store[key] == 1
    assert redis.expire_calls == [
        (key, context_matcher.MATCH_RATE_LIMIT_WINDOW_SECONDS)
    ]


def test_match_rate_limit_blocks_after_100_requests_per_minute():
    """The match limiter should return 429 after the per-agent minute quota."""
    redis = _FakeRedis()
    redis.incr_values = [101]

    try:
        asyncio.run(context_matcher._enforce_match_rate_limit(redis, AGENT_DID))
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected match rate limit failure")

    assert response.status_code == 429
    assert response.detail["rate_limited"] is True
    assert response.detail["limit"] == context_matcher.MATCH_RATE_LIMIT_PER_MINUTE
    assert response.detail["retry_after_seconds"] == 42


def test_session_assertion_fallback_accepts_well_formed_jwt_without_jti(monkeypatch):
    """Phase 3 fallback should accept JWT-shaped assertions without a DB lookup."""
    service_id = uuid4()
    request = ContextMatchRequest(
        agent_did=AGENT_DID,
        service_id=service_id,
        session_assertion=_jwt(
            {
                "sub": AGENT_DID,
                "service_id": str(service_id),
                "ontology_tag": "health.pharmacy.order",
            }
        ),
        requested_fields=["user.name"],
    )
    db = _InspectableSession(rows=[])
    monkeypatch.setattr(
        context_matcher.credentials,
        "verify_session_assertion",
        lambda token: (_ for _ in ()).throw(ValueError("unsigned token")),
    )

    assertion_id, ontology_tag = asyncio.run(
        context_matcher._verify_session_assertion(db, request)
    )

    assert assertion_id is None
    assert ontology_tag == "health.pharmacy.order"
    assert db.executed == []


def test_session_assertion_rejects_subject_mismatch(monkeypatch):
    """Session assertions must bind to the requesting agent DID."""
    service_id = uuid4()
    request = ContextMatchRequest(
        agent_did=AGENT_DID,
        service_id=service_id,
        session_assertion="header.payload.signature.with-enough-length",
        requested_fields=["user.name"],
    )
    monkeypatch.setattr(
        context_matcher.credentials,
        "verify_session_assertion",
        lambda token: {"sub": "did:key:z6MkDifferentAgent"},
    )

    try:
        asyncio.run(context_matcher._verify_session_assertion(_InspectableSession([]), request))
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected subject mismatch")

    assert response.status_code == 403
    assert response.detail == "session assertion subject does not match agent_did"


def test_decode_unverified_session_assertion_rejects_malformed_tokens():
    """The fallback verifier should still require a JWT-shaped token."""
    try:
        context_matcher._decode_unverified_session_assertion("not-a-jwt")
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected malformed token rejection")

    assert response.status_code == 403


def test_rule_matching_and_default_allow_commit_sensitive_fields():
    """Rule scopes and default allow policy should classify fields per spec."""
    now = datetime(2026, 4, 27, tzinfo=timezone.utc)
    service = context_matcher.ServiceContext(
        service_id=uuid4(),
        domain="pharmacy.example",
        did="did:web:pharmacy.example",
        ontology_tag="health.pharmacy.order",
        ontology_domain="HEALTH",
        trust_tier=4,
        trust_score=92.0,
        declared_required_fields=["user.name"],
        declared_optional_fields=["user.insurance_id"],
        field_sensitivity_tiers={"user.name": 1, "user.insurance_id": 3},
    )
    service_rule = ContextProfileRuleRecord(
        rule_id=uuid4(),
        priority=10,
        scope_type="service_did",
        scope_value="did:web:pharmacy.example",
        permitted_fields=["user.name"],
        denied_fields=[],
        action="permit",
        created_at=now,
    )
    sensitivity_rule = ContextProfileRuleRecord(
        rule_id=uuid4(),
        priority=20,
        scope_type="sensitivity",
        scope_value="3",
        permitted_fields=[],
        denied_fields=[],
        action="permit",
        created_at=now,
    )
    unknown_rule = ContextProfileRuleRecord(
        rule_id=uuid4(),
        priority=30,
        scope_type="unsupported",
        scope_value="anything",
        permitted_fields=[],
        denied_fields=[],
        action="permit",
        created_at=now,
    )

    assert context_matcher.rule_matches_service(service_rule, service) is True
    assert context_matcher.rule_matches_service(sensitivity_rule, service) is True
    assert context_matcher.rule_matches_service(unknown_rule, service) is False
    assert (
        context_matcher.evaluate_profile(
            [unknown_rule],
            "user.insurance_id",
            service,
            default_policy="allow",
        )
        == "commit"
    )
    assert (
        context_matcher.evaluate_profile(
            [unknown_rule],
            "user.name",
            service,
            default_policy="allow",
        )
        == "permit"
    )


def test_disclose_uses_layer3_trust_cache_before_database_lookup():
    """Disclose-time trust checks should use the Layer 3 Redis trust snapshot."""
    match_id = uuid4()
    service_id = uuid4()
    commitment_id = uuid4()
    disclosure_id = uuid4()
    created_at = datetime.now(timezone.utc)
    redis = _FakeRedis()
    redis.store[trust.service_trust_cache_key(str(service_id))] = json.dumps(
        {
            "ontology_tag": "health.pharmacy.order",
            "trust_tier": 3,
            "trust_score": 87.5,
        }
    )
    db = _InspectableSession(
        rows=[
            [_commitment_row(match_id, commitment_id, service_id)],
            [{"field_name": "user.insurance_id", "sensitivity": "high"}],
            [{"field_name": "user.insurance_id", "nonce": "nonce-for-insurance"}],
            [{"id": disclosure_id, "created_at": created_at}],
        ]
    )

    response = asyncio.run(
        context_disclosure.disclose_context(
            db=db,
            request=DisclosureRequest(
                match_id=match_id,
                agent_did=AGENT_DID,
                service_id=service_id,
                commitment_ids=[commitment_id],
                field_values={"user.name": "Michael Williams"},
            ),
            redis=redis,
        )
    )

    assert response.committed_field_nonces == {
        "user.insurance_id": "nonce-for-insurance"
    }
    assert trust.service_trust_cache_key(str(service_id)) in redis.get_calls
    assert not any("FROM services s" in sql for sql, _ in db.executed)
    assert db.executed[-1][1]["trust_score_at_disclosure"] == 87.5
