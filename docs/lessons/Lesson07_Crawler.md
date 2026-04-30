# Lesson 07: The Watchdog -- Crawler, DNS Verification, and Trust Tiers

> **Beginner frame:** A crawler is a periodic inspector. It checks whether registered services are still reachable and whether the service controls the domain it claims, so stale or unverifiable records do not keep looking trustworthy forever.

## Welcome Back, Systems Engineer!

Services register themselves via `POST /manifests`, but how does AgentLedger know they're still alive? How does it verify that `flightbooker.example.com` actually controls that domain? Today we dissect the **Celery background workers** that crawl manifests, verify DNS ownership, and manage the trust tier progression that underpins the entire trust model.

**Goal:** Understand the two crawl vectors (standard crawl and DNS verification), how they use synchronous database connections, and how the trust tier system works.
**Time:** 60 minutes
**Prerequisites:** Lessons 01-06
**Why this matters:** Without the crawler, the registry goes stale. Without DNS verification, any service can claim any domain. The crawler is the immune system of AgentLedger.

---

## Learning Objectives

- Explain why Celery workers use synchronous psycopg2 instead of async asyncpg
- Trace Vector A (standard crawl) from beat schedule through manifest fetch to failure tracking
- Trace Vector B (DNS verification) from registration trigger through TXT record check to trust tier promotion
- Describe the trust tier progression (1 through 4)
- Explain the 30-day verification window and consecutive failure threshold
- Understand the conditional Celery task registration pattern

---

## File Map

```
crawler/
|-- __init__.py
|-- worker.py                    # Celery app creation (60 lines)
|-- tasks/
    |-- __init__.py
    |-- crawl.py                 # Vector A -- standard crawl (237 lines)
    |-- verify_domain.py         # Vector B -- DNS verification (244 lines)
    |-- expire_identity_records.py  # Layer 2 cleanup
    |-- revalidate_service_identity.py  # Layer 2 identity revalidation

api/services/
|-- verifier.py                  # DNS token generation (17 lines)

api/routers/
|-- verify.py                    # Manual verification trigger (37 lines)
```

---

## Code Walkthrough: `crawler/worker.py`

```python
# crawler/worker.py -- Celery app creation

def create_celery_app() -> Celery | None:
    if Celery is None:
        return None

    app = Celery("agentledger", broker=settings.redis_url, backend=settings.redis_url)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
    )
    app.conf.include = [
        "crawler.tasks.crawl",
        "crawler.tasks.expire_identity_records",
        "crawler.tasks.revalidate_service_identity",
        "crawler.tasks.verify_domain",
    ]
    app.conf.beat_schedule = {
        "crawl-all-active-services": {
            "task": "crawler.crawl_all",
            "schedule": 60 * 60 * 24,  # every 24 hours
        },
        "verify-all-pending-domains": {
            "task": "crawler.verify_all_pending",
            "schedule": 60 * 60 * 24,  # every 24 hours
        },
        "expire-identity-records": {
            "task": "crawler.expire_identity_records",
            "schedule": 60,  # every minute
        },
        "revalidate-service-identity": {
            "task": "crawler.revalidate_service_identity",
            "schedule": 60 * 60 * 24,  # every 24 hours
        },
    }
    return app
```

Key details:

1. **JSON-only serialization** -- `task_serializer="json"` and `accept_content=["json"]`. Celery's default serialization format can execute arbitrary code via deserialized payloads. JSON-only prevents this. This is a security-critical configuration.

2. **Redis as both broker and backend** -- The same Redis instance handles task queuing (broker) and result storage (backend). This keeps the infrastructure simple.

3. **Beat schedule** -- Four periodic tasks:
   - `crawl_all` every 24 hours -- re-fetches manifests from all active services
   - `verify_all_pending` every 24 hours -- checks DNS TXT records for unverified services
   - `expire_identity_records` every 60 seconds -- Layer 2 credential cleanup
   - `revalidate_service_identity` every 24 hours -- refreshes active service `did:web` attestations

4. **Conditional import** -- `Celery` is imported in a try/except block. If Celery isn't installed (e.g., during unit tests), `create_celery_app()` returns None and all tasks fall back to plain functions.

### The Sync Connection

```python
def get_sync_connection():
    import psycopg2
    return psycopg2.connect(settings.database_url_sync)
```

**Why psycopg2 instead of asyncpg?** Celery workers run synchronous Python processes. They can't use `await` or async session factories. The `database_url_sync` setting uses the `postgresql://` scheme (psycopg2 driver) instead of `postgresql+asyncpg://`. Same database, different driver.

This means the codebase has two database access patterns:
- **FastAPI routes**: async SQLAlchemy + asyncpg (`database_url`)
- **Celery tasks**: sync psycopg2 (`database_url_sync`)

