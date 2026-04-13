"""Load test profiles for AgentLedger Layer 1 API.

Targets:
- Run with 100 concurrent users against one endpoint profile at a time.
- Keep the backing dataset bounded by reusing a fixed manifest pool.
- Flush per-IP Redis rate-limit keys during the run so 429s do not mask
  application latency.

Examples:
    $env:LOAD_PROFILE='health'
    locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 60s --host http://localhost:8000 --csv tests/load/results/health

    $env:LOAD_PROFILE='search'
    locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 60s --host http://localhost:8000 --csv tests/load/results/search

Profiles:
- `health`
- `ontology`
- `services`
- `search`
- `manifests`
- `service_detail`
- `mixed`
"""

from __future__ import annotations

import os
import threading
import time
from itertools import count
from uuid import NAMESPACE_DNS, uuid5

import httpx
import redis
from locust import HttpUser, between, events, task

API_KEY = os.environ.get("LOAD_API_KEY", "dev-local-only")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LOAD_PROFILE = os.environ.get("LOAD_PROFILE", "mixed").strip().lower()
MANIFEST_POOL_SIZE = int(os.environ.get("MANIFEST_POOL_SIZE", "200"))
SEED_COUNT = int(os.environ.get("LOAD_SEED_COUNT", "25"))
WAIT_MIN_SECONDS = float(os.environ.get("LOAD_WAIT_MIN_SECONDS", "0.25"))
WAIT_MAX_SECONDS = float(os.environ.get("LOAD_WAIT_MAX_SECONDS", "0.5"))
FLUSH_RATE_LIMITS = os.environ.get("LOAD_FLUSH_RATE_LIMITS", "1") != "0"
SERVICE_DETAIL_ID = str(uuid5(NAMESPACE_DNS, "agentledger-perftest-service-0"))
_QUERY = "book flights with fare comparison and seat selection"

_flush_stop = threading.Event()
_manifest_counter = count()


def _manifest_payload(index: int) -> dict:
    """Return a deterministic manifest payload for a bounded service pool."""
    seed = uuid5(NAMESPACE_DNS, f"agentledger-perftest-service-{index}")
    service_id = str(seed)
    suffix = seed.hex[:12]
    return {
        "manifest_version": "1.0",
        "service_id": service_id,
        "name": f"PerfTest-{index:03d}",
        "domain": f"perftest-{suffix}.example.com",
        "capabilities": [
            {
                "id": f"book-{suffix}",
                "ontology_tag": "travel.air.book",
                "description": "Book flights to major cities with instant confirmation, seat selection, and payment processing.",
            },
            {
                "id": f"search-{suffix}",
                "ontology_tag": "travel.air.search",
                "description": "Search flights across airlines with fares, schedules, route comparison, and passenger filters.",
            },
        ],
        "pricing": {"model": "per_transaction"},
        "context": {"data_retention_days": 30},
        "operations": {"uptime_sla_percent": 99.5},
        "last_updated": "2026-04-12T00:00:00Z",
    }


def _next_manifest_payload() -> dict:
    """Rotate through a fixed pool instead of growing the database indefinitely."""
    return _manifest_payload(next(_manifest_counter) % MANIFEST_POOL_SIZE)


def _seed_perf_manifests(host: str) -> None:
    """Ensure a small deterministic manifest pool exists before the test starts."""
    with httpx.Client(base_url=host, timeout=30.0) as client:
        for index in range(min(SEED_COUNT, MANIFEST_POOL_SIZE)):
            response = client.post("/v1/manifests", json=_manifest_payload(index), headers=HEADERS)
            response.raise_for_status()


def _flush_rate_limit_keys() -> None:
    """Continuously delete ratelimit:ip:* keys so 429s don't skew latency."""
    client = redis.from_url(REDIS_URL)
    try:
        while not _flush_stop.is_set():
            for key in client.scan_iter("ratelimit:ip:*"):
                client.delete(key)
            time.sleep(1)
    finally:
        client.close()


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Seed deterministic perf data and start the rate-limit flusher."""
    if environment.host:
        _seed_perf_manifests(environment.host)

    if FLUSH_RATE_LIMITS:
        _flush_stop.clear()
        thread = threading.Thread(target=_flush_rate_limit_keys, daemon=True)
        thread.start()


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Stop the background Redis flusher."""
    _flush_stop.set()


class AgentLedgerUser(HttpUser):
    """Profile-driven Locust user for endpoint-specific latency runs."""

    wait_time = between(WAIT_MIN_SECONDS, WAIT_MAX_SECONDS)

    @task
    def health_check(self):
        self.client.get("/v1/health", name="/v1/health")

    @task
    def get_ontology(self):
        self.client.get("/v1/ontology", headers=HEADERS, name="/v1/ontology")

    @task
    def structured_query(self):
        self.client.get(
            "/v1/services",
            params={"ontology": "travel.air.book"},
            headers=HEADERS,
            name="/v1/services?ontology=travel.air.book",
        )

    @task
    def semantic_search(self):
        self.client.post(
            "/v1/search",
            json={"query": _QUERY},
            headers=HEADERS,
            name="/v1/search",
        )

    @task
    def register_manifest(self):
        self.client.post(
            "/v1/manifests",
            json=_next_manifest_payload(),
            headers=HEADERS,
            name="/v1/manifests",
        )

    @task
    def get_service_detail(self):
        self.client.get(
            f"/v1/services/{SERVICE_DETAIL_ID}",
            headers=HEADERS,
            name="/v1/services/{id}",
        )


_PROFILE_TASKS = {
    "health": {AgentLedgerUser.health_check: 1},
    "ontology": {AgentLedgerUser.get_ontology: 1},
    "services": {AgentLedgerUser.structured_query: 1},
    "search": {AgentLedgerUser.semantic_search: 1},
    "manifests": {AgentLedgerUser.register_manifest: 1},
    "service_detail": {AgentLedgerUser.get_service_detail: 1},
    "mixed": {
        AgentLedgerUser.health_check: 3,
        AgentLedgerUser.get_ontology: 2,
        AgentLedgerUser.structured_query: 3,
        AgentLedgerUser.semantic_search: 3,
        AgentLedgerUser.register_manifest: 1,
        AgentLedgerUser.get_service_detail: 1,
    },
}

if LOAD_PROFILE not in _PROFILE_TASKS:
    raise RuntimeError(f"unknown LOAD_PROFILE: {LOAD_PROFILE}")

AgentLedgerUser.tasks = _PROFILE_TASKS[LOAD_PROFILE]
