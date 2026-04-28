"""Tests for Layer 5 workflow ranking engine."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from api.models.workflow import RankedStep, ServiceCandidate, WorkflowRankResponse
from api.routers import workflows as workflows_router
from api.services import workflow_ranker


class _FakeMappings:
    """Minimal mappings wrapper for workflow ranker tests."""

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

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        return _FakeResult(self._rows.pop(0) if self._rows else [])


class _FilteringRankSession:
    """DB double that filters candidate rows using query params."""

    def __init__(self, workflow_id, steps, candidates_by_tag):
        self.workflow_id = workflow_id
        self.steps = steps
        self.candidates_by_tag = candidates_by_tag
        self.executed = []

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        params = params or {}
        self.executed.append((sql_text, params))
        if "FROM workflows" in sql_text and "status = 'published'" in sql_text:
            return _FakeResult([{"id": self.workflow_id}])
        if "FROM workflow_steps" in sql_text and "SELECT" in sql_text:
            return _FakeResult(self.steps)
        if "FROM service_capabilities" in sql_text:
            rows = []
            for row in self.candidates_by_tag.get(params["ontology_tag"], []):
                if row["trust_tier"] < params["min_trust_tier"]:
                    continue
                if row["trust_score"] < params["min_trust_score"]:
                    continue
                if (
                    "pricing_model" in params
                    and row.get("pricing_model") != params["pricing_model"]
                ):
                    continue
                if "geo" in params:
                    restrictions = row.get("geo_restrictions")
                    if restrictions and params["geo"] not in restrictions:
                        continue
                rows.append(row)
            rows.sort(key=lambda row: (-row["trust_score"], str(row["service_id"])))
            return _FakeResult(rows[:10])
        return _FakeResult([])


class _FakeRedis:
    """Async Redis double for rank cache tests."""

    def __init__(self):
        self.store = {}
        self.get_calls = []
        self.set_calls = []

    async def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.set_calls.append((key, value, ex))
        return True


def _steps():
    """Build two workflow step rows."""
    return [
        {
            "step_number": 1,
            "ontology_tag": "travel.air.book",
            "is_required": True,
            "context_fields_required": ["user.name", "user.frequent_flyer_id"],
            "context_fields_optional": [],
            "min_trust_tier": 3,
            "min_trust_score": 75.0,
        },
        {
            "step_number": 2,
            "ontology_tag": "travel.lodging.book",
            "is_required": True,
            "context_fields_required": ["user.name", "user.email"],
            "context_fields_optional": [],
            "min_trust_tier": 2,
            "min_trust_score": 60.0,
        },
    ]


def test_rank_workflow_steps_filters_by_trust_and_sorts_candidates():
    """Ranked steps should exclude services below step trust floors."""
    workflow_id = uuid4()
    high_trust_flight = uuid4()
    low_tier_flight = uuid4()
    hotel = uuid4()
    db = _FilteringRankSession(
        workflow_id=workflow_id,
        steps=_steps(),
        candidates_by_tag={
            "travel.air.book": [
                {
                    "service_id": low_tier_flight,
                    "name": "LowTierFlights",
                    "domain": "lowtier.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 99.0,
                    "trust_tier": 2,
                    "pricing_model": "usage",
                },
                {
                    "service_id": high_trust_flight,
                    "name": "FlightBookerPro",
                    "domain": "flightbooker.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 91.2,
                    "trust_tier": 3,
                    "pricing_model": "usage",
                },
            ],
            "travel.lodging.book": [
                {
                    "service_id": hotel,
                    "name": "HotelBookerPro",
                    "domain": "hotelbooker.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 82.0,
                    "trust_tier": 2,
                    "pricing_model": "usage",
                }
            ],
        },
    )

    ranked = asyncio.run(
        workflow_ranker.rank_workflow_steps(
            workflow_id=workflow_id,
            geo=None,
            pricing_model=None,
            db=db,
        )
    )

    assert [step.step_number for step in ranked] == [1, 2]
    assert ranked[0].min_trust_tier == 3
    assert [candidate.service_id for candidate in ranked[0].candidates] == [
        high_trust_flight
    ]
    assert low_tier_flight not in {
        candidate.service_id for candidate in ranked[0].candidates
    }
    assert ranked[0].candidates[0].rank_score == 0.912
    assert ranked[0].candidates[0].can_disclose is True


def test_rank_workflow_steps_applies_optional_geo_and_pricing_filters():
    """Geo and pricing filters should narrow otherwise eligible candidates."""
    workflow_id = uuid4()
    pass_service = uuid4()
    wrong_geo_service = uuid4()
    wrong_price_service = uuid4()
    db = _FilteringRankSession(
        workflow_id=workflow_id,
        steps=_steps()[:1],
        candidates_by_tag={
            "travel.air.book": [
                {
                    "service_id": pass_service,
                    "name": "USUsageFlights",
                    "domain": "ususage.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 88.0,
                    "trust_tier": 3,
                    "pricing_model": "usage",
                    "geo_restrictions": ["US", "CA"],
                },
                {
                    "service_id": wrong_geo_service,
                    "name": "EUUsageFlights",
                    "domain": "euusage.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 95.0,
                    "trust_tier": 3,
                    "pricing_model": "usage",
                    "geo_restrictions": ["EU"],
                },
                {
                    "service_id": wrong_price_service,
                    "name": "USSubscriptionFlights",
                    "domain": "ussubscription.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 93.0,
                    "trust_tier": 3,
                    "pricing_model": "subscription",
                    "geo_restrictions": ["US"],
                },
            ]
        },
    )

    ranked = asyncio.run(
        workflow_ranker.rank_workflow_steps(
            workflow_id=workflow_id,
            geo="US",
            pricing_model="usage",
            db=db,
        )
    )

    assert [candidate.service_id for candidate in ranked[0].candidates] == [
        pass_service
    ]


def test_compute_workflow_quality_score_matches_spec_formula():
    """Quality score formula should return the manually verified 73.25 case."""
    workflow_id = uuid4()
    db = _InspectableSession(
        rows=[
            [{"status": "published", "execution_count": 50, "success_count": 45}],
            [{"verified_count": 30}],
            [{"trust_score": 85.0}],
        ]
    )

    score = asyncio.run(
        workflow_ranker.compute_workflow_quality_score(
            workflow_id=workflow_id,
            db=db,
            redis=None,
        )
    )

    assert score == 73.25


def test_get_workflow_rank_uses_redis_cache_on_second_call():
    """Second rank lookup for the same workflow should use the Redis cache."""
    workflow_id = uuid4()
    service_id = uuid4()
    db = _FilteringRankSession(
        workflow_id=workflow_id,
        steps=_steps()[:1],
        candidates_by_tag={
            "travel.air.book": [
                {
                    "service_id": service_id,
                    "name": "FlightBookerPro",
                    "domain": "flightbooker.example",
                    "ontology_domain": "TRAVEL",
                    "trust_score": 91.2,
                    "trust_tier": 3,
                    "pricing_model": "usage",
                }
            ]
        },
    )
    redis = _FakeRedis()

    first = asyncio.run(
        workflow_ranker.get_workflow_rank(
            workflow_id=workflow_id,
            geo=None,
            pricing_model=None,
            db=db,
            redis=redis,
        )
    )
    execute_count_after_first = len(db.executed)
    second = asyncio.run(
        workflow_ranker.get_workflow_rank(
            workflow_id=workflow_id,
            geo=None,
            pricing_model=None,
            db=db,
            redis=redis,
        )
    )

    assert second == first
    assert len(db.executed) == execute_count_after_first
    assert redis.set_calls[0][0] == workflow_ranker.rank_cache_key(workflow_id)
    assert redis.set_calls[0][2] == workflow_ranker.RANK_CACHE_TTL_SECONDS
    assert redis.get_calls == [
        workflow_ranker.rank_cache_key(workflow_id),
        workflow_ranker.rank_cache_key(workflow_id),
    ]


def test_rank_workflow_route_returns_rank_response(client, api_key_headers, monkeypatch):
    """GET /v1/workflows/{id}/rank should return ranked step candidates."""
    workflow_id = uuid4()
    service_id = uuid4()

    async def fake_rank(
        workflow_id,
        *,
        geo,
        pricing_model,
        agent_did=None,
        db,
        redis=None,
    ):
        assert geo == "US"
        assert pricing_model == "usage"
        assert agent_did == "did:key:z6MkRankAgent"
        return WorkflowRankResponse(
            workflow_id=workflow_id,
            ranked_steps=[
                RankedStep(
                    step_number=1,
                    ontology_tag="travel.air.book",
                    is_required=True,
                    min_trust_tier=3,
                    min_trust_score=75.0,
                    candidates=[
                        ServiceCandidate(
                            service_id=service_id,
                            name="FlightBookerPro",
                            trust_score=91.2,
                            trust_tier=3,
                            rank_score=0.912,
                            can_disclose=True,
                        )
                    ],
                )
            ],
        )

    monkeypatch.setattr(
        workflows_router.workflow_ranker,
        "get_workflow_rank",
        fake_rank,
    )

    response = client.get(
        (
            f"/v1/workflows/{workflow_id}/rank?geo=US&pricing_model=usage"
            "&agent_did=did:key:z6MkRankAgent"
        ),
        headers=api_key_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_id"] == str(workflow_id)
    assert body["ranked_steps"][0]["candidates"][0]["can_disclose"] is True