---

## Code Walkthrough: Vector A -- Standard Crawl (`crawl.py`)

### Pure Helpers

The file starts with three pure functions that are unit-testable without any database or network:

```python
WELL_KNOWN_MANIFEST_PATH = "/.well-known/agent-manifest.json"
CRAWL_TIMEOUT_SECONDS = 15
MAX_CONSECUTIVE_FAILURES = 3

def build_manifest_url(domain: str) -> str:
    return f"https://{domain}{WELL_KNOWN_MANIFEST_PATH}"

def compute_manifest_hash(payload: dict) -> str:
    serialized = json.dumps(payload, sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()

def should_mark_service_inactive(consecutive_failures: int) -> bool:
    return consecutive_failures >= MAX_CONSECUTIVE_FAILURES
```

The manifest URL convention: every service publishes its manifest at `https://{domain}/.well-known/agent-manifest.json`. This mirrors the `.well-known` URI convention used by Let's Encrypt, OAuth, and other web standards.

### The Crawl Implementation

```python
def _crawl_service_impl(service_id: str, domain: str) -> dict:
    url = build_manifest_url(domain)
    conn = get_sync_connection()

    try:
        response = httpx.get(url, timeout=CRAWL_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        manifest_hash = compute_manifest_hash(payload)

        _log_crawl_event(conn, service_id, domain, "crawl_success", {
            "url": url, "status_code": response.status_code,
            "manifest_hash": manifest_hash,
        })
        _update_service_after_crawl(conn, service_id, manifest_hash, is_active=True)
        return {"service_id": service_id, "domain": domain, "status": "success", ...}

    except Exception as exc:
        _log_crawl_event(conn, service_id, domain, "crawl_failure", {
            "url": url, "error": str(exc),
        })
        failures = _get_consecutive_failure_count(conn, service_id)
        if should_mark_service_inactive(failures):
            _update_service_after_crawl(conn, service_id, None, is_active=False)
            return {..., "status": "marked_inactive", ...}
        else:
            _update_service_after_crawl(conn, service_id, None, is_active=True)
            return {..., "status": "failure", ...}
    finally:
        conn.close()
```

The crawl flow:

1. **Fetch** -- httpx GET with 15-second timeout and redirect following
2. **On success** -- Log the event, update the service record, store the manifest hash
3. **On failure** -- Log the failure, count consecutive failures, mark inactive after 3

### Consecutive Failure Tracking

```python
def _get_consecutive_failure_count(conn, service_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT event_type FROM crawl_events
            WHERE service_id = %s
            ORDER BY created_at DESC LIMIT 10
        """, (service_id,))
        count = 0
        for (event_type,) in cur.fetchall():
            if event_type == "crawl_failure":
                count += 1
            else:
                break
        return count
```

This reads the most recent 10 crawl events in reverse chronological order and counts consecutive failures. The `break` on the first non-failure event is key -- three failures followed by a success resets the counter. The service only goes inactive after three consecutive failures with no successes in between.

### Manifest Change Detection

```python
def _update_service_after_crawl(conn, service_id, manifest_hash, is_active):
    with conn.cursor() as cur:
        cur.execute("""UPDATE services SET last_crawled_at = NOW(), is_active = %s ...""", ...)
        if manifest_hash is not None:
            cur.execute("""
                SELECT manifest_hash FROM manifests
                WHERE service_id = %s AND is_current = true ...
            """, (service_id,))
            row = cur.fetchone()
            if row and row[0] != manifest_hash:
                cur.execute(
                    "UPDATE manifests SET is_current = false WHERE ...",
                    (service_id,),
                )
    conn.commit()
```

If the manifest hash changed since the last crawl, the old manifest is marked `is_current = false`. This preserves manifest history -- you can see every version a service has published.

### Crawl All

```python
def _crawl_all_impl() -> dict:
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, domain FROM services WHERE is_active = true AND is_banned = false")
            services = cur.fetchall()
    finally:
        conn.close()

    results = []
    for service_id, domain in services:
        result = _crawl_service_impl(str(service_id), domain)
        results.append(result)
    return {"total": len(results), "results": results}
```

Sequential crawling -- each service is crawled one at a time. This is simple and safe (no connection pool exhaustion), but could be parallelized with Celery task groups in a future optimization.

---

## Code Walkthrough: Vector B -- DNS Verification (`verify_domain.py`)

### DNS TXT Resolution

```python
def _resolve_txt_records(domain: str) -> list[str]:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "TXT")
        records = []
        for rdata in answers:
            for txt_string in rdata.strings:
                records.append(txt_string.decode("utf-8", errors="replace"))
        return records
    except Exception as exc:
        logger.debug("DNS TXT lookup failed for %s: %s", domain, exc)
        return []
```

