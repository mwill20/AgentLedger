# Lesson 58: Hardening the Trust Layer — Rate Limits, Caching & Load Testing

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/services/liability_claims.py`, `api/services/liability_snapshot.py`, `spec/LAYER6_COMPLETION.md`
**Prerequisites:** Lesson 57
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A courthouse doesn't let anyone file unlimited lawsuits. Filing fees, standing requirements, and docket management prevent the court system from being weaponized as a harassment tool. Layer 6's hardening design plays the same role: it limits claim filing rates to prevent the evidence-gathering pipeline from being used as a denial-of-service vector, caches claim status to absorb polling traffic, and was load-tested at 100 concurrent users to confirm the p95 < 200ms acceptance target.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain the claim filing rate limit: threshold, window, key design, and fail-open rationale
- Describe the Redis claim status cache: TTL, invalidation pattern, and fail-open behavior
- Explain the five key design decisions from the Layer 6 completion document
- Describe the Layer 6 load test configuration and results
- Name the two hardening differences between Layer 5 (workflow query rate limit) and Layer 6 (claim filing rate limit)

---

## The Claim Filing Rate Limit

```python
# api/services/liability_claims.py:28–30
CLAIM_FILING_RATE_LIMIT = 10
CLAIM_FILING_RATE_WINDOW_SECONDS = 3600

def claim_rate_limit_key(claimant_did: str) -> str:
    digest = sha256(claimant_did.encode("utf-8")).hexdigest()
    return f"liability:claim_rate:{digest}"
```

**Target: claimant DID, not IP address.** Agent platforms that file claims on behalf of multiple agents would share the same IP. A per-IP limit would punish legitimate multi-agent platforms. Per-claimant-DID limits target the right unit of accountability.

**10 claims per hour per DID.** Evidence gathering runs 8 DB queries per claim. At 10 claims/hour, a single claimant generates 80 DB queries per hour — negligible. At 1,000 claims/hour (no rate limit), a single claimant could generate 8,000 DB queries/hour, potentially saturating the DB connection pool.

**DID is hashed.** Agent DIDs can be long strings (did:key DIDs are ~50 characters; did:web DIDs can be longer). Hashing with SHA-256 produces a fixed-length key that doesn't expose DIDs in Redis keyspace and cannot be enumerated via `SCAN`.

**Fail-open.** If Redis is unavailable, the rate limit is bypassed. Claim filing with evidence gathering is write-heavy — excess DB load (from unbounded claims without rate limiting) is the worst-case consequence of a Redis outage, not a security breach. Availability of the dispute process is prioritized over the rate limit enforcement.

---

## Comparison: Layer 5 vs. Layer 6 Rate Limits

| Dimension | Layer 5 (workflow query) | Layer 6 (claim filing) |
|-----------|------------------------|----------------------|
| Target | API key | Claimant DID |
| Threshold | 200 queries / 60 seconds | 10 filings / 3600 seconds |
| Key | `sha256(api_key)` | `sha256(claimant_did)` |
| Fail-open | Yes | Yes |
| Rationale | Prevents search scraping (read-only) | Prevents evidence-gathering abuse (write-heavy) |

Layer 5's 200/60s reflects a read-only surface that can handle bursts. Layer 6's 10/3600 reflects that each claim filing triggers expensive background evidence gathering. Lower threshold, longer window.

---

## The Redis Claim Status Cache

```python
# Cache key: liability:claim_status:{claim_id}
# TTL: 60 seconds
CLAIM_STATUS_CACHE_TTL_SECONDS = 60
```

**Why cache claim status?** Compliance dashboards and automated claim tracking systems poll `GET /liability/claims/{claim_id}` frequently — checking whether evidence gathering has completed, whether a determination has been made. The full claim detail response (with evidence and determination) requires 3 DB queries. A status-only cache reduces the polling cost to a single Redis GET.

**Cache invalidation pattern:**
```python
async def refresh_claim_status_cache(redis, claim_id, claim_status):
    await invalidate_claim_status_cache(redis, claim_id)  # DELETE
    await cache_claim_status(redis, claim_id, claim_status)  # SETEX 60s
