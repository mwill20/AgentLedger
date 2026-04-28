"""Shared test fixtures."""

from __future__ import annotations

from uuid import uuid4

import os

import pytest
from fastapi.testclient import TestClient

# Ensure test API key is configured before importing app
os.environ.setdefault("API_KEYS", "test-api-key")
os.environ.setdefault("ADMIN_API_KEYS", "test-admin-key")

from api.dependencies import get_db  # noqa: E402
from api.main import app  # noqa: E402


class DummySession:
    """Minimal session object used by router tests that monkeypatch service calls."""


@pytest.fixture
def api_key_headers() -> dict[str, str]:
    """Default auth header for protected endpoints."""
    return {"X-API-Key": "test-api-key"}


@pytest.fixture
def admin_api_key_headers() -> dict[str, str]:
    """Admin auth header for revocation endpoints."""
    return {"X-API-Key": "test-admin-key"}


@pytest.fixture
def sample_manifest_payload() -> dict:
    """Valid manifest payload used across tests."""
    return {
        "manifest_version": "1.0",
        "service_id": str(uuid4()),
        "name": "SkyBridge Travel",
        "domain": "skybridge.example",
        "public_key": "test-public-key",
        "capabilities": [
            {
                "id": "book-flight",
                "ontology_tag": "travel.air.book",
                "description": "Book flights for travelers with payment, seat selection, and refunds.",
            }
        ],
        "pricing": {"model": "freemium", "tiers": [], "billing_method": "api_key"},
        "context": {
            "required": [{"name": "traveler_name", "type": "string"}],
            "optional": [{"name": "loyalty_number", "type": "string"}],
            "data_retention_days": 30,
            "data_sharing": "none",
        },
        "operations": {
            "uptime_sla_percent": 99.9,
            "rate_limits": {"rpm": 120, "rpd": 10000},
            "sandbox_url": "https://sandbox.skybridge.example",
        },
        "legal_entity": "SkyBridge Travel LLC",
        "last_updated": "2026-04-11T20:30:00Z",
    }


@pytest.fixture
def sample_health_context_profile_payload() -> dict:
    """Default HEALTH context profile used by Layer 4 tests."""
    return {
        "agent_did": "did:key:z6MkContextHealthAgent",
        "profile_name": "default",
        "default_policy": "deny",
        "rules": [
            {
                "priority": 20,
                "scope_type": "trust_tier",
                "scope_value": "4",
                "permitted_fields": ["user.name", "user.email", "user.dob"],
                "denied_fields": [],
                "action": "permit",
            },
            {
                "priority": 10,
                "scope_type": "domain",
                "scope_value": "HEALTH",
                "permitted_fields": ["user.name", "user.insurance_id"],
                "denied_fields": ["user.ssn", "user.full_medical_history"],
                "action": "permit",
            },
        ],
    }


@pytest.fixture
def sample_finance_context_profile_payload() -> dict:
    """Default FINANCE context profile used by Layer 4 tests."""
    return {
        "agent_did": "did:key:z6MkContextFinanceAgent",
        "profile_name": "default",
        "default_policy": "deny",
        "rules": [
            {
                "priority": 10,
                "scope_type": "domain",
                "scope_value": "FINANCE",
                "permitted_fields": ["user.name", "user.email"],
                "denied_fields": ["user.ssn"],
                "action": "permit",
            }
        ],
    }


@pytest.fixture
def default_context_profile_payloads(
    sample_health_context_profile_payload: dict,
    sample_finance_context_profile_payload: dict,
) -> list[dict]:
    """Two seeded default Layer 4 profile payloads for tests."""
    return [
        sample_health_context_profile_payload,
        sample_finance_context_profile_payload,
    ]


@pytest.fixture
def client() -> TestClient:
    """FastAPI test client with DB dependency overridden."""

    async def override_get_db():
        yield DummySession()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
