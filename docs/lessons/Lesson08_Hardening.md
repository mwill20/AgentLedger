# Lesson 08: The Bouncer -- Rate Limiting, Typosquat Detection, and Hardening

> **Beginner frame:** Hardening is the set of guardrails that keeps a useful API from becoming an easy target. Rate limits slow abuse, typosquat checks flag suspicious lookalikes, and sanitization keeps unsafe input away from deeper systems.

## Welcome Back, Systems Engineer!

The API works. Services register, search returns results, the crawler verifies domains. But what stops a malicious actor from flooding the API with 10,000 requests per second? What prevents `g00gle.com` from impersonating `google.com`? Today we dissect the **hardening layer** -- rate limiting, typosquat detection, and the security decisions that make AgentLedger POC-hardened.

**Goal:** Understand the pure ASGI rate limiting middleware, the Levenshtein-based typosquat detector, and why each security decision was made.
**Time:** 60 minutes
**Prerequisites:** Lessons 01-07
**Why this matters:** Security bugs are the most expensive to fix after deployment. Understanding the hardening layer means understanding why each defense exists and what it protects against.

---

## Learning Objectives

- Explain why pure ASGI middleware was chosen over Starlette's BaseHTTPMiddleware
- Trace a request through the two-layer rate limiting system (IP + API key)
- Understand the Redis pipeline optimization for per-IP rate limiting
- Explain the Levenshtein distance algorithm and how it detects typosquats
- Describe the fail-open design philosophy and when it's appropriate
- Identify the exempt paths and explain why they're exempt

---

## File Map

```
api/
|-- ratelimit.py              # Rate limiting middleware (249 lines)
|-- services/
|   |-- typosquat.py          # Typosquat detection (113 lines)
|-- models/
    |-- sanitize.py           # Input sanitization (49 lines, covered in Lesson 04)
```

---

## Code Walkthrough: Rate Limiting (`api/ratelimit.py`)

### Why Pure ASGI?

The rate limiter was originally built using Starlette's `BaseHTTPMiddleware`. During Phase 5 load testing, this was identified as the primary performance bottleneck.

The problem: `BaseHTTPMiddleware` spawns a background thread for every request and buffers the entire response body. At 100 concurrent users, this creates 100+ threads competing for the GIL, plus memory overhead from response buffering.

The solution: Pure ASGI middleware. Instead of inheriting from `BaseHTTPMiddleware`, the middleware directly implements the ASGI protocol (`__call__(self, scope, receive, send)`). This eliminates the thread-per-request overhead entirely.

### The Middleware Class

```python
class RateLimitMiddleware:
    """Pure ASGI rate-limiting middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = _parse_scope_path(scope)

        # Skip rate limiting for exempt paths
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # --- Per-IP check ---
        from api.dependencies import redis_client
        client_ip = _get_client_ip(scope)
        ip_allowed, ip_remaining, ip_retry_after = await _check_ip_rate_limit(
            redis_client, client_ip
        )

        if not ip_allowed:
            await _send_json_response(send, 429, {"detail": "IP rate limit exceeded"}, {
                "Retry-After": str(ip_retry_after),
                "X-RateLimit-Limit": str(IP_RATE_LIMIT),
                "X-RateLimit-Remaining": "0",
            })
            return

        # --- Per-API-key quota check ---
        api_key = _get_header(scope, b"x-api-key")
        if api_key:
            from api.dependencies import async_session_factory
            key_allowed, key_retry_after = await _check_api_key_quota(
                async_session_factory, api_key
            )
            if not key_allowed:
                await _send_json_response(send, 429,
                    {"detail": "API key quota exhausted"},
                    {"Retry-After": str(key_retry_after or 86400)},
                )
                return

        # --- Proceed with rate limit headers ---
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-ratelimit-limit", str(IP_RATE_LIMIT).encode()))
                headers.append((b"x-ratelimit-remaining", str(ip_remaining).encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
```

Step by step:

1. **Non-HTTP scope bypass** -- WebSocket connections and lifespan events pass through untouched.
2. **Exempt paths** -- `/v1/health`, `/docs`, `/openapi.json`, `/redoc` skip rate limiting. Health checks must always work (monitoring depends on it). Docs are read-only static content.
3. **Per-IP check** -- Redis-backed, 100 requests per 60-second window.
4. **Per-API-key check** -- Database-backed monthly quota for DB-provisioned keys.
5. **Header injection** -- `send_with_headers` wraps the send callable to inject `X-RateLimit-Limit` and `X-RateLimit-Remaining` headers into every successful response.

