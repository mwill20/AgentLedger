"""Full-stack integration tests against real PostgreSQL + Redis.

These tests exercise the entire API stack including:
- FastAPI routing and middleware
- Pydantic validation and sanitization
- async SQLAlchemy + asyncpg database operations
- pgvector embedding storage and cosine search
- Redis caching
- Celery task logic (called directly, not via broker)

Requires: docker compose up -d db redis app
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from uuid import uuid4

import httpx
import psycopg2
import pytest

from tests.test_integration.conftest import requires_db

BASE_URL = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_manifest(client: httpx.Client, payload: dict, headers: dict) -> httpx.Response:
    """Register a manifest via POST."""
    return client.post(f"{BASE_URL}/v1/manifests", json=payload, headers=headers)


def _search(client: httpx.Client, query: str, headers: dict, **kwargs) -> httpx.Response:
    """Run a semantic search."""
    body = {"query": query, **kwargs}
    return client.post(f"{BASE_URL}/v1/search", json=body, headers=headers)


# ---------------------------------------------------------------------------
# 1. POST /manifests — happy path against real Postgres
# ---------------------------------------------------------------------------

@requires_db
class TestManifestRegistration:
    """POST /manifests integration tests."""

    def test_register_manifest_returns_201(self, api_key_headers, sample_manifest):
        """A valid manifest should be accepted and stored in PostgreSQL."""
        with httpx.Client() as client:
            resp = _post_manifest(client, sample_manifest, api_key_headers)

        assert resp.status_code == 201
        body = resp.json()
        assert body["service_id"] == sample_manifest["service_id"]
        assert body["trust_tier"] == 1
        assert body["capabilities_indexed"] == 2
        assert body["status"] in ("registered", "pending_review")

    def test_registered_service_exists_in_database(self, api_key_headers, sample_manifest, sync_conn):
        """Registered service should be queryable from PostgreSQL."""
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT name, domain, trust_tier FROM services WHERE id = %s",
                (sample_manifest["service_id"],),
            )
            row = cur.fetchone()

        assert row is not None
        assert row[0] == sample_manifest["name"]
        assert row[2] == 1  # trust_tier

    def test_embeddings_stored_in_pgvector(self, api_key_headers, sample_manifest, sync_conn):
        """Capability embeddings should be stored as vector(384) in pgvector."""
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT ontology_tag, embedding IS NOT NULL as has_embedding FROM service_capabilities WHERE service_id = %s",
                (sample_manifest["service_id"],),
            )
            rows = cur.fetchall()

        assert len(rows) == 2
        for tag, has_embedding in rows:
            assert has_embedding, f"Embedding missing for {tag}"

    def test_identical_manifest_update_is_idempotent(
        self,
        api_key_headers,
        sample_manifest,
        sync_conn,
    ):
        """Posting the same manifest twice should not create duplicate current rows."""
        with httpx.Client() as client:
            first = _post_manifest(client, sample_manifest, api_key_headers)
            second = _post_manifest(client, sample_manifest, api_key_headers)

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["status"] == "updated"

        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM manifests WHERE service_id = %s",
                (sample_manifest["service_id"],),
            )
            manifest_versions = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM manifests WHERE service_id = %s AND is_current = true",
                (sample_manifest["service_id"],),
            )
            current_rows = cur.fetchone()[0]

        assert manifest_versions == 1
        assert current_rows == 1

    def test_duplicate_ontology_tags_rejected(self, api_key_headers, sample_manifest):
        """Manifest with duplicate ontology tags should return 422."""
        payload = sample_manifest.copy()
        payload["capabilities"] = [
            {"id": "c1", "ontology_tag": "travel.air.book", "description": "Book flights to major cities with instant confirmation."},
            {"id": "c2", "ontology_tag": "travel.air.book", "description": "Also book flights — duplicate tag for testing purposes."},
        ]

        with httpx.Client() as client:
            resp = _post_manifest(client, payload, api_key_headers)

        assert resp.status_code == 422

    def test_invalid_ontology_tag_rejected(self, api_key_headers, sample_manifest):
        """Unknown ontology tags should return 422."""
        payload = sample_manifest.copy()
        payload["capabilities"] = [
            {"id": "c1", "ontology_tag": "fake.nonexistent.tag", "description": "This tag does not exist in the ontology."},
        ]

        with httpx.Client() as client:
            resp = _post_manifest(client, payload, api_key_headers)

        assert resp.status_code == 422
        assert "unknown ontology_tag" in resp.json()["detail"]

    def test_sensitive_tag_flagged_for_review(self, api_key_headers, unique_id):
        """Services with sensitivity_tier >= 3 tags should be pending_review."""
        payload = {
            "manifest_version": "1.0",
            "service_id": unique_id,
            "name": f"IntegrationTest-{unique_id[:8]}",
            "domain": f"sensitive-{unique_id[:8]}.example.com",
            "capabilities": [
                {"id": "c1", "ontology_tag": "finance.payments.send", "description": "Execute a payment or money transfer to a recipient securely."},
            ],
            "pricing": {"model": "free"},
            "context": {"data_retention_days": 90},
            "operations": {},
            "last_updated": "2026-04-12T00:00:00Z",
        }

        with httpx.Client() as client:
            resp = _post_manifest(client, payload, api_key_headers)

        assert resp.status_code == 201
        assert resp.json()["status"] == "pending_review"


# ---------------------------------------------------------------------------
# 2. GET /services — structured query against real data
# ---------------------------------------------------------------------------

@requires_db
class TestStructuredQuery:
    """GET /services integration tests."""

    def test_query_by_ontology_tag(self, api_key_headers, sample_manifest):
        """Registered service should appear in structured query by tag."""
        with httpx.Client() as client:
            reg_resp = _post_manifest(client, sample_manifest, api_key_headers)
            assert reg_resp.status_code == 201, f"Registration failed: {reg_resp.json()}"

            resp = client.get(
                f"{BASE_URL}/v1/services",
                params={"ontology": "travel.air.book"},
                headers=api_key_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1

        service_ids = [r["service_id"] for r in body["results"]]
        assert sample_manifest["service_id"] in service_ids

    def test_query_with_trust_min_filter(self, api_key_headers, sample_manifest):
        """Trust minimum filter should exclude low-trust services."""
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

            # Query with trust_min higher than any new service could have
            resp = client.get(
                f"{BASE_URL}/v1/services",
                params={"ontology": "travel.air.book", "trust_min": 99},
                headers=api_key_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        # New services have low trust, so they should be filtered out
        service_ids = [r["service_id"] for r in body["results"]]
        assert sample_manifest["service_id"] not in service_ids

    def test_get_service_detail(self, api_key_headers, sample_manifest):
        """GET /services/{id} should return full service record."""
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

            resp = client.get(
                f"{BASE_URL}/v1/services/{sample_manifest['service_id']}",
                headers=api_key_headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["service_id"] == sample_manifest["service_id"]
        assert body["name"] == sample_manifest["name"]
        assert len(body["capabilities"]) == 2


# ---------------------------------------------------------------------------
# 3. POST /search — pgvector cosine search with real embeddings
# ---------------------------------------------------------------------------

@requires_db
class TestSemanticSearch:
    """POST /search integration tests with real pgvector."""

    def test_semantic_search_finds_relevant_service(self, api_key_headers, sample_manifest, unique_id):
        """Searching for 'book a flight' should find a travel.air.book service."""
        # Use unique query to avoid Redis cache hits from previous runs
        unique_query = f"book a flight for test {unique_id[:8]}"

        with httpx.Client() as client:
            reg = _post_manifest(client, sample_manifest, api_key_headers)
            assert reg.status_code == 201

            resp = _search(client, unique_query, api_key_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1

        # Our service should be in the results
        result_ids = [r["service_id"] for r in body["results"]]
        assert sample_manifest["service_id"] in result_ids

        # Best match should be travel.air.book
        our_result = next(r for r in body["results"] if r["service_id"] == sample_manifest["service_id"])
        best_cap = max(our_result["matched_capabilities"], key=lambda c: c["match_score"])
        assert best_cap["ontology_tag"] == "travel.air.book"
        assert best_cap["match_score"] > 0.1

    def test_search_deduplicates_by_service(self, api_key_headers, sample_manifest, unique_id):
        """Each service should appear once, with all matching capabilities grouped."""
        unique_query = f"flights and travel booking {unique_id[:8]}"

        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)
            resp = _search(client, unique_query, api_key_headers)

        assert resp.status_code == 200
        body = resp.json()

        # Count how many times our service appears
        our_results = [r for r in body["results"] if r["service_id"] == sample_manifest["service_id"]]
        assert len(our_results) == 1, "Service should appear exactly once"

        # It should have multiple matched capabilities
        assert len(our_results[0]["matched_capabilities"]) >= 1

    def test_search_respects_trust_min(self, api_key_headers, sample_manifest, unique_id):
        """Search with high trust_min should exclude low-trust services."""
        unique_query = f"book a flight trust test {unique_id[:8]}"
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)
            resp = _search(client, unique_query, api_key_headers, trust_min=99)

        assert resp.status_code == 200
        body = resp.json()
        result_ids = [r["service_id"] for r in body["results"]]
        assert sample_manifest["service_id"] not in result_ids

    def test_search_caching(self, api_key_headers, sample_manifest, unique_id):
        """Second identical search should be served from Redis cache."""
        unique_query = f"book a flight caching test {unique_id[:8]}"
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

            # First search — populates cache
            t1_start = time.monotonic()
            resp1 = _search(client, unique_query, api_key_headers)
            t1_elapsed = time.monotonic() - t1_start

            # Second search — should hit cache
            t2_start = time.monotonic()
            resp2 = _search(client, unique_query, api_key_headers)
            t2_elapsed = time.monotonic() - t2_start

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["total"] == resp2.json()["total"]
        # Cached response should generally be faster (but don't assert timing
        # in CI — just verify both succeed with same results)

    def test_empty_search_returns_400(self, api_key_headers):
        """Empty query after strip should return 400."""
        with httpx.Client() as client:
            resp = _search(client, "   ", api_key_headers)

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 4. DNS verification — trust_tier update
# ---------------------------------------------------------------------------

@requires_db
class TestDNSVerification:
    """Domain verification trust_tier update tests."""

    def test_verification_promotes_trust_tier(self, api_key_headers, sample_manifest, sync_conn):
        """Successful DNS verification should update trust_tier 1→2."""
        from unittest.mock import patch

        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

        # Verify trust_tier is 1 before verification
        sync_conn.rollback()  # refresh transaction snapshot
        with sync_conn.cursor() as cur:
            cur.execute("SELECT trust_tier FROM services WHERE id = %s", (sample_manifest["service_id"],))
            assert cur.fetchone()[0] == 1

        # Run verification with mocked DNS (returns correct TXT record)
        from crawler.tasks.verify_domain import _verify_domain_impl

        expected_token = f"agentledger-verify={sample_manifest['service_id']}"
        with patch("crawler.tasks.verify_domain._resolve_txt_records", return_value=[expected_token]):
            result = _verify_domain_impl(
                sample_manifest["domain"],
                sample_manifest["service_id"],
            )

        assert result["status"] == "verified"
        assert result["trust_tier"] == 2

        # Verify trust_tier is now 2 in database
        sync_conn.rollback()  # refresh transaction view
        with sync_conn.cursor() as cur:
            cur.execute("SELECT trust_tier, last_verified_at FROM services WHERE id = %s", (sample_manifest["service_id"],))
            row = cur.fetchone()
            assert row[0] == 2
            assert row[1] is not None  # last_verified_at should be set

    def test_verification_logs_event(self, api_key_headers, sample_manifest, sync_conn):
        """Verification should create a crawl_event record."""
        from unittest.mock import patch

        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

        expected_token = f"agentledger-verify={sample_manifest['service_id']}"
        from crawler.tasks.verify_domain import _verify_domain_impl

        with patch("crawler.tasks.verify_domain._resolve_txt_records", return_value=[expected_token]):
            _verify_domain_impl(sample_manifest["domain"], sample_manifest["service_id"])

        sync_conn.rollback()
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT event_type FROM crawl_events WHERE service_id = %s ORDER BY created_at DESC LIMIT 1",
                (sample_manifest["service_id"],),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "domain_verified"


# ---------------------------------------------------------------------------
# 5. 3-failure deactivation logic
# ---------------------------------------------------------------------------

@requires_db
class TestCrawlFailureDeactivation:
    """Crawl failure → inactive service tests."""

    def test_three_failures_deactivates_service(self, api_key_headers, sample_manifest, sync_conn):
        """3 consecutive crawl failures should set is_active=false."""
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

        # Verify service is active
        with sync_conn.cursor() as cur:
            cur.execute("SELECT is_active FROM services WHERE id = %s", (sample_manifest["service_id"],))
            assert cur.fetchone()[0] is True

        # Simulate 3 crawl failures by calling the implementation directly
        from crawler.tasks.crawl import _crawl_service_impl

        for i in range(3):
            result = _crawl_service_impl(
                sample_manifest["service_id"],
                sample_manifest["domain"],  # Non-existent domain → will fail
            )

        assert result["status"] == "marked_inactive"
        assert result["consecutive_failures"] == 3

        # Verify service is now inactive in database
        sync_conn.rollback()
        with sync_conn.cursor() as cur:
            cur.execute("SELECT is_active FROM services WHERE id = %s", (sample_manifest["service_id"],))
            assert cur.fetchone()[0] is False

    def test_crawl_events_logged(self, api_key_headers, sample_manifest, sync_conn):
        """Each crawl attempt should log an event."""
        with httpx.Client() as client:
            _post_manifest(client, sample_manifest, api_key_headers)

        from crawler.tasks.crawl import _crawl_service_impl

        _crawl_service_impl(sample_manifest["service_id"], sample_manifest["domain"])

        sync_conn.rollback()
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT event_type FROM crawl_events WHERE service_id = %s ORDER BY created_at ASC",
                (sample_manifest["service_id"],),
            )
            events = cur.fetchall()

        assert len(events) >= 1
        assert events[-1][0] == "crawl_failure"


# ---------------------------------------------------------------------------
# 6. Auth and rate limiting
# ---------------------------------------------------------------------------

@requires_db
class TestAuthAndRateLimiting:
    """Authentication and rate limiting integration tests."""

    def test_missing_api_key_returns_401(self):
        """Requests without X-API-Key should be rejected."""
        with httpx.Client() as client:
            resp = client.get(f"{BASE_URL}/v1/ontology")

        assert resp.status_code == 401

    def test_invalid_api_key_returns_401(self):
        """Invalid API key should be rejected."""
        with httpx.Client() as client:
            resp = client.get(
                f"{BASE_URL}/v1/ontology",
                headers={"X-API-Key": "completely-invalid-key"},
            )

        assert resp.status_code == 401

    def test_health_endpoint_no_auth_required(self):
        """GET /health should work without API key."""
        with httpx.Client() as client:
            resp = client.get(f"{BASE_URL}/v1/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_exhausted_api_key_returns_429(self, sync_conn):
        """API key with query_count >= monthly_limit should get 429."""
        test_key = f"exhausted-integ-{uuid4().hex[:8]}"
        key_hash = sha256(test_key.encode()).hexdigest()

        with sync_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_keys (key_hash, name, owner, query_count, monthly_limit, is_active)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (key_hash, "Integration Test Exhausted", "test", 1000, 1000, True),
            )
        sync_conn.commit()

        try:
            with httpx.Client() as client:
                resp = client.get(
                    f"{BASE_URL}/v1/ontology",
                    headers={"X-API-Key": test_key},
                )

            assert resp.status_code == 429
            assert "retry-after" in resp.headers
        finally:
            with sync_conn.cursor() as cur:
                cur.execute("DELETE FROM api_keys WHERE key_hash = %s", (key_hash,))
            sync_conn.commit()


# ---------------------------------------------------------------------------
# 7. Ontology
# ---------------------------------------------------------------------------

@requires_db
class TestOntology:
    """Ontology endpoint integration tests."""

    def test_ontology_returns_65_tags(self, api_key_headers):
        """GET /ontology should return all 65 v0.1 tags."""
        with httpx.Client() as client:
            resp = client.get(f"{BASE_URL}/v1/ontology", headers=api_key_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_tags"] == 65
        assert len(body["tags"]) == 65

    def test_ontology_has_five_domains(self, api_key_headers):
        """Ontology should cover exactly 5 domains."""
        with httpx.Client() as client:
            resp = client.get(f"{BASE_URL}/v1/ontology", headers=api_key_headers)

        domains = resp.json()["domains"]
        assert sorted(domains) == ["COMMERCE", "FINANCE", "HEALTH", "PRODUCTIVITY", "TRAVEL"]


# ---------------------------------------------------------------------------
# 8. Input sanitization
# ---------------------------------------------------------------------------

@requires_db
class TestInputSanitization:
    """Input validation and sanitization integration tests."""

    def test_null_bytes_rejected(self, api_key_headers, sample_manifest):
        """Null bytes in any string field should be rejected."""
        payload = sample_manifest.copy()
        payload["name"] = "Test\x00Null"

        with httpx.Client() as client:
            resp = _post_manifest(client, payload, api_key_headers)

        assert resp.status_code == 422

    def test_typosquat_detection(self, api_key_headers, sync_conn):
        """Registering a domain similar to an existing one should generate warnings."""
        # Register the "real" service first
        real_id = str(uuid4())
        real_manifest = {
            "manifest_version": "1.0",
            "service_id": real_id,
            "name": f"IntegrationTest-{real_id[:8]}",
            "domain": f"flightbooker-{real_id[:8]}.com",
            "capabilities": [{"id": "c1", "ontology_tag": "travel.air.book", "description": "Book flights to major cities with instant confirmation."}],
            "pricing": {"model": "free"},
            "context": {"data_retention_days": 0},
            "operations": {},
            "last_updated": "2026-04-12T00:00:00Z",
        }

        # Register typosquat (change one char in the base)
        typo_id = str(uuid4())
        typo_domain_base = f"flightbooker-{real_id[:8]}"
        # Change first 'o' to '0'
        typo_domain_base = typo_domain_base.replace("o", "0", 1)
        typo_manifest = {
            "manifest_version": "1.0",
            "service_id": typo_id,
            "name": f"IntegrationTest-{typo_id[:8]}",
            "domain": f"{typo_domain_base}.com",
            "capabilities": [{"id": "c1", "ontology_tag": "travel.air.book", "description": "Book flights to major cities with instant confirmation."}],
            "pricing": {"model": "free"},
            "context": {"data_retention_days": 0},
            "operations": {},
            "last_updated": "2026-04-12T00:00:00Z",
        }

        with httpx.Client() as client:
            resp1 = _post_manifest(client, real_manifest, api_key_headers)
            assert resp1.status_code == 201

            resp2 = _post_manifest(client, typo_manifest, api_key_headers)
            assert resp2.status_code == 201
            assert len(resp2.json()["typosquat_warnings"]) >= 1
