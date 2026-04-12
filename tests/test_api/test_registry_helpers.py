"""Unit tests for pure helper functions in api.services.registry."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.services.registry import (
    _cache_get,
    _cache_set,
    _manifest_hash,
    _manifest_url,
    _resolve_context_rows,
    _service_summary_from_row,
    _status_for_manifest,
    _trust_score_for_manifest,
    build_ontology_response,
    ensure_ontology_tag_exists,
    load_ontology_index,
    load_ontology_payload,
)
from api.models.manifest import ContextField, ServiceManifest


# ---------------------------------------------------------------------------
# Ontology helpers
# ---------------------------------------------------------------------------

class TestLoadOntology:
    def test_payload_has_tags(self):
        payload = load_ontology_payload()
        assert "tags" in payload
        assert len(payload["tags"]) == 65

    def test_index_keys_are_strings(self):
        index = load_ontology_index()
        for key in index:
            assert isinstance(key, str)
            assert "." in key  # e.g., travel.air.book

    def test_build_ontology_response(self):
        resp = build_ontology_response()
        assert resp.total_tags == 65
        assert len(resp.domains) == 5
        assert len(resp.tags) == 65
        assert len(resp.by_domain) == 5


class TestEnsureOntologyTagExists:
    def test_valid_tag_passes(self):
        ensure_ontology_tag_exists("travel.air.book")  # should not raise

    def test_invalid_tag_raises_422(self):
        with pytest.raises(HTTPException) as exc_info:
            ensure_ontology_tag_exists("nonexistent.tag.fake")
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _make_manifest(**overrides) -> ServiceManifest:
    """Build a minimal valid manifest for testing helpers."""
    data = {
        "manifest_version": "1.0",
        "service_id": "00000000-0000-0000-0000-000000000001",
        "name": "TestService",
        "domain": "test.example.com",
        "capabilities": [
            {
                "id": "cap-1",
                "ontology_tag": "travel.air.book",
                "description": "Book flights to major cities with instant confirmation and seat selection.",
            }
        ],
        "pricing": {"model": "per_transaction"},
        "context": {"data_retention_days": 30},
        "operations": {"uptime_sla_percent": 99.5},
        "last_updated": "2026-04-12T00:00:00Z",
    }
    data.update(overrides)
    return ServiceManifest(**data)


class TestManifestHash:
    def test_deterministic(self):
        m = _make_manifest()
        assert _manifest_hash(m) == _manifest_hash(m)

    def test_different_names_produce_different_hashes(self):
        h1 = _manifest_hash(_make_manifest(name="Alpha"))
        h2 = _manifest_hash(_make_manifest(name="Beta"))
        assert h1 != h2


class TestManifestUrl:
    def test_format(self):
        url = _manifest_url("example.com")
        assert url == "https://example.com/.well-known/agent-manifest.json"


class TestStatusForManifest:
    def test_low_sensitivity_registered(self):
        m = _make_manifest()
        assert _status_for_manifest(m) == "registered"

    def test_high_sensitivity_pending_review(self):
        # health.records.access is sensitivity_tier 3
        m = _make_manifest(
            capabilities=[
                {
                    "id": "cap-1",
                    "ontology_tag": "health.records.retrieve",
                    "description": "Access medical records securely for authorized healthcare providers.",
                }
            ]
        )
        assert _status_for_manifest(m) == "pending_review"


class TestTrustScoreForManifest:
    def test_returns_float(self):
        m = _make_manifest()
        score = _trust_score_for_manifest(m)
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0

    def test_no_uptime_returns_baseline(self):
        m = _make_manifest(operations={})
        score = _trust_score_for_manifest(m)
        assert score >= 0.0


class TestResolveContextRows:
    def test_empty_list(self):
        assert _resolve_context_rows([], is_required=True) == []

    def test_single_field(self):
        field = ContextField(name="location", type="string", sensitivity="low")
        rows = _resolve_context_rows([field], is_required=True)
        assert len(rows) == 1
        assert rows[0]["field_name"] == "location"
        assert rows[0]["is_required"] is True


# ---------------------------------------------------------------------------
# Service summary builder
# ---------------------------------------------------------------------------

class TestServiceSummaryFromRow:
    def test_builds_summary(self):
        row = {
            "service_id": "00000000-0000-0000-0000-000000000001",
            "name": "TestService",
            "domain": "test.example.com",
            "trust_tier": 1,
            "trust_score": 10.0,
            "pricing_model": "per_transaction",
            "is_active": True,
            "ontology_tag": "travel.air.book",
            "description": "Book flights.",
            "is_verified": False,
            "avg_latency_ms": None,
            "success_rate_30d": None,
        }
        summary = _service_summary_from_row(row, match_score=0.85)
        assert summary.name == "TestService"
        assert summary.rank_score > 0
        assert len(summary.matched_capabilities) == 1
        assert summary.matched_capabilities[0].match_score == 0.85


# ---------------------------------------------------------------------------
# Redis cache helpers
# ---------------------------------------------------------------------------

class TestCacheHelpers:
    def test_cache_get_returns_value(self):
        redis = AsyncMock()
        redis.get.return_value = '{"result": "cached"}'
        result = asyncio.run(_cache_get(redis, "test:key"))
        assert result == '{"result": "cached"}'

    def test_cache_get_returns_none_on_miss(self):
        redis = AsyncMock()
        redis.get.return_value = None
        result = asyncio.run(_cache_get(redis, "test:missing"))
        assert result is None

    def test_cache_get_returns_none_on_error(self):
        redis = AsyncMock()
        redis.get.side_effect = Exception("connection lost")
        result = asyncio.run(_cache_get(redis, "test:key"))
        assert result is None

    def test_cache_set_writes(self):
        redis = AsyncMock()
        asyncio.run(_cache_set(redis, "test:key", "value"))
        redis.set.assert_called_once()

    def test_cache_set_ignores_error(self):
        redis = AsyncMock()
        redis.set.side_effect = Exception("connection lost")
        # Should not raise
        asyncio.run(_cache_set(redis, "test:key", "value"))


# ---------------------------------------------------------------------------
# Scheduler import
# ---------------------------------------------------------------------------

class TestSchedulerConstants:
    def test_schedule_has_two_tasks(self):
        from crawler.scheduler import CRAWL_SCHEDULE

        assert len(CRAWL_SCHEDULE) == 2
        assert "crawl-all-active-services" in CRAWL_SCHEDULE
        assert "verify-all-pending-domains" in CRAWL_SCHEDULE