```

The invalidate-then-set pattern is used (rather than SET with TTL). DELETE + SETEX is two operations but ensures the old value is gone before the new one is written. A brief gap between the two where the key doesn't exist is acceptable — callers fall through to the database for a single query.

**Fail-open.** All Redis operations are wrapped in try/except. If Redis returns an error, the function returns silently — the claim is still persisted in the database, and the next read hits the DB directly. This is the same fail-open pattern used in Layers 3–5.

---

## The Five Key Design Decisions

From `spec/LAYER6_COMPLETION.md §5`:

| Decision | Rationale |
|----------|-----------|
| **Snapshots are synchronous** | Trust scores in `services` are overwritten by crawl cycles. Sync creation captures state before the 201 response returns — closing the timing attack window |
| **Attribution gaming is self-defeating** | Using an undertrusted service shifts weight TO the agent, not away from them. The system cannot be gamed by choosing bad services |
| **Evidence records copy raw data at gather time** | Source records can be modified or GDPR-erased after the claim is filed; forensic copies are immune to post-hoc changes |
| **GDPR-erased disclosures produce tombstone evidence records** | The fact of the disclosure is preserved; its content is not. Absence would mislead attribution |
| **Layer 6 is evidence infrastructure, not adjudication** | Avoids regulated financial/legal entity classification |

---

## Layer 6 Load Test Results

Acceptance criterion 10: `GET /liability/snapshots/{execution_id}` p95 < 200ms @ 100 concurrent requests.

**Test configuration:**
- Profile: `layer6`
- Users: 100 concurrent
- Target endpoint: `GET /liability/snapshots/{execution_id}`
- Single seeded execution with a pre-created snapshot
- Duration: 30 seconds

**Results (from LAYER6_COMPLETION.md):**

| Metric | Value |
|--------|-------|
| Total requests | (session-specific) |
| Failures | 0 |
| **p95** | **< 200ms (target met)** |

**Why snapshot reads are fast:** The snapshot read is a single SELECT by UUID primary key — a B-tree index lookup on `liability_snapshots.execution_id` (which has a `UNIQUE` constraint, equivalent to a unique index). No joins, no aggregations. At 100 concurrent users hitting the same snapshot row, the DB connection pool handles all requests against a hot page in the PostgreSQL buffer cache.

---

## What Happens Under Sustained Claim Abuse

If a malicious actor bypasses the rate limit (e.g., with 1000 different claimant DIDs, each filing 10 claims/hour):

1. **Evidence gathering is idempotent** — re-gathering the same execution attaches no new evidence records (`ON CONFLICT DO NOTHING`). Duplicate claims against the same execution don't amplify DB load multiplicatively.

2. **Claim deduplication** — the `(execution_id, claimant_did, claim_type)` unique check returns 409 for duplicate filings. An attacker with one DID can file at most 5 distinct claim types per execution.

3. **Evidence gathering is bounded** — 8 sources × 1 DB query each = 8 queries per claim, regardless of execution complexity. The cost is bounded per claim.

4. **The rate limit is the primary defense** — 1000 DIDs filing at max rate = 10,000 claims/hour = 80,000 evidence queries/hour. This is the expected worst-case without additional defenses. A real deployment would add IP-level or organization-level rate limiting at the API gateway.

---

## Exercise 1 — Observe Rate Limiting

File claims rapidly to trigger the rate limit:

```bash
EXECUTION_ID="<execution-uuid>"
for i in $(seq 1 12); do
  CLAIM_TYPE=$([ $((i % 5)) -eq 0 ] && echo "data_misuse" || echo "service_failure")
  curl -s -X POST "http://localhost:8000/v1/liability/claims" \
    -H "X-API-Key: dev-local-only" \
    -H "Content-Type: application/json" \
    -d "{
      \"execution_id\": \"$EXECUTION_ID\",
      \"claimant_did\": \"did:key:z6MkRateLimitTestAgent\",
      \"claim_type\": \"$CLAIM_TYPE\",
      \"description\": \"Rate limit test claim $i\"
    }" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('status', d.get('detail', 'error')))"
