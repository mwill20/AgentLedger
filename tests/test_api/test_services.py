"""Tests for structured service queries."""

from __future__ import annotations

from uuid import uuid4

from api.models.service import MatchedCapability, ServiceDetail, ServiceSearchResponse, ServiceSummary
from api.routers import services as services_router


def test_get_services_returns_ranked_results(client, api_key_headers, monkeypatch):
    """GET /v1/services should return the structured query payload."""
    service_id = uuid4()

    async def fake_query_services(**kwargs):
        return ServiceSearchResponse(
            total=1,
            limit=10,
            offset=0,
            results=[
                ServiceSummary(
                    service_id=service_id,
                    name="SkyBridge Travel",
                    domain="skybridge.example",
                    trust_tier=1,
                    trust_score=10.0,
                    rank_score=0.91,
                    pricing_model="freemium",
                    is_active=True,
                    matched_capabilities=[
                        MatchedCapability(
                            ontology_tag="travel.air.book",
                            description="Book flights quickly with traveler and payment context.",
                            match_score=1.0,
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(services_router.registry, "query_services", fake_query_services)

    response = client.get(
        "/v1/services",
        params={"ontology": "travel.air.book"},
        headers=api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["results"][0]["domain"] == "skybridge.example"
    assert body["results"][0]["matched_capabilities"][0]["ontology_tag"] == "travel.air.book"


def test_get_service_detail_returns_full_record(client, api_key_headers, monkeypatch):
    """GET /v1/services/{service_id} should return the service detail document."""
    service_id = uuid4()

    async def fake_get_service_detail(db, service_id):
        return ServiceDetail(
            service_id=service_id,
            name="SkyBridge Travel",
            domain="skybridge.example",
            manifest_url="https://skybridge.example/.well-known/agent-manifest.json",
            trust_tier=1,
            trust_score=10.0,
            is_active=True,
            is_banned=False,
            current_manifest={"name": "SkyBridge Travel"},
            capabilities=[],
        )

    monkeypatch.setattr(services_router.registry, "get_service_detail", fake_get_service_detail)

    response = client.get(f"/v1/services/{service_id}", headers=api_key_headers)

    assert response.status_code == 200
    assert response.json()["service_id"] == str(service_id)