The function uses `dnspython` to resolve TXT records. On any error (NXDOMAIN, timeout, network failure), it returns an empty list. This fail-safe approach means DNS issues don't crash the worker -- they just prevent verification.

### The Verification Flow

```python
def _verify_domain_impl(domain: str, service_id: str) -> dict:
    conn = get_sync_connection()
    try:
        service_info = _get_service_info(conn, service_id)
        if service_info is None:
            return {"status": "service_not_found"}

        # Already verified -- skip
        if service_info["trust_tier"] >= 2:
            return {"status": "already_verified", "trust_tier": service_info["trust_tier"]}

        # Check 30-day verification window
        first_seen = service_info["first_seen_at"]
        if first_seen:
            age_days = (datetime.now(timezone.utc) - first_seen).days
            if age_days > VERIFICATION_MAX_AGE_DAYS:
                return {"status": "verification_window_expired", "age_days": age_days}

        # Resolve and check TXT records
        txt_records = _resolve_txt_records(domain)
        if evaluate_domain_verification(service_id, txt_records):
            _promote_trust_tier(conn, service_id, new_tier=2)
            return {"status": "verified", "trust_tier": 2}
        else:
            return {"status": "pending", "txt_records_found": len(txt_records)}
    finally:
        conn.close()
```

Four possible outcomes:

1. **service_not_found** -- Service was deleted between queuing and execution
2. **already_verified** -- Trust tier is already >= 2 (skip redundant work)
3. **verification_window_expired** -- More than 30 days since registration without verification
4. **verified** or **pending** -- TXT record matched or didn't match

### The 30-Day Window

`VERIFICATION_MAX_AGE_DAYS = 30` -- After 30 days without DNS verification, the service's verification window expires. This prevents the beat scheduler from perpetually checking services whose operators clearly aren't going to set up TXT records.

### Trust Tier Promotion

```python
def _promote_trust_tier(conn, service_id: str, new_tier: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE services
            SET trust_tier = %s, last_verified_at = NOW(), updated_at = NOW()
            WHERE id = %s AND trust_tier < %s
        """, (new_tier, service_id, new_tier))
    conn.commit()
```

The `WHERE trust_tier < %s` condition prevents downgrades. If a service is already at tier 3 (probed), this won't demote it to tier 2.

### Enqueueing from the API

```python
def enqueue_domain_verification(domain: str, service_id: UUID | str) -> bool:
    if celery_app is None:
        return False
    verify_domain_task.delay(domain, str(service_id))
    return True
```

Called from `api/routers/manifests.py` after every successful registration. The `.delay()` call is non-blocking -- it just queues the task and returns immediately.

---

## Code Walkthrough: DNS Token Verification (`api/services/verifier.py`)

```python
# api/services/verifier.py -- entire file (17 lines)

def expected_dns_txt_token(service_id: UUID | str) -> str:
    return f"agentledger-verify={service_id}"

def verify_txt_records(service_id: UUID | str, txt_records: list[str]) -> bool:
    expected = expected_dns_txt_token(service_id).lower()
    normalized = {record.strip().strip('"').lower() for record in txt_records}
    return expected in normalized
```

The verification protocol:
1. When a service registers, AgentLedger generates a token: `agentledger-verify={service_id}`
2. The service operator adds this as a DNS TXT record on their domain
3. The crawler resolves the TXT records and checks for a match

The normalization handles real-world DNS quirks: TXT records may have surrounding quotes, extra whitespace, or inconsistent casing.

---

## Trust Tier Progression

```
Tier 1: crawled
  Registration complete, manifest fetched successfully.
  Any service starts here.
     |
     | DNS TXT record "agentledger-verify={service_id}" found
     v
Tier 2: domain_verified
  The service operator controls the domain they claim.
  This is the highest tier achievable in Layer 1.
     |
     | Capability probing would require a future opt-in protocol
     v
Tier 3: probed
  The service's capabilities have been independently tested.
     |
     | Third-party auditor attestation (Layer 3)
     v
Tier 4: attested
  An independent auditor has vouched for the service.
```

Currently, only tiers 1 and 2 are achievable. Tiers 3 and 4 are defined in the schema and ranking algorithm but populated by future layers.

---

## The Conditional Registration Pattern

Both `crawl.py` and `verify_domain.py` use the same pattern:

```python
if celery_app is not None:
    @celery_app.task(name="crawler.crawl_service")
    def crawl_service_task(service_id: str, domain: str) -> dict:
        return _crawl_service_impl(service_id, domain)
else:
    def crawl_service_task(service_id: str, domain: str) -> dict:
        return _crawl_service_impl(service_id, domain)
```

The actual logic lives in `_crawl_service_impl()` (a plain function). The Celery decorator is only applied when Celery is available. When it's not (unit tests, local dev without Redis), the same function exists as a regular callable.