done
```

**Expected:** First 10 succeed (`filed`); request 11 returns `429` with "claim filing rate limit exceeded".

---

## Exercise 2 — Inspect the Rate Limit Key in Redis

```bash
docker exec agentledger-redis-1 redis-cli KEYS "liability:claim_rate:*"
docker exec agentledger-redis-1 redis-cli TTL "$(docker exec agentledger-redis-1 redis-cli KEYS 'liability:claim_rate:*' | head -1)"
```

**Expected:** One key with TTL close to 3600 seconds (1 hour), representing the DID's rolling window.

---

## Exercise 3 — Load Test the Snapshot Endpoint

If locust is installed:

```bash
# Run 30-second, 100-user load test against the snapshot endpoint
locust --headless \
  --host http://localhost:8000 \
  --users 100 --spawn-rate 20 \
  --run-time 30s \
  -f tests/load/locustfile.py \
  --only-summary 2>&1 | grep -E "(p95|failures|requests)"
```

**Expected:** p95 ≤ 200ms, 0 failures.

---

## Best Practices

**Rate limit at the right granularity.** The claim filing rate limit targets claimant DID — the correct level for controlling evidence-gathering abuse. But a deployment with untrusted agents should add a second rate limit at the API gateway level (by IP or organization) to prevent 1000-DID attacks.

**The 60s claim status cache TTL matches the Layer 4 and Layer 5 cache TTLs.** Consistent TTLs across layers make the system's staleness budget predictable: consumers can assume any cached data is at most 60 seconds old, regardless of which layer it came from.

---

## Interview Q&A

**Q: Why is claim filing rate-limited but snapshot reads are not?**
A: Claim filing triggers evidence gathering (8 DB queries, background task scheduling). Snapshot reads are single indexed SELECT queries — fast enough that rate limiting would add complexity without meaningfully protecting the database. Rate limiting is applied where the cost per request is high enough to create an abuse vector.

**Q: What is the worst-case scenario if the claim filing rate limit is disabled entirely?**
A: A malicious actor creates 10,000 agent DIDs and files 10 claims per DID against the same execution every hour. Each claim triggers 8 evidence queries, for 800,000 evidence queries/hour. With a typical DB connection pool of 20 connections, this would saturate the pool and cause connection timeouts for legitimate requests across all layers. The rate limit prevents this DoS pattern at the cost of rejecting excess legitimate filings (which, in practice, no legitimate compliance workflow would hit).

**Q: Why doesn't Layer 6 cache the full claim detail response (evidence + determination) rather than just the status?**
A: Evidence records are never deleted and determinations are append-only — the full claim detail for a resolved claim is stable. But claims in earlier states (`filed`, `evidence_gathered`) have evidence that grows with each re-gather. Caching a growing list would require either complex invalidation on each evidence insert or accepting stale evidence lists. Status-only caching avoids this: the status is a simple string that changes at well-defined transition points.

---

## Key Takeaways

- Claim filing rate limit: 10/hour per claimant DID (hashed), fail-open, targets write-heavy evidence gathering
- Layer 6 rate limit is 20× more restrictive than Layer 5 (10/3600 vs. 200/60) because claim filing is write-heavy
- Redis claim status cache: 60s TTL, invalidate-then-set on each transition, fail-open
- Five key design decisions: sync snapshots, self-defeating attribution gaming, forensic evidence copies, GDPR tombstones, evidence-not-adjudication
- Snapshot read load test: p95 < 200ms @ 100 concurrent (single indexed SELECT by execution_id)
- Sustained abuse defense: idempotent evidence gathering + claim deduplication + bounded 8-query cost per claim

---

## Next Lesson

**Lesson 59 — The Database Foundation: Migration 007 & Layer 6 Schema** covers the five Layer 6 tables in `007_layer6_liability.py` — their constraints, indexes, and how the append-only design prevents evidence tampering.
