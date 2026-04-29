# Lesson 40: The Stress Test — Hardening, Caching, Rate Limiting & Interview Readiness

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_matcher.py`, `api/services/context_profiles.py`, `api/services/context_disclosure.py`, `spec/LAYER4_SPEC.md`  
**Prerequisites:** Lessons 31–39  
**Estimated time:** 90 minutes

---

## Welcome Back, Agent Architect!

You've traced every flow in Layer 4: profiles, mismatch detection, the matching engine, HMAC commitments, selective disclosure, audit trail, and compliance export. Now comes the final test — not "does it work?" but "does it hold under adversarial pressure?"

This lesson examines every hardening decision in Layer 4: what caches exist and why, what rate limits protect what resources, the four threat surfaces this layer was designed to resist, and the canonical interview questions a senior reviewer will ask about the design.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Describe every Redis cache in Layer 4: key, TTL, and invalidation strategy
- Explain the per-agent match rate limit and why it targets agent_did not IP
- Trace all four Layer 4 threats and the code that mitigates each
- Explain the session assertion fallback mode and its security implications
- Recite the Layer 4 invariant and explain why all three parts are necessary
- Answer the five canonical interview questions about Layer 4 design

---

## The Caching Strategy

Layer 4 uses Redis for three distinct caches. Each has a different TTL and a different invalidation strategy.

### 1. Profile cache — `context:profile:{agent_did}` (60s TTL)

```python
# api/services/context_profiles.py
async def get_active_profile(db, agent_did, redis=None):
    key = f"context:profile:{agent_did}"
    cached = await _cache_get_profile(redis, key)
    if cached:
        return cached
    # ... DB query ...
    await _cache_set_profile(redis, key, result, ttl=60)
```

**Why 60 seconds?** Profile updates are infrequent but security-sensitive. A stale profile could allow or block context that the agent just changed. 60 seconds is the maximum acceptable window. Shorter TTLs (e.g., 5s) would increase DB load for high-traffic agents; longer (e.g., 300s) would make profile updates feel unresponsive.

**Invalidation:** `update_active_profile()` calls `_cache_invalidate_profile()` — explicit key deletion on write. The 60s TTL is the fallback for cases where the invalidation call fails (e.g., Redis briefly unavailable).

### 2. Match result cache — `context:match:{match_id}` (300s TTL)

```python
# api/services/context_matcher.py
await redis.set(
    f"context:match:{match_id}",
    response.model_dump_json(),
    ex=300
)
```

**Why 300 seconds?** The disclose phase needs the match snapshot. The commitment TTL is also 5 minutes. Aligning the match cache TTL to the commitment TTL means the cache is useful for exactly as long as the commitments are valid — never longer.

**Invalidation:** None needed. The `match_id` is a UUID generated per-match — there is only ever one version of each match result. After 5 minutes, both the cache and the commitments have expired. The commitment-row fallback handles the case where Redis evicts the key before TTL.

### 3. Rate limit counter — `ratelimit:match:{agent_did}` (60s TTL)

```python
await redis.incr(f"ratelimit:match:{agent_did}")
await redis.expire(f"ratelimit:match:{agent_did}", 60)  # if new key
```

**Why not use a sliding window?** A fixed 60-second window (reset at key creation, not at each request) is simpler and sufficient. The goal is to prevent DoS via match flooding — exact burst semantics are not required.

**Best-effort only:** If Redis is unavailable, `_enforce_match_rate_limit()` swallows the exception and the match proceeds. Rate limiting is a performance protection, not a security gate. The security gates (session verification, trust checks) do not depend on Redis.

---

## The Rate Limit Design

### Why per `agent_did`, not per IP?

A single IP might proxy thousands of agents (enterprise API gateway, cloud agent platform). Rate limiting per IP would punish legitimate users of multi-tenant platforms. Rate limiting per `agent_did` targets the right unit: one agent cannot flood the system, but 10,000 agents behind the same gateway each get their own limit.

### The 100 matches / 60 seconds threshold

100 matches per minute = 1.67 matches per second. A single agent in a complex workflow might call match once per step, with steps taking 100ms+ each. 100/minute leaves room for burst (e.g., a 10-step workflow that pre-matches all steps) while blocking automated scanning.

### What the rate limit does NOT protect

The rate limit targets the match endpoint specifically — the most expensive step (8 DB queries, Redis reads, session verification). The disclose endpoint has no rate limit because it is always preceded by a match (which was already rate-limited). Profile reads are cached, so rate-limiting them would have minimal effect.

---

## The Four Layer 4 Threats

These extend the Layer 1–3 threat model (14 threats). Layer 4 adds 4 more.

### Threat 15: Context Over-Harvesting
**Attack:** Service requests fields beyond its declared manifest to build a richer user profile than authorised.  
**Severity:** High  
**Mitigation:** `detect_mismatch()` catches every over-requested field. Critical mismatches (tier-3+ fields) can trigger Layer 3 revocation via `resolve_mismatch(escalate_to_trust=True)`.  
**Code:** `context_mismatch.py:78–99`, `context_matcher.py` step 4

### Threat 16: Profile Bypass
**Attack:** Service manipulates trust score or service metadata between match and disclose to access fields it was not authorised for at match time.  
**Severity:** Critical  
**Mitigation:** Disclose phase re-queries service trust state and re-enforces the trust gate. A trust drop between match and disclose raises 403.  
**Code:** `context_disclosure.py` step 4, approximately line 545

### Threat 17: Commitment Replay
**Attack:** Service captures a commitment hash and attempts to use it against a different agent or in a different session to obtain a nonce.  
**Severity:** High  
**Mitigation:** Commitment rows are scoped to `(match_id, agent_did, service_id)`. The disclose query includes all three as filters. A commitment from agent A cannot be disclosed by agent B.  
**Code:** `context_disclosure.py:519–569`, the WHERE clause in step 2

### Threat 18: Audit Trail Forgery
**Attack:** Operator modifies `context_disclosures` rows to hide a disclosure from the compliance PDF.  
**Severity:** Medium  
**Mitigation:** `context_disclosures` is append-only — no DELETE operation is exposed. `revoke_disclosure()` uses UPDATE to set `erased=true` but cannot delete. An operator with direct DB access could still modify rows — but this is outside the application threat model.  
**Code:** `context_disclosure.py:657–704`

---

## Session Assertion Fallback Mode

The matching engine includes a fallback for session assertion verification (`context_matcher.py` ~lines 48–63):

```python
try:
    claims = await _verify_session_assertion(db, request.session_assertion)
    fallback_mode = False