This means test code can call `_crawl_service_impl()` directly without Celery infrastructure.

---

## Code Walkthrough: Manual Verification (`api/routers/verify.py`)

```python
@router.post("/services/{service_id}/verify")
async def verify_service_domain(service_id: UUID) -> dict:
    from crawler.tasks.verify_domain import _verify_domain_impl
    conn = get_sync_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT domain FROM services WHERE id = %s", (str(service_id),))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="service not found")
            domain = row[0]
    finally:
        conn.close()
    return _verify_domain_impl(domain, str(service_id))
```

This endpoint runs verification synchronously (not via Celery). It's used for testing and for operators who want immediate verification after setting up their TXT record, rather than waiting for the next beat cycle.

Note the sync psycopg2 usage inside an async route handler -- this blocks the uvicorn worker thread during the DNS lookup. For a rarely-called endpoint, this is acceptable. For high-traffic endpoints, it would need to be refactored to async.

---

## Hands-On Exercises

### Exercise 1: Check the Expected TXT Token

```python
from api.services.verifier import expected_dns_txt_token
token = expected_dns_txt_token("550e8400-e29b-41d4-a716-446655440000")
print(token)
# Expected: agentledger-verify=550e8400-e29b-41d4-a716-446655440000
```

### Exercise 2: Test Consecutive Failure Logic

```python
from crawler.tasks.crawl import should_mark_service_inactive

assert not should_mark_service_inactive(0)
assert not should_mark_service_inactive(1)
assert not should_mark_service_inactive(2)
assert should_mark_service_inactive(3)
assert should_mark_service_inactive(10)
```

### Exercise 3: Trigger Manual Verification

```powershell
# After registering a service
curl -X POST -H "X-API-Key: dev-local-only" `
  http://localhost:8000/v1/services/{service-id-here}/verify
# Expected: {"status": "pending", "txt_records_found": 0}
```

---

## Interview Prep

**Q: Why do Celery workers use psycopg2 instead of asyncpg?**

**A:** Celery workers are synchronous Python processes. They don't run an asyncio event loop, so they can't use `await` or async database drivers. psycopg2 is the standard synchronous PostgreSQL driver. Both drivers connect to the same database -- the only difference is the connection protocol. The `database_url_sync` setting provides the psycopg2-compatible URL.

---

**Q: How does AgentLedger prevent stale services from appearing in search results?**

**A:** Two mechanisms: (1) The crawler re-fetches every active service's manifest every 24 hours. After 3 consecutive fetch failures, the service is marked `is_active = false` and excluded from search results. (2) The `is_banned` flag allows manual removal. Both conditions are checked in every search query's WHERE clause.

---

**Q: What is the DNS TXT verification protocol?**

**A:** When a service registers, AgentLedger generates a token `agentledger-verify={service_id}`. The service operator adds this as a TXT record on their domain. The crawler checks daily (via Celery beat) for up to 30 days. When found, the service is promoted from trust tier 1 to tier 2 (domain_verified). This proves the registrant controls the claimed domain, similar to how Let's Encrypt validates domain ownership.

---

## Key Takeaways

- Two crawl vectors: Vector A (manifest fetch) and Vector B (DNS verification)
- Celery workers use synchronous psycopg2; FastAPI uses async asyncpg
- Beat schedule: crawl every 24h, verify every 24h, revalidate service identity every 24h, expire identities every 60s
- 3 consecutive crawl failures marks a service inactive
- DNS verification promotes trust_tier 1 to 2 within a 30-day window
- JSON-only Celery serialization prevents unsafe deserialization attacks
- Conditional task registration enables testing without Celery infrastructure
- All crawl events are logged to `crawl_events` table for audit trail

---

## Summary Reference Card

| Component | Task Name | Schedule | Purpose |
|-----------|-----------|----------|---------|
| `crawl.py` | `crawler.crawl_all` | Every 24h | Re-fetch all active manifests |
| `crawl.py` | `crawler.crawl_service` | On demand | Crawl single service |
| `verify_domain.py` | `crawler.verify_all_pending` | Every 24h | Check DNS TXT for tier-1 services |
| `verify_domain.py` | `crawler.verify_domain` | On registration | Check DNS TXT for single service |
| `verifier.py` | (not a task) | -- | Token generation and matching |
| `verify.py` | (router) | -- | Manual verification endpoint |

---

## Ready for Lesson 08?

Next up, we'll explore **The Bouncer** -- the rate limiting middleware that protects AgentLedger from abuse, the typosquat detector that catches impersonation attempts, and the hardening measures that got the system to POC readiness.

*Remember: The crawler is the immune system. Without it, the registry fills with dead links and unverified claims. Trust without verification is just hope!*
