"""Shared test fixtures."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.dependencies import get_db
from api.main import app


class DummySession:
    """Minimal session object used by router tests that monkeypatch service calls."""


@pytest.fixture
def api_key_headers() -> dict[str, str]:
    """Default auth header for protected endpoints."""
    return {"X-API-Key": "dev-api-key"}


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
def client() -> TestClient:
    """FastAPI test client with DB dependency overridden."""

    async def override_get_db():
        yield DummySession()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
