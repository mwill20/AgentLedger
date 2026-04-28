"""Tests for Layer 4 context profile endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from api.models.context import (
    ContextProfileCreateResponse,
    ContextProfileRecord,
    ContextProfileRuleRecord,
)
from api.routers import context as context_router
from api.services.context_profiles import _build_profile_record


def _rule(priority: int, scope_type: str, scope_value: str) -> ContextProfileRuleRecord:
    """Build one stored rule record for route tests."""
    return ContextProfileRuleRecord(
        rule_id=uuid4(),
        priority=priority,
        scope_type=scope_type,
        scope_value=scope_value,
        permitted_fields=["user.name"],
        denied_fields=[],
        action="permit",
        created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )


def test_post_context_profiles_returns_created(
    client,
    api_key_headers,
    sample_health_context_profile_payload,
    monkeypatch,
):
    """POST /v1/context/profiles should create a profile with rules."""

    async def fake_create_profile(db, request):
        assert request.agent_did == sample_health_context_profile_payload["agent_did"]
        assert any(rule.scope_value == "HEALTH" for rule in request.rules)
        return ContextProfileCreateResponse(
            profile_id=uuid4(),
            agent_did=request.agent_did,
            profile_name=request.profile_name,
            default_policy=request.default_policy,
            rule_count=len(request.rules),
            created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(
        context_router.context_profiles,
        "create_profile",
        fake_create_profile,
    )

    response = client.post(
        "/v1/context/profiles",
        json=sample_health_context_profile_payload,
        headers=api_key_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["agent_did"] == sample_health_context_profile_payload["agent_did"]
    assert body["profile_name"] == "default"
    assert body["default_policy"] == "deny"
    assert body["rule_count"] == 2


def test_get_context_profiles_returns_rules_sorted(
    client,
    api_key_headers,
    sample_health_context_profile_payload,
    monkeypatch,
):
    """GET /v1/context/profiles/{agent_did} should return rules sorted by priority."""
    agent_did = sample_health_context_profile_payload["agent_did"]

    async def fake_get_active_profile(db, agent_did, redis=None):
        return ContextProfileRecord(
            profile_id=uuid4(),
            agent_did=agent_did,
            profile_name="default",
            is_active=True,
            default_policy="deny",
            rules=[
                _rule(10, "domain", "HEALTH"),
                _rule(20, "trust_tier", "4"),
            ],
            created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(
        context_router.context_profiles,
        "get_active_profile",
        fake_get_active_profile,
    )

    response = client.get(
        f"/v1/context/profiles/{agent_did}",
        headers=api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_did"] == agent_did
    assert [rule["priority"] for rule in body["rules"]] == [10, 20]


def test_put_context_profiles_replaces_rules(
    client,
    api_key_headers,
    sample_health_context_profile_payload,
    monkeypatch,
):
    """PUT /v1/context/profiles/{agent_did} should update an active profile."""
    agent_did = sample_health_context_profile_payload["agent_did"]
    update_payload = {
        "profile_name": "default",
        "default_policy": "allow",
        "rules": sample_health_context_profile_payload["rules"][:1],
    }

    async def fake_update_active_profile(db, agent_did, request, redis=None):
        return ContextProfileRecord(
            profile_id=uuid4(),
            agent_did=agent_did,
            profile_name=request.profile_name,
            is_active=True,
            default_policy=request.default_policy,
            rules=[_rule(20, "trust_tier", "4")],
            created_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(
        context_router.context_profiles,
        "update_active_profile",
        fake_update_active_profile,
    )

    response = client.put(
        f"/v1/context/profiles/{agent_did}",
        json=update_payload,
        headers=api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["default_policy"] == "allow"


def test_context_profile_rejects_invalid_field_name(
    client,
    api_key_headers,
    sample_health_context_profile_payload,
):
    """Profile validation should reject malformed context field names."""
    sample_health_context_profile_payload["rules"][0]["permitted_fields"] = ["user name"]

    response = client.post(
        "/v1/context/profiles",
        json=sample_health_context_profile_payload,
        headers=api_key_headers,
    )

    assert response.status_code == 422


def test_context_profile_fixtures_seed_two_defaults(default_context_profile_payloads):
    """Layer 4 test fixtures should seed two default profile payloads."""
    assert len(default_context_profile_payloads) == 2
    assert {item["rules"][0]["scope_value"] for item in default_context_profile_payloads} == {
        "4",
        "FINANCE",
    }


def test_build_profile_record_sorts_rules_by_priority():
    """Service row mapping should sort persisted rules by priority."""
    profile_id = uuid4()
    now = datetime(2026, 4, 27, tzinfo=timezone.utc)
    profile_row = {
        "id": profile_id,
        "agent_did": "did:key:z6MkContextHealthAgent",
        "profile_name": "default",
        "is_active": True,
        "default_policy": "deny",
        "created_at": now,
        "updated_at": now,
    }
    rule_rows = [
        {
            "id": uuid4(),
            "priority": 20,
            "scope_type": "trust_tier",
            "scope_value": "4",
            "permitted_fields": ["user.email"],
            "denied_fields": [],
            "action": "permit",
            "created_at": now,
        },
        {
            "id": uuid4(),
            "priority": 10,
            "scope_type": "domain",
            "scope_value": "HEALTH",
            "permitted_fields": ["user.name"],
            "denied_fields": ["user.ssn"],
            "action": "permit",
            "created_at": now,
        },
    ]

    profile = _build_profile_record(profile_row, rule_rows)

    assert [rule.priority for rule in profile.rules] == [10, 20]
