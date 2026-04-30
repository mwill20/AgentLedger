# Lesson 49: The Four Threats — Anti-Gaming & Hardening

> **Beginner frame:** Workflow hardening is anti-cheat for quality signals. It protects validation, ranking, caching, and feedback paths from manipulation.

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_validator.py`, `api/services/workflow_ranker.py`, `api/services/workflow_context.py`, `api/services/workflow_registry.py`
**Prerequisites:** Lessons 41â€“48
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

Layer 5 adds four new threats to the system's threat model. Layers 1â€“4 addressed 14 threats; Layer 5 adds four more (threats 19â€“22 in the full model). This lesson traces each threat, the code that mitigates it, and what breaks if the mitigation is removed.

It also covers the Layer 5 caching strategy (three cache types, three invalidation patterns) and the rate limit design for the workflow query surface.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Name all four Layer 5 threats and their severity
- Identify the exact code line that mitigates each threat
- Explain what breaks if each mitigation is removed
- Describe the three Redis cache types in Layer 5 and their TTLs
- Explain why workflow list caches are invalidated aggressively on every publication
- Recite the Layer 5 rate limit design: target, threshold, and fail-open rationale

---

## The Four Layer 5 Threats

### Threat 19: Workflow Laundering
**Attack:** Submit a legitimate workflow, get it validated and published, then update the spec to route steps to malicious services.
**Severity:** Critical
**Mitigation:** `compute_spec_hash()` in `workflow_validator.py:25â€“27` computes `sha256(json.dumps(spec, sort_keys=True))` and stores it in `workflows.spec_hash` at publication time. `update_workflow_spec()` in `workflow_registry.py:662â€“670` rejects any PUT on a published workflow:

```python
if workflow_row["status"] == "published":
    raise HTTPException(409, "published workflow spec is immutable; submit a new workflow...")
```

**What breaks without it:** An adversary could get a legitimate two-step workflow approved (validator sees steps 1 and 2), then modify the stored spec to add a malicious step 3 that routes sensitive context to an exfiltration service. Every agent that runs the "validated" workflow would expose data without realizing the spec had changed.

---

### Threat 20: Step Poisoning
**Attack:** Publish a workflow with 9 legitimate steps and 1 malicious step that over-requests context beyond what is needed for that step's ontology tag.
**Severity:** Critical
**Mitigation (two layers):**

**Layer 1 â€” Validation checklist item 2:**
```
"context_minimal": true
```
The validator is required to confirm, for each step, that context_fields_required contains only fields reasonably necessary for that step's ontology tag. A malicious step requesting `user.ssn` for a `travel.air.search` step would fail this check.

**Layer 2 â€” Automatic domain validator assignment (`_validation_domain()` in `workflow_registry.py:325â€“334`):**
```python
for step in request.steps:
    if int(row["sensitivity_tier"]) >= 3:
        return str(row["domain"])
```
Any step touching a sensitivity_tier â‰¥ 3 field automatically assigns the workflow to a domain-specific validator. A travel workflow that includes a step requesting health data must be reviewed by a HEALTH domain expert, not a general validator.

**What breaks without it:** An adversary builds a legitimate-looking travel workflow but embeds a step requesting `user.ssn` and `user.dob`. A general-purpose validator approves it without noticing. Every agent running the workflow inadvertently discloses high-sensitivity data to the travel service.

---

### Threat 21: Quality Gaming
**Attack:** An agent platform reports thousands of fake success outcomes to inflate a workflow's quality score, making it appear at the top of search results.
**Severity:** High
**Mitigation:** The unverifiable cap in `workflow_ranker.py:159â€“160`:

```python
if verification_rate < 0.5:
    raw = min(raw, 0.70)
```

**The math:** Even if an adversary reports 10,000 unverified success outcomes, `verification_rate = 0 / 10,000 = 0.0`, which is < 0.5 â†’ score capped at 70.0. A legitimate workflow with verified executions can score 70.0â€“100.0. The adversarial workflow is visible but cannot outrank genuinely verified workflows.

**What breaks without it:** A malicious workflow author could create a low-quality workflow, generate 10,000 fake success reports in an API loop, and push the quality score to 100.0 â€” appearing first in every search result. Agent platforms would preferentially execute a gaming-inflated workflow over a genuinely good one.

**The secondary verification check:** Executions that include a valid `context_bundle_id` can be verified against the Layer 4 `context_disclosures` audit trail (`verify_execution()` in `workflow_executor.py:447â€“490`). Fake reports without a bundle stay unverified. Reports with a bundle are only verified if actual Layer 4 disclosures exist.

---

### Threat 22: Context Bundle Abuse
**Attack:** An agent obtains an approved bundle for a legitimate workflow and reuses the `bundle_id` to authorize context disclosure for a different workflow, or reuses it multiple times for the same workflow.
**Severity:** High
**Mitigation (three layers):**

**Layer 1 â€” Workflow + agent scoping:**
```python
# workflow_executor.py:86â€“116
SELECT id FROM workflow_context_bundles
WHERE id = :context_bundle_id
  AND workflow_id = :workflow_id    # must match this execution's workflow
  AND agent_did = :agent_did        # must match this agent