except Exception:
    # Phase 3 stub: decode without signature verification
    claims = await _decode_unverified_session_assertion(request.session_assertion)
    fallback_mode = True
```

**Security implications of fallback mode:**
- The `sub` claim is trusted without signature verification — an attacker could craft a JWT claiming any agent DID
- Match results under fallback mode set `verified=false` in the audit record
- The fallback was added to allow Layer 4 to operate when Layer 2 is temporarily unavailable

**In production:** Fallback mode should be disabled via configuration once Layer 2 is fully operational. The spec notes this as a Phase 3 design decision, not a permanent feature.

---

## The Three-Part Invariant — A Final Statement

```
context flows only when:
  (1) manifest declared it      → detect_mismatch() confirms the field is in declared_required ∪ declared_optional
  (2) user profile permits it   → evaluate_profile() returns 'permit' or 'commit'
  (3) service trust clears threshold → _check_trust_threshold() confirms trust_tier ≥ required_tier
```

**Why all three are necessary:**

Remove (1): A service can request any field, even fields it has no business purpose for. Over-harvesting becomes trivial.

Remove (2): The agent's preferences are irrelevant — every declared field flows to every service regardless of what the agent specified. Profiles become decorative.

Remove (3): A low-trust service with no attestation can receive tier-4 fields (SSN, medical records). Layer 3's entire trust computation is bypassed.

The three conditions are not redundant — each protects against a different attack class.

---

## Layer 4 Test Coverage

| File | Tests | Coverage scope |
|------|-------|----------------|
| `test_context_profiles.py` | ~8 | Profile CRUD, rule sort order, cache behaviour |
| `test_context_matcher.py` | ~10 | Full 8-step flow, trust gates, profile evaluation |
| `test_context_mismatch.py` | ~6 | Detection, severity, escalation path |
| `test_context_disclosure.py` | ~10 | Commitment generation, nonce release, expiry, replay rejection |
| `test_context_compliance.py` | ~4 | PDF generation, erased row rendering |
| `test_context_hardening.py` | ~6 | Rate limit, cache TTL, fallback mode |

Run all Layer 4 tests:
```bash
pytest tests/test_api/test_context_*.py -q
```

---

## Hardening Exercise — Threat Walkthrough

For each of the four threats, write the SQL query that would catch a violation in a production database audit:

**Threat 15 (Over-harvesting):**
```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT service_id, COUNT(*) as mismatches, MAX(severity) as max_severity
      FROM context_mismatch_events
      WHERE created_at > NOW() - INTERVAL '24 hours'
      GROUP BY service_id
      ORDER BY mismatches DESC LIMIT 10;"
