# Lesson 09: The Proving Ground -- Testing and Load Testing

## Welcome Back, Systems Engineer!

Every feature we've built across Lessons 01-08 needs to be verified. How do you test an async FastAPI app without a real database? How do you simulate 100 concurrent AI agents hitting the API? Today we dissect the **testing infrastructure** -- the shared fixtures that enable fast isolated tests, the monkeypatching patterns that replace database calls, and the Locust load test harness that validated production readiness.

**Goal:** Understand the testing patterns, run the test suite, and interpret load test results.
**Time:** 60 minutes
**Prerequisites:** Lessons 01-08
**Why this matters:** 213 collected tests and 80%+ coverage are the gates that prevent regressions. Understanding how they work means you can add tests for new features without guessing.

---

## Learning Objectives

- Explain the dependency override pattern for database-free unit tests
- Understand the shared fixtures in `conftest.py` and when to use each
- Describe the monkeypatching pattern for service function isolation
- Trace the load test from seed phase through profile-driven execution
- Explain why the rate limit flusher exists and how it prevents false latency readings
- Run the test suite and interpret the results

---

## Test Organization

```
tests/
|-- conftest.py              # Shared fixtures (80 lines)
|-- test_api/                # Unit tests (no database required)
|   |-- test_authorization.py    # HITL approval queue
|   |-- test_manifests.py    # Manifest registration
|   |-- test_crypto.py       # JWT + DID helpers
|   |-- test_identity.py     # Identity and authorization routes
|   |-- test_identity_service.py  # Revocation + proof nonce helpers
|   |-- test_search.py       # Semantic search
|   |-- test_service_identity.py  # did:web activation
|   |-- test_services.py     # Structured queries
|   |-- test_sessions.py     # Session assertion routes
|   |-- test_sessions_service.py  # Session replay guards
|   |-- test_ratelimit.py    # Rate limiting middleware
|   |-- test_sanitization.py # Input sanitization
|   |-- test_typosquat.py    # Typosquat detection
|   |-- test_embedder.py     # Embedding generation
|   |-- test_ranker.py       # Ranking algorithm
|   |-- test_registry_helpers.py # Ontology + registry helper logic
|   |-- test_crawler_helpers.py  # Worker schedule + crawl helpers
|   `-- test_verify.py       # DNS verification
|-- test_crawler/            # Task-level crawler tests
|-- test_integration/        # Integration tests (with DB)
`-- load/
    `-- locustfile.py         # Locust load tests (246 lines)
```

The key split: `test_api/` tests run **without Docker, without a database, without Redis**. They verify routing, validation, and business logic in isolation. Integration tests and load tests require the full stack.

---

## Code Walkthrough: `tests/conftest.py`

### Environment Setup (Before Import)

```python
import os

# Ensure test API key is configured before importing app
os.environ.setdefault("API_KEYS", "test-api-key")
os.environ.setdefault("ADMIN_API_KEYS", "test-admin-key")

from api.dependencies import get_db  # noqa: E402
from api.main import app  # noqa: E402
```

These `setdefault` calls MUST come before importing the app. Why? `api/config.py` creates the `settings` singleton at import time:

```python
# api/config.py
settings = Settings()  # reads env vars NOW, at import time
```

If `API_KEYS` isn't set when `Settings()` runs, `settings.api_keys` will be empty, and the auth dependency will reject all test requests with 401. The `setdefault` ensures a valid key exists without overwriting a value that's already set.

### The DummySession

```python
class DummySession:
    """Minimal session object used by router tests that monkeypatch service calls."""
```

This is an intentionally empty class. Router tests don't use the database session -- they monkeypatch the service functions that would normally use it. The DummySession just satisfies FastAPI's dependency injection, which expects `get_db` to yield something.

### Shared Fixtures

```python
@pytest.fixture
def api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "test-api-key"}

@pytest.fixture
def admin_api_key_headers() -> dict[str, str]:
    return {"X-API-Key": "test-admin-key"}