### The ASGI Helpers

```python
def _parse_scope_path(scope: dict) -> str:
    return scope.get("path", "/")

def _get_client_ip(scope: dict) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"

def _get_header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None
```

In ASGI, headers are a list of `(bytes, bytes)` tuples, not a dict. The manual iteration in `_get_header` is O(n) but n is typically small (10-20 headers). Header names are compared as bytes for performance.

### Per-IP Rate Limiting with Redis Pipeline

```python
async def _check_ip_rate_limit(redis_client, client_ip):
    if redis_client is None or redis_client.__class__.__name__ == "NullRedisClient":
        return True, IP_RATE_LIMIT, 0
    if IP_RATE_LIMIT <= 0:
        return True, 0, 0

    key = f"ratelimit:ip:{client_ip}"
    try:
        pipe = redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, IP_RATE_WINDOW_SECONDS)
        pipe.ttl(key)
        current, _expire_ok, ttl = await pipe.execute()

        remaining = max(0, IP_RATE_LIMIT - current)
        if current > IP_RATE_LIMIT:
            return False, 0, max(1, ttl)
        return True, remaining, 0
    except Exception:
        return True, IP_RATE_LIMIT, 0
```

The Redis pipeline is a critical optimization. Three operations in one network round-trip:

1. **INCR** -- Atomically increment the counter for this IP
2. **EXPIRE** -- Set/refresh the 60-second TTL on every request in the window
3. **TTL** -- Get remaining seconds until the window resets (used for Retry-After header)

Before this optimization, these were three sequential `await` calls -- three network round-trips per request. The pipeline reduces this to one, which was essential for meeting the <500ms p95 target at 100 concurrent users.

**NullRedisClient detection**: The check `redis_client.__class__.__name__ == "NullRedisClient"` handles the case where Redis isn't available. The `NullRedisClient` (from dependencies.py) doesn't support `pipeline()`, so we skip rate limiting entirely.

**Fail-open**: If Redis raises any exception, the function returns `(True, IP_RATE_LIMIT, 0)` -- the request is allowed. This is deliberate: Redis downtime should not prevent the API from serving requests.

### Per-API-Key Quota

```python
async def _check_api_key_quota(session_factory, api_key):
    if session_factory is None:
        return True, None

    # Config-based keys bypass DB check
    configured = {k.strip() for k in settings.api_keys.split(",") if k.strip()}
    if api_key in configured:
        return True, None

    key_hash = sha256(api_key.encode("utf-8")).hexdigest()

    try:
        async with session_factory() as session:
            result = await session.execute(
                text("SELECT query_count, monthly_limit, is_active FROM api_keys WHERE key_hash = :key_hash"),
                {"key_hash": key_hash},
            )
            row = result.mappings().first()
            if row is None:
                return True, None  # Not in DB -- auth will reject

            if not row["is_active"]:
                return False, None

            if row["monthly_limit"] is not None and row["query_count"] >= row["monthly_limit"]:
                # Calculate retry-after as remaining days in month
                remaining_days = days_in_month - now.day + 1
                return False, remaining_days * 86400

            # Increment and allow
            await session.execute(
                text("UPDATE api_keys SET query_count = query_count + 1, last_used_at = NOW() WHERE key_hash = :key_hash"),
                {"key_hash": key_hash},
            )
            await session.commit()
            return True, None
    except Exception:
        return True, None  # fail-open
```

Two paths:

1. **Config-based keys** (from `API_KEYS` env var) -- bypass quota entirely. These are developer keys, not metered.
2. **DB-backed keys** (from `api_keys` table) -- have `monthly_limit` and `query_count`. When the count reaches the limit, requests are rejected with a `Retry-After` header.

The key is stored as a SHA-256 hash in the database, not in plaintext. This means even if the database is compromised, API keys aren't exposed.

### Response Building for 429s

```python
async def _send_json_response(send, status_code, body, extra_headers=None):
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
```

In pure ASGI, responses are sent as two messages: `http.response.start` (status + headers) and `http.response.body` (the payload). This function builds both messages manually, which is why the middleware doesn't need Starlette's response classes.

---

## Code Walkthrough: Typosquat Detection (`api/services/typosquat.py`)

### The Levenshtein Distance Algorithm

