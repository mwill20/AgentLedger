"""Tests for semantic search endpoints."""

from __future__ import annotations

from uuid import uuid4

from api.models.service import MatchedCapability, ServiceSearchResponse, ServiceSummary
from api.routers import search as search_router


def test_post_search_returns_semantic_matches(client, api_key_headers, monkeypatch):
    """POST /v1/search should return ranked semantic results."""
    service_id = uuid4()

    async def fake_search_services(db, request, **kwargs):
        return ServiceSearchResponse(
            total=1,
            limit=request.limit,
            offset=request.offset,
            results=[
                ServiceSummary(
                    service_id=service_id,
                    name="SkyBridge Travel",
                    domain="skybridge.example",
                    trust_tier=1,
                    trust_score=10.0,
                    rank_score=0.88,
                    pricing_model="freemium",
                    is_active=True,
                    matched_capabilities=[
                        MatchedCapability(
                            ontology_tag="travel.air.book",
                            description="Book flights to major cities with instant confirmation.",
                            match_score=0.84,
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(search_router.registry, "search_services", fake_search_services)

    response = client.post(
        "/v1/search",
        json={"query": "book a flight to New York", "limit": 10},
        headers=api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["results"][0]["matched_capabilities"][0]["match_score"] > 0