```

A bundle from workflow A cannot be used with workflow B â€” even if the same agent owns both.

**Layer 2 â€” 30-minute TTL:**
```sql
expires_at = NOW() + INTERVAL '30 minutes'
```
Bundles cannot be reused indefinitely â€” they expire after 30 minutes. An attacker who captures a bundle_id cannot use it after expiry.

**Layer 3 â€” Consumed status:** After a bundle is used in one execution, it transitions to `consumed`. The `approve_context_bundle()` endpoint rejects any second approval attempt with 409 Conflict.

**What breaks without it:** An adversary captures a bundle_id from a legitimate approved workflow execution and uses it to authorize context disclosure for a different, malicious workflow. The verification would succeed (the audit trail shows disclosures exist) even though the agent never approved context disclosure for the second workflow.

---

## The Layer 5 Caching Strategy

Layer 5 uses three distinct Redis cache types. Each has a different key structure, TTL, and invalidation trigger.

### Cache 1 â€” Workflow Detail (60s TTL)

```python
# Keys:
f"workflow:detail:{workflow_id}"   # by UUID
f"workflow:slug:{slug}"            # by text slug
```

**Invalidation trigger:** `invalidate_workflow_caches(redis, workflow_id=..., slug=...)` called on every status change (publication, update, deprecation) and every quality_score change.

**Why 60s?** Workflow specs change infrequently â€” typically only at publication time. A 60-second window is short enough that publication is visible quickly but long enough to absorb burst read traffic.

**Cross-population:** A fetch by UUID also populates the slug cache, and vice versa. Both paths share the same cached response.

### Cache 2 â€” Workflow List (60s TTL)

```python
# Key:
f"workflow:list:{sha256(json.dumps(filter_params, sort_keys=True))}"
```

**Invalidation trigger:** All `workflow:list:*` keys are deleted on every status change or quality_score change. The `_matching_cache_keys()` utility scans for the pattern.

**Why aggressive invalidation?** List queries are sorted by `quality_score DESC`. Any execution report that changes a quality score changes the list ordering. Rather than tracking which list pages are affected, all list caches are cleared on every quality change. The 60s TTL limits the damage from this aggressive strategy.

**Why hash the filter parameters?** A distinct cache key per filter combination requires a stable hash. SHA-256 of canonical JSON (`sort_keys=True`, sorted tags list) produces a deterministic key regardless of key ordering in the request.

### Cache 3 â€” Workflow Rank (60s TTL)

```python
# Key:
f"workflow:rank:{workflow_id}:{geo or 'any'}:{pricing_model or 'any'}:{agent_did or 'anonymous'}"
```

**Invalidation trigger:** `_invalidate_rank_cache(redis, workflow_id)` called on every execution report that changes the quality_score. Clears `workflow:rank:{workflow_id}:*` using pattern matching.

**Why a separate cache from workflow detail?** The rank response contains per-step service candidates ranked by Layer 3 trust scores â€” not the workflow spec. A spec change (publication) affects the detail cache; a quality score change (execution report) affects both the detail and rank caches.

**Why four cache key dimensions?** `geo` and `pricing_model` filter candidates differently; `agent_did` changes `can_disclose` flags. The same workflow has different rank results for different agent/geo/pricing combinations.

---

## Rate Limit Design

```python
# api/services/workflow_registry.py:163â€“197
WORKFLOW_QUERY_RATE_LIMIT_PER_MINUTE = 200
WORKFLOW_QUERY_RATE_LIMIT_WINDOW_SECONDS = 60

cache_key = f"workflow:query:rate:{sha256(api_key.encode()).hexdigest()}"
count = await redis.incr(cache_key)
if int(count) == 1:
    await redis.expire(cache_key, 60)
if int(count) > 200:
    raise HTTPException(429, {"limit": 200, "window_seconds": 60, ...})
```

**Target: API key, not IP.** Multi-tenant platforms (multiple agents behind one API gateway) would share the same IP. Per-IP rate limiting would punish legitimate platforms. Per-API-key limits target the right unit: one platform's discovery requests, not one IP's traffic.

**200 queries / 60 seconds.** A platform building a workflow catalog makes burst queries at startup (discovering available workflows by domain and tag). 200/minute accommodates catalog sync. Sustained scraping (thousands of queries per minute) is blocked.

**Fail open:** If Redis is unavailable, the rate limit is bypassed. Workflow discovery is a read-only operation â€” excess DB load is the worst-case consequence of a rate limit outage, not a security breach. The fail-open pattern prioritizes availability.

**API key is hashed.** `sha256(api_key.encode()).hexdigest()` prevents the API key from appearing in Redis key space, where it could be enumerated via `KEYS *` or `SCAN`.

---

## Hardening Exercise â€” Threat Walkthrough SQL

For each threat, a SQL query that would detect a violation in a production database:

**Threat 19 (Workflow laundering):**
```sql
-- Any published workflow where spec_hash is null (hash not set at publication = missed the immutability check)
SELECT id, name, status, spec_hash, published_at
FROM workflows
WHERE status = 'published' AND spec_hash IS NULL;
```

**Threat 20 (Step poisoning):**
```sql
-- Steps requesting high-sensitivity fields in low-trust workflows
SELECT w.id, w.name, ws.step_number, ws.ontology_tag, ws.context_fields_required
FROM workflow_steps ws
JOIN workflows w ON w.id = ws.workflow_id
WHERE w.status = 'published'
  AND ws.min_trust_tier < 3
  AND EXISTS (
    SELECT 1 FROM unnest(ws.context_fields_required) AS f(field)
    WHERE f.field ILIKE '%ssn%' OR f.field ILIKE '%dob%' OR f.field ILIKE '%medical%'
  );