```python
def levenshtein_distance(s1: str, s2: str) -> int:
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    # Ensure s1 is the shorter string for space optimization
    if len(s1) > len(s2):
        s1, s2 = s2, s1

    previous_row = list(range(len(s1) + 1))

    for j, c2 in enumerate(s2):
        current_row = [j + 1]
        for i, c1 in enumerate(s1):
            cost = 0 if c1 == c2 else 1
            current_row.append(
                min(
                    current_row[i] + 1,       # insertion
                    previous_row[i + 1] + 1,  # deletion
                    previous_row[i] + cost,   # substitution
                )
            )
        previous_row = current_row

    return previous_row[-1]
```

The Levenshtein distance counts the minimum number of single-character edits (insertions, deletions, substitutions) to transform one string into another.

The implementation uses a standard dynamic programming approach with a single-row space optimization:
- Standard DP uses an O(n*m) matrix. This version uses O(min(n,m)) space by only keeping the previous row.
- The strings are swapped so `s1` is always shorter, minimizing memory usage.

### Domain Base Extraction

```python
def _extract_domain_base(domain: str) -> str:
    parts = domain.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[:-1])
    return domain.lower()
```

This strips the TLD before comparison. Why? Consider:
- `example.com` vs `example.org` -- distance 2 on full strings, but these are legitimately different registrations.
- `flightbooker.com` vs `f1ightbooker.com` -- distance 1 on the base, which is a genuine typosquat.

By comparing `flightbooker` vs `f1ightbooker` (without TLD), we avoid false positives from TLD differences.

### Finding Similar Domains

```python
TYPOSQUAT_MAX_DISTANCE = 2

def find_similar_domains(
    candidate_domain: str,
    existing_domains: list[str],
    max_distance: int = TYPOSQUAT_MAX_DISTANCE,
    max_matches: int = 5,
) -> list[dict[str, str | int]]:
    candidate_base = _extract_domain_base(candidate_domain)
    matches = []

    for existing in existing_domains:
        if existing.lower() == candidate_domain.lower():
            continue  # exact match is an update, not a typosquat

        existing_base = _extract_domain_base(existing)

        # Quick length check
        if abs(len(candidate_base) - len(existing_base)) > max_distance:
            continue

        distance = levenshtein_distance(candidate_base, existing_base)
        if distance <= max_distance:
            matches.append({"domain": existing, "distance": distance})

    matches.sort(key=lambda item: (int(item["distance"]), str(item["domain"])))
    return matches[:max_matches]
```

Key optimizations:

1. **Exact match skip** -- If the domain matches exactly, it's an update to an existing registration, not a typosquat.
2. **Length pre-check** -- If the length difference exceeds `max_distance`, the Levenshtein distance must be at least that large. This skips the O(n*m) computation for obviously dissimilar strings.
3. **Sorted results** -- Closest matches first (by distance), then alphabetically for ties.
4. **Max 5 matches** -- Prevents overwhelming the response with warnings.

### Typosquat Patterns Detected

| Pattern | Example | Distance |
|---------|---------|----------|
| Character substitution | flightbooker vs f1ightbooker | 1 |
| Character insertion | paypal vs paypall | 1 |
| Character deletion | google vs gogle | 1 |
| Adjacent transposition | amazon vs amzaon | 2 |
| Two substitutions | example vs exampie | 1 |

Distance 2 catches most common typosquatting attacks while keeping false positives manageable.

---

## Input Sanitization Recap

Covered in detail in Lesson 04, but here's how it fits into the hardening picture:

- **Null byte rejection** (`check_null_bytes_recursive`) -- Prevents SQL injection through null bytes, log injection, and path traversal. PostgreSQL TEXT columns reject null bytes at the driver level, but catching them at the model layer produces a clear 422 error.
- **Whitespace stripping** (`strip_strings_recursive`) -- Prevents subtle matching bugs where `"travel.air.book "` fails to match `"travel.air.book"`.
- Both run in `mode="before"` model validators, before Pydantic parses field types.

---

## The Fail-Open Philosophy

AgentLedger's rate limiting is explicitly fail-open:

```python
# Redis fails? Allow the request.
except Exception:
    return True, IP_RATE_LIMIT, 0

# DB fails? Allow the request.
except Exception:
    return True, None
```

Why fail-open instead of fail-closed?

1. **AgentLedger is an agent registry.** If an AI agent can't discover services because Redis is down, that's worse than allowing a few extra requests through.
2. **Defense in depth.** Rate limiting isn't the only defense. Auth (API keys) is a separate layer. Input validation is a separate layer. The database has its own constraints.
3. **Observability.** In the current middleware helpers, Redis/DB failures are treated as fail-open and returned silently from this path. That keeps the hot path simple, but it also means operators need visibility from other telemetry rather than relying on these helpers to emit logs.

