"""Integration test fixtures using real PostgreSQL and Redis.

Requires Docker containers to be running:
  docker compose up -d db redis

Tests create and clean up their own data using unique UUIDs.
"""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import psycopg2
import pytest
import redis as redis_lib

# Configure for local Docker DB before importing app modules
# Point all connections to localhost (host machine → Docker containers)
# Must be set BEFORE any app modules are imported
os.environ["DATABASE_URL"] = "postgresql+asyncpg://agentledger:agentledger@localhost:5432/agentledger"
os.environ["DATABASE_URL_SYNC"] = "postgresql://agentledger:agentledger@localhost:5432/agentledger"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ.setdefault("API_KEYS", "dev-local-only")

# Patch settings to use localhost URLs (for crawler tasks that use settings.database_url_sync)
from api.config import settings as _settings  # noqa: E402
_settings.database_url_sync = os.environ["DATABASE_URL_SYNC"]
_settings.database_url = os.environ["DATABASE_URL"]
_settings.redis_url = os.environ["REDIS_URL"]


def _db_available() -> bool:
    """Check if the test database is reachable."""
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL_SYNC"])
        conn.close()
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(),
    reason="PostgreSQL not available (run: docker compose up -d db redis)",
)


@pytest.fixture(scope="session")
def sync_conn():
    """Shared sync connection for setup/teardown."""
    conn = psycopg2.connect(os.environ["DATABASE_URL_SYNC"])
    yield conn
    conn.close()


@pytest.fixture
def api_key_headers() -> dict[str, str]:
    """Auth headers for integration tests (matches docker-compose API_KEYS)."""
    return {"X-API-Key": "dev-local-only"}


@pytest.fixture
def unique_id() -> str:
    """Generate a unique UUID for test isolation."""
    return str(uuid4())


@pytest.fixture
def sample_manifest(unique_id) -> dict:
    """Valid manifest payload with unique IDs for each test."""
    return {
        "manifest_version": "1.0",
        "service_id": unique_id,
        "name": f"IntegrationTest-{unique_id[:8]}",
        "domain": f"test-{unique_id[:8]}.example.com",
        "capabilities": [
            {
                "id": "cap-1",
                "ontology_tag": "travel.air.book",
                "description": "Book flights to major cities with instant confirmation and seat selection.",
            },
            {
                "id": "cap-2",
                "ontology_tag": "travel.air.search",
                "description": "Search for available flights by route date and passenger count worldwide.",
            },
        ],
        "pricing": {"model": "per_transaction"},
        "context": {"data_retention_days": 30},
        "operations": {"uptime_sla_percent": 99.5},
        "last_updated": "2026-04-12T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def flush_query_cache():
    """Flush Redis query/search cache before each test so stale results don't interfere."""
    try:
        r = redis_lib.from_url(os.environ["REDIS_URL"])
        for key in r.scan_iter("query:*"):
            r.delete(key)
        for key in r.scan_iter("search:*"):
            r.delete(key)
        r.close()
    except Exception:
        pass  # fail-open: if Redis is down, tests still run
    yield


@pytest.fixture(autouse=True)
def cleanup_test_data(sync_conn, unique_id):
    """Clean up any data created during the test."""
    yield
    # Reset any failed transaction state before cleanup
    sync_conn.rollback()
    try:
        with sync_conn.cursor() as cur:
            # Clean up in dependency order (crawl_events FK → services)
            cur.execute("DELETE FROM crawl_events WHERE service_id IN (SELECT id FROM services WHERE name LIKE 'IntegrationTest-%%')")
            cur.execute("DELETE FROM service_capabilities WHERE service_id IN (SELECT id FROM services WHERE name LIKE 'IntegrationTest-%%')")
            cur.execute("DELETE FROM service_pricing WHERE service_id IN (SELECT id FROM services WHERE name LIKE 'IntegrationTest-%%')")
            cur.execute("DELETE FROM service_context_requirements WHERE service_id IN (SELECT id FROM services WHERE name LIKE 'IntegrationTest-%%')")
            cur.execute("DELETE FROM service_operations WHERE service_id IN (SELECT id FROM services WHERE name LIKE 'IntegrationTest-%%')")
            cur.execute("DELETE FROM manifests WHERE service_id IN (SELECT id FROM services WHERE name LIKE 'IntegrationTest-%%')")
            cur.execute("DELETE FROM services WHERE name LIKE 'IntegrationTest-%%'")
        sync_conn.commit()
    except Exception:
        sync_conn.rollback()