```

Every protected endpoint needs an API key header. These fixtures prevent repeating the magic string `"test-api-key"` across 50+ test functions.

```python
@pytest.fixture
def sample_manifest_payload() -> dict:
    return {
        "manifest_version": "1.0",
        "service_id": str(uuid4()),
        "name": "SkyBridge Travel",
        "domain": "skybridge.example",
        "public_key": "test-public-key",
        "capabilities": [{
            "id": "book-flight",
            "ontology_tag": "travel.air.book",
            "description": "Book flights for travelers with payment, seat selection, and refunds.",
        }],
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
```

A complete, valid manifest payload. Tests that need to register a manifest use this fixture directly or modify specific fields to test edge cases.

### The Test Client

```python
@pytest.fixture
def client() -> TestClient:
    async def override_get_db():
        yield DummySession()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
```

This is the core testing pattern:

1. **`app.dependency_overrides[get_db]`** -- Replaces the real `get_db` dependency with one that yields a `DummySession`. FastAPI checks this dict before resolving dependencies.
2. **`with TestClient(app)`** -- Creates a synchronous test client that sends requests to the FastAPI app without a running server.
3. **`app.dependency_overrides.clear()`** -- Cleanup after the test. Without this, overrides leak between tests.

---

## Testing Patterns

### Pattern 1: Auth Testing

```python
def test_endpoint_requires_api_key(client):
    """No X-API-Key header returns 401."""
    response = client.get("/v1/ontology")
    assert response.status_code == 401

def test_endpoint_rejects_wrong_key(client):
    """Invalid key returns 401."""
    response = client.get("/v1/ontology", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401

def test_endpoint_accepts_valid_key(client, api_key_headers):
    """Valid key returns 200."""
    response = client.get("/v1/ontology", headers=api_key_headers)
    assert response.status_code == 200
```

Every protected endpoint gets these three tests. They verify the auth dependency chain works independently of the business logic.

### Pattern 2: Monkeypatching Service Functions

```python
def test_register_manifest(client, api_key_headers, sample_manifest_payload, monkeypatch):
    async def mock_register(db, manifest):
        return ManifestRegistrationResponse(
            service_id=manifest.service_id,
            trust_tier=1,
            trust_score=20.0,
            status="registered",
            capabilities_indexed=1,
            typosquat_warnings=[],
        )

    monkeypatch.setattr("api.services.registry.register_manifest", mock_register)
    monkeypatch.setattr(
        "crawler.tasks.verify_domain.enqueue_domain_verification",
        lambda *a, **kw: False,
    )

    response = client.post("/v1/manifests", json=sample_manifest_payload, headers=api_key_headers)
    assert response.status_code == 201
    assert response.json()["status"] == "registered"
```

The monkeypatch replaces `registry.register_manifest` with a function that returns a canned response. This means:
- No database queries execute
- No embedding model loads
- No Redis calls happen
- The test runs in milliseconds

The test verifies: (1) the router correctly parses the request, (2) the Pydantic model validates successfully, (3) the response format is correct.

### Pattern 3: Validation Testing

```python
def test_manifest_rejects_null_bytes(client, api_key_headers, sample_manifest_payload):
    sample_manifest_payload["name"] = "Bad\x00Name"
    response = client.post("/v1/manifests", json=sample_manifest_payload, headers=api_key_headers)
    assert response.status_code == 422
    assert "null bytes" in response.json()["detail"][0]["msg"]
```

These tests don't need monkeypatching because the validation fails before the service function is called. Pydantic rejects the payload at the model layer.

### Pattern 4: Pure Function Testing

```python
# test_typosquat.py
def test_levenshtein_identical():
    assert levenshtein_distance("hello", "hello") == 0

def test_levenshtein_single_substitution():
    assert levenshtein_distance("cat", "bat") == 1

# test_ranker.py
def test_rank_score_all_perfect():
    score = compute_rank_score(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    assert score == 1.0

# test_embedder.py
def test_hash_embed_deterministic():
    v1 = _hash_embed("test query")
    v2 = _hash_embed("test query")
    assert v1 == v2  # same input always produces same output
```

Pure functions (no I/O, no state) get the simplest tests. No fixtures, no mocking, no setup. These are the fastest and most reliable tests in the suite.

### Pattern 5: Rate Limiter Testing with FakeRedis

```python
class FakePipeline:
    def __init__(self, results, *, execute_error=None):
        self._results = results
        self._execute_error = execute_error
    def incr(self, key): pass
    def expire(self, key, ttl): pass
    def ttl(self, key): pass
    async def execute(self):
        if self._execute_error is not None:
            raise self._execute_error
        return self._results

class FakeRedis:
    def __init__(self, *, incr_result=None, ttl_result=0, pipeline_error=None):
        self.incr_result = incr_result
        self.ttl_result = ttl_result
        self.pipeline_error = pipeline_error
    def pipeline(self):
        return FakePipeline(
            [self.incr_result or 0, True, self.ttl_result],
            execute_error=self.pipeline_error,
        )
```

The rate limiter tests create custom fake Redis clients that return predetermined values. This allows testing all branches:
- Under limit: `FakeRedis(incr_result=1)` -- returns allowed
- At limit: `FakeRedis(incr_result=100)` -- returns allowed (100 is the limit, not over)
- Over limit: `FakeRedis(incr_result=101)` -- returns rejected
- Redis failure: `FakeRedis(pipeline_error=ConnectionError())` -- tests fail-open behavior

---

## Code Walkthrough: Load Testing (`tests/load/locustfile.py`)

### Configuration

```python
API_KEY = os.environ.get("LOAD_API_KEY", "dev-local-only")
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LOAD_PROFILE = os.environ.get("LOAD_PROFILE", "mixed").strip().lower()
MANIFEST_POOL_SIZE = int(os.environ.get("MANIFEST_POOL_SIZE", "200"))
SEED_COUNT = int(os.environ.get("LOAD_SEED_COUNT", "25"))
```

Every parameter is configurable via environment variables. This enables running different profiles without code changes.

### Deterministic Manifest Pool

```python
def _manifest_payload(index: int) -> dict:
    seed = uuid5(NAMESPACE_DNS, f"agentledger-perftest-service-{index}")
    service_id = str(seed)
    suffix = seed.hex[:12]
    return {
        "manifest_version": "1.0",
        "service_id": service_id,
        "name": f"PerfTest-{index:03d}",
        "domain": f"perftest-{suffix}.example.com",
        ...
    }

def _next_manifest_payload() -> dict:
    return _manifest_payload(next(_manifest_counter) % MANIFEST_POOL_SIZE)
```

Key design decisions:

1. **`uuid5` with `NAMESPACE_DNS`** -- Deterministic UUIDs. The same index always produces the same service_id. This means the load test can be restarted without creating duplicate services.

2. **`% MANIFEST_POOL_SIZE`** -- Modular arithmetic creates a bounded pool. After 200 unique manifests, it wraps around and re-submits existing ones. This triggers the idempotency short-circuit in `register_manifest()`, preventing unbounded database growth during long load tests.

3. **Seed phase** -- `_seed_perf_manifests()` pre-loads 25 manifests before the test starts. This ensures structured queries and service detail lookups have data to return.

### Rate Limit Flusher

```python
def _flush_rate_limit_keys() -> None:
    client = redis.from_url(REDIS_URL)
    try:
        while not _flush_stop.is_set():
            for key in client.scan_iter("ratelimit:ip:*"):
                client.delete(key)
            time.sleep(1)
    finally:
        client.close()
```

This background thread continuously deletes `ratelimit:ip:*` keys from Redis. Why? Without it, the 100 concurrent Locust users would all share the same IP (localhost) and hit the 100 requests/minute rate limit almost immediately. All subsequent requests would return 429, and the p95 latency would measure the rate limiter, not the application.

By flushing the keys every second, the load test measures actual endpoint latency.

### Profile-Driven Task Selection

```python
_PROFILE_TASKS = {
    "health": {AgentLedgerUser.health_check: 1},
    "ontology": {AgentLedgerUser.get_ontology: 1},
    "services": {AgentLedgerUser.structured_query: 1},
    "search": {AgentLedgerUser.semantic_search: 1},
    "manifests": {AgentLedgerUser.register_manifest: 1},
    "service_detail": {AgentLedgerUser.get_service_detail: 1},
    "identity_verify": {AgentLedgerUser.verify_agent_credential: 1},
    "identity_lookup": {AgentLedgerUser.get_agent_identity: 1},
    "identity_mixed": {
        AgentLedgerUser.verify_agent_credential: 5,
        AgentLedgerUser.get_agent_identity: 4,
        AgentLedgerUser.list_pending_authorizations: 1,
    },
    "mixed": {
        AgentLedgerUser.health_check: 3,
        AgentLedgerUser.get_ontology: 2,
        AgentLedgerUser.structured_query: 3,
        AgentLedgerUser.semantic_search: 3,
        AgentLedgerUser.register_manifest: 1,
        AgentLedgerUser.get_service_detail: 1,
    },
}

AgentLedgerUser.tasks = _PROFILE_TASKS[LOAD_PROFILE]
```

Setting `LOAD_PROFILE=search` runs only the semantic search endpoint. Setting `LOAD_PROFILE=identity_verify` isolates credential verification. Setting `LOAD_PROFILE=mixed` runs the Layer 1 discovery endpoints, while `identity_mixed` stresses the Layer 2 identity and approval paths.

### Running the Load Test

```powershell
# Single endpoint profile
$env:LOAD_PROFILE='search'
locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 60s `
  --host http://localhost:8000 --csv tests/load/results/search

# Mixed profile
$env:LOAD_PROFILE='mixed'
locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 60s `
  --host http://localhost:8000 --csv tests/load/results/mixed
```

Parameters:
- `-u 100` -- 100 concurrent users
- `-r 20` -- Ramp up 20 users per second (full load in 5 seconds)
- `--run-time 60s` -- Run for 60 seconds
- `--csv` -- Write results to CSV for analysis

### Current Performance Targets

The original Layer 1 load target was under 500ms at the 95th percentile with 100 concurrent users. Layer 2 added tighter goals for the identity endpoints, especially `POST /v1/identity/agents/verify`, which now has its own dedicated Locust profile and a 200ms-class target. The current Layer 2 completion snapshot records that endpoint at 110ms p95 and 61ms median under 100 concurrent users after the hardening pass.

The main performance levers remain:
1. Pure ASGI middleware (eliminated thread contention)
2. Redis pipeline (single round-trip for rate limiting)
3. Manifest idempotency (hash comparison skips unnecessary writes)
4. Batched capability inserts (one SQL statement instead of N)
5. Key-object caching and hot-path revocation checks for identity verification
6. 4 uvicorn workers (parallel request processing)

---

## Running the Test Suite

```powershell
# Run all unit tests (no Docker needed)
cd C:\Projects\AgentLedger
python -m pytest tests/test_api/ -v

# Run with coverage report
python -m pytest tests/test_api/ --cov=api --cov-report=term-missing

# Run a specific test file
python -m pytest tests/test_api/test_ranker.py -v

# Run integration tests (requires Docker stack)
docker compose up -d
python -m pytest tests/test_integration/ -v
```

---

## Hands-On Exercises

### Exercise 1: Run the Unit Tests

```powershell
cd C:\Projects\AgentLedger
python -m pytest tests/test_api/ -v --tb=short
```

Count passing, failing, and skipped tests. All should pass without Docker running.

### Exercise 2: Write a New Test

Add a test to `test_ranker.py`:

```python
def test_rank_score_capability_dominates():
    """With all other factors at 0, capability match alone drives the score."""
    score = compute_rank_score(
        capability_match=1.0, trust_score=0.0,
        latency_score=0.0, cost_score=0.0,
        reliability_score=0.0, context_fit=0.0,
    )
    assert score == 0.35  # capability_match * 0.35
```

### Exercise 3: Examine Test Coverage

```powershell
python -m pytest tests/test_api/ --cov=api --cov-report=html
# Open htmlcov/index.html in a browser to see which lines are covered
```

---

## Interview Prep

**Q: How do you test FastAPI endpoints without a database?**

**A:** FastAPI's `app.dependency_overrides` allows replacing any `Depends()` dependency with a test double. We override `get_db` to yield a `DummySession` (empty class), then monkeypatch the service functions that would use the session. This means tests verify routing, validation, and response formatting without any database connection. Tests run in milliseconds, not seconds.

---

**Q: How does the load test prevent rate limiting from skewing results?**

**A:** A background thread continuously deletes `ratelimit:ip:*` keys from Redis. Without this, all 100 Locust users share the same IP (localhost) and hit the 100/minute rate limit almost immediately. The flusher ensures the load test measures actual endpoint latency, not rate limiter rejection time.

---

**Q: Why does the load test use deterministic UUIDs instead of random ones?**

**A:** uuid5 with a fixed namespace produces the same UUID for the same index. This creates a bounded manifest pool (200 services) that wraps around via modular arithmetic. Benefits: (1) Re-running the test doesn't create duplicate services, (2) the idempotency short-circuit in register_manifest kicks in for repeated submissions, preventing unbounded database growth, (3) seed data is reproducible across test runs.

---

## Key Takeaways

- Unit tests run WITHOUT Docker -- no DB, no Redis, no Celery
- `app.dependency_overrides[get_db]` replaces the real DB with DummySession
- Monkeypatching service functions isolates router tests from business logic
- Environment variables must be set BEFORE importing the app (pydantic-settings reads at import time)
- Load tests use a bounded manifest pool with deterministic UUIDs
- Rate limit key flusher prevents 429s from masking application latency
- Profile-driven load testing enables per-endpoint and mixed-traffic analysis
- Target: 213 collected tests, 80%+ coverage, Layer 1 discovery endpoints under 500ms p95, and hardened identity verification in the low-hundreds of milliseconds at 100 concurrent

---

## Summary Reference Card

| Test Type | Location | Requires Docker? | Speed | What It Tests |
|-----------|----------|-------------------|-------|---------------|
| Unit tests | `tests/test_api/` | No | Fast (ms) | Routing, validation, pure logic |
| Integration tests | `tests/test_integration/` | Yes | Medium (s) | Full request through database |
| Load tests | `tests/load/` | Yes | Slow (min) | Latency at concurrency |

| Fixture | Purpose |
|---------|---------|
| `client` | TestClient with DummySession |
| `api_key_headers` | `{"X-API-Key": "test-api-key"}` |
| `admin_api_key_headers` | `{"X-API-Key": "test-admin-key"}` |
| `sample_manifest_payload` | Complete valid manifest dict |

---

## Ready for Lesson 10?

Next up, the final lesson: **The Architect's View** -- an end-to-end architecture deep dive that synthesizes everything from Lessons 01-09 into a complete system understanding. This is the optional capstone for those who want to see the full picture.

*Remember: Tests are not overhead. They're the contract that says "this system works." Break the contract, break the system!*