The opposite choice (fail-closed) would be appropriate for, say, a payment processing API where allowing unbounded requests could cause financial harm.

---

## Hands-On Exercises

### Exercise 1: Test Rate Limiting on a Protected Route

```powershell
# Send 101 requests rapidly
for ($i=0; $i -lt 101; $i++) {
    $response = curl.exe -s -o $null -w "%{http_code}" `
        -H "x-api-key: dev-local-only" `
        http://localhost:8000/v1/ontology
    if ($response -eq "429") {
        Write-Host "Rate limited at request $i"
        break
    }
}
```

Use a non-exempt route for this exercise. `/v1/health` is intentionally excluded from rate limiting, so it should never return `429`.

### Exercise 2: Test Typosquat Detection

```python
from api.services.typosquat import levenshtein_distance, find_similar_domains

# Distance calculations
assert levenshtein_distance("google", "gogle") == 1
assert levenshtein_distance("paypal", "paypall") == 1
assert levenshtein_distance("amazon", "amzaon") == 2
assert levenshtein_distance("totally", "different") == 7

# Find similar domains
existing = ["flightbooker.com", "paygate.io", "example.com"]
matches = find_similar_domains("f1ightbooker.com", existing)
print(matches)
# Expected: [{'domain': 'flightbooker.com', 'distance': 1}]
```

### Exercise 3: Observe Rate Limit Headers

```powershell
curl -v -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
# Look for in response headers:
# X-RateLimit-Limit: 100
# X-RateLimit-Remaining: 99
```

---

## Interview Prep

**Q: Why did AgentLedger switch from BaseHTTPMiddleware to pure ASGI middleware?**

**A:** BaseHTTPMiddleware spawns a background thread per request and buffers the entire response body. At 100 concurrent users, this creates 100+ threads competing for the GIL, plus memory overhead. The pure ASGI middleware processes requests in the asyncio event loop without threads or response buffering, which eliminated the primary performance bottleneck during Phase 5 load testing.

---

**Q: How does the per-IP rate limiter work?**

**A:** It uses a Redis-backed sliding window. Each IP gets a counter key (`ratelimit:ip:{ip}`) with a 60-second TTL. On each request, a Redis pipeline atomically: (1) increments the counter, (2) refreshes the TTL, and (3) reads the remaining TTL. If the counter exceeds 100, the request gets a 429 with a Retry-After header. The pipeline ensures all three operations happen in a single network round-trip.

---

**Q: Why is typosquat detection advisory instead of blocking?**

**A:** Because legitimate domains can be similar. `airbnb-flights.com` might be a legitimate competitor to `airbnb.com`, not a typosquat. Blocking registrations based on string similarity would create false positives. Instead, warnings are returned in the response and logged for review. The service operator can decide whether the similarity is concerning.

---

## Key Takeaways

- Pure ASGI middleware eliminates the thread-per-request overhead of BaseHTTPMiddleware
- Two rate limiting layers: per-IP (Redis, 100/min) and per-API-key (DB, monthly quota)
- Redis pipeline reduces three round-trips to one for IP rate limiting
- Fail-open design: Redis/DB failures allow requests through (availability over strictness)
- Levenshtein distance with max_distance=2 catches common typosquat patterns
- Domain base extraction prevents false positives from TLD differences
- Exempt paths (/health, /docs) skip rate limiting -- monitoring must always work
- API keys are stored as SHA-256 hashes in the database

---

## Summary Reference Card

| Defense | Mechanism | Threshold | Behavior on Failure |
|---------|-----------|-----------|-------------------|
| Per-IP rate limit | Redis INCR + EXPIRE | 100 req/60s | Fail-open (allow) |
| Per-key quota | DB query_count | monthly_limit | Fail-open (allow) |
| Typosquat detection | Levenshtein distance | distance <= 2 | Advisory warning |
| Null byte rejection | Recursive string scan | Any \x00 | 422 rejection |
| Whitespace stripping | Recursive strip() | All strings | Auto-cleaned |
| FQDN validation | Regex (RFC 1035) | Invalid format | 422 rejection |
| Exempt paths | Set membership | /health, /docs, etc. | Skip rate limiting |

---

## Ready for Lesson 09?

Next up, we'll explore **The Proving Ground** -- the testing strategy that achieved 80%+ coverage, the shared fixtures that enable fast isolated tests, and the Locust load testing harness that validated <500ms p95 at 100 concurrent users.

*Remember: Hardening is what separates a demo from a production system. Every decision here -- fail-open, advisory warnings, ASGI middleware -- was made with a specific threat model in mind!*