```

**Threat 21 (Quality gaming):**
```sql
-- Workflows with high quality_score but low verification_rate (suspicious pattern)
SELECT
    w.id, w.name, w.quality_score, w.execution_count, w.success_count,
    COUNT(CASE WHEN we.verified = true THEN 1 END) AS verified_count,
    ROUND(
        COUNT(CASE WHEN we.verified = true THEN 1 END)::numeric / NULLIF(w.execution_count, 0),
        2
    ) AS verification_rate
FROM workflows w
LEFT JOIN workflow_executions we ON we.workflow_id = w.id
WHERE w.quality_score > 65.0
  AND w.execution_count > 10
GROUP BY w.id
HAVING COUNT(CASE WHEN we.verified = true THEN 1 END)::float / NULLIF(w.execution_count, 0) < 0.1;
```

**Threat 22 (Bundle abuse):**
```sql
-- Bundles used in execution reports but not consumed (status mismatch)
SELECT wcb.id, wcb.workflow_id, wcb.agent_did, wcb.status
FROM workflow_context_bundles wcb
WHERE wcb.id IN (SELECT context_bundle_id FROM workflow_executions WHERE context_bundle_id IS NOT NULL)
  AND wcb.status != 'consumed';
```

---

## Exercise 1 â€” Verify Spec Immutability

After publishing a workflow, confirm that its `spec_hash` is set in the database:

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, status, spec_hash, published_at FROM workflows WHERE status='published';"
```

**Expected:** `spec_hash` is a 64-character hex string for all published workflows.

---

## Exercise 2 â€” Verify the Unverifiable Cap

Using the quality score formula, compute the maximum achievable score with `verification_rate=0`:

```python
# Any combination of success_rate=1.0, volume_factor=1.0, avg_step_trust=1.0
raw = 1.0*0.35 + 1.0*0.30*1.0 + 0.0*0.20 + 1.0*0.15
print(f"raw={raw}")                     # 0.80
print(f"capped={min(raw, 0.70)*100}")   # 70.0
```

---

## Interview Q&A

**Q: What stops an adversary from creating 100 agent DIDs and using each one to report verified executions with legitimate bundles?**
A: Creating 100 registered agents requires 100 API key registrations (Layer 2), each with a valid cryptographic DID. Each execution report requires an approved context bundle â€” which requires actual Layer 4 context disclosures for each step. Running 100 real workflow executions with real context disclosures is prohibitively expensive and leaves a full audit trail. The quality gaming threat assumes fabricated reports without context bundles, not genuine executions.

**Q: Why is the rank cache invalidated on execution reports when rank results don't include quality_score?**
A: The rank response doesn't include the workflow's quality_score directly. However, the rank endpoint documentation implies freshness â€” an agent platform using the rank response should see up-to-date service trust scores. More pragmatically, the rank cache and quality score are both signals that change when execution data arrives. Invalidating both on execution ensures the next rank request is computed fresh rather than returning a cached response from before the execution.

**Q: Why does Layer 5's rate limit target the workflow query endpoints specifically rather than all Layer 5 endpoints?**
A: The submit endpoint (`POST /workflows`) is naturally rate-limited by the validation pipeline â€” authors can't submit hundreds of workflows per minute without running out of ontology tags and author DIDs to validate against. Execution reports are already rate-limited by the cost of actual workflow executions. The list and rank endpoints are the only pure read-surface that could be scraped without resource cost â€” the 200/minute limit targets this specific surface.

---

## Key Takeaways

- **Threat 19 (Laundering):** `spec_hash` at publication + PUT rejection for published workflows
- **Threat 20 (Poisoning):** Checklist item 2 (context_minimal) + auto-assign domain validator for sensitivity_tier â‰¥ 3
- **Threat 21 (Gaming):** `verification_rate < 0.5` cap at 70.0 â€” cannot be bypassed by volume
- **Threat 22 (Bundle abuse):** Triple protection â€” workflow+agent scoping + 30min TTL + consumed status
- Three cache types: detail (60s, explicit invalidation), list (60s, aggressive invalidation on any quality change), rank (60s, pattern-deleted on execution report)
- Rate limit: 200 queries/60s per API key, fail-open, hashed key

---

## Next Lesson

**Lesson 50 â€” The Final Debrief: Full Layer 5 Flow & Interview Readiness** completes the Layer 5 curriculum with the full end-to-end flow diagram, the six canonical interview questions, the Layer 5 invariant, Layer 6 handoff points, and a summary of the complete 50-lesson curriculum.