```

**Threat 16 (Profile bypass):** Query for disclosures where `trust_tier_at_disclosure < 3` but `fields_disclosed` contains a tier-3 field name — this should never appear.

**Threat 17 (Commitment replay):** Query for `context_commitments` where `nonce_released=true` and the same `commitment_hash` appears in more than one row — impossible by design, but a data integrity check.

**Threat 18 (Audit forgery):** Query for `context_disclosures` where `erased=true` but `erased_at IS NULL` — a sign of direct DB manipulation.

---

## Interview Q&A

**Q: What is the Layer 4 three-part invariant and what does each part protect against?**  
A: "Context flows only when: (1) the service declared the field in its manifest — protects against over-harvesting; (2) the agent's profile permits it — enforces agent preferences; (3) the service's trust tier meets the field's sensitivity requirement — enforces Layer 3 trust gating. Remove any one and a different attack class becomes possible."

**Q: Why is the nonce released in a separate disclose step rather than at match time?**  
A: "Two reasons. First, the disclose phase re-verifies trust — trust can drop between match and disclose, and we don't want to have already released sensitive data by then. Second, the gap between match and disclose gives the agent (or their platform's UX) a chance to present the classification to a human for review before high-sensitivity data leaves the system."

**Q: What happens if Redis is unavailable during a match request?**  
A: "Three things happen: the rate limit check is skipped (best-effort); the profile is read from the database instead of cache (fallback); the match result is not cached (the disclose phase will use the commitment-row snapshot instead). All three caches are best-effort — the system degrades gracefully, with increased DB load, rather than failing hard."

**Q: How does Layer 4 prevent a service from using a commitment nonce it received for one field to verify a different field?**  
A: "Each HMAC commitment is `hmac(nonce, field_value)`. The nonce is unique per commitment — generated fresh for each field in each match. Even if a service has nonce_A (released for field_A), it cannot use it to verify field_B because `hmac(nonce_A, field_B_value) ≠ commitment_B_hash`. The binding is per `(nonce, field_value)` pair."

**Q: Why does the compliance PDF not include field values, given that it's a "complete compliance record"?**  
A: "Because field values are never stored anywhere in Layer 4 — not in the database, not in Redis, not in any log. The audit trail records what was shared (field names), not the values themselves. This is a deliberate privacy-preserving design: if values were stored, every database access would be a privacy risk. The compliance PDF can prove *what categories of data were shared* without storing the data itself."

---

## Key Takeaways

- Three Redis caches: profile (60s, explicit invalidation), match (300s, no invalidation needed), rate limit (60s, sliding)
- Rate limit targets `agent_did` not IP — right unit for multi-tenant API gateways
- Four Layer 4 threats: over-harvesting, profile bypass, commitment replay, audit forgery
- Session assertion fallback is a Phase 3 design stub — disable in production
- The three-part invariant is not redundant — each condition blocks a different attack class
- Disclose-phase trust re-verification is the safety net for the trust-recompute latency window

---

## Curriculum Complete: Layers 1–4

You have now completed the full Layer 1–4 curriculum:

| Layer | Lessons | Core Concept |
|-------|---------|-------------|
| Layer 1 | 01–10 | Manifest Registry — discovery and distribution |
| Layer 2 | 11–20 | Identity & Credentials — cryptographic agent identity |
| Layer 3 | 21–30 | Trust & Verification — blockchain-anchored attestation |
| Layer 4 | 31–40 | Context Matching — privacy-preserving selective disclosure |

The next curriculum will cover **Layer 5: Workflow Registry** — how multi-step orchestration patterns are validated, published, and quality-scored across the stack you have now fully studied.
