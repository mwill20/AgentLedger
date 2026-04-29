# Lesson 46: The Talent Agency â€” Per-Step Ranking Engine

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_ranker.py` (lines 55â€“411), `api/routers/workflows.py`
**Prerequisites:** Lesson 45
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

A talent agency for a film production doesn't give the director a list of every actor on earth. It gives a shortlist for each role: actors who can play the character, ranked by fit. Layer 5's rank endpoint is that agency: for each workflow step, it queries the Layer 1 registry for capable services, filters by the step's trust thresholds, applies the agent's context profile, and returns a ranked shortlist.

This lesson traces the full `GET /workflows/{id}/rank` path â€” from cache key construction through SQL candidate retrieval and Layer 4 profile evaluation â€” and explains why this endpoint achieved p95=24ms at 100 concurrent requests in the acceptance load test.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain the four dimensions of `rank_cache_key()` and why each matters
- Trace `_rank_candidates_for_step()` through its JOIN, filters, and ordering
- Explain `_candidate_can_disclose()` and why it reuses Layer 4's `evaluate_profile()`
- Describe why `rank_score = trust_score / 100` rather than a composite formula
- Trace `get_workflow_rank()` and explain the cache-first + cache-on-miss pattern
- Explain why the load test achieved p95=24ms: first request vs. all subsequent requests

---

## The Rank Cache Key

```python
# api/services/workflow_ranker.py:55â€“66
def rank_cache_key(
    workflow_id: UUID,
    geo: str | None = None,
    pricing_model: str | None = None,
    agent_did: str | None = None,
) -> str:
    agent_segment = agent_did or "anonymous"
    return (
        f"workflow:rank:{workflow_id}:{geo or 'any'}:"
        f"{pricing_model or 'any'}:{agent_segment}"
    )
```

**Four cache dimensions:**

| Dimension | Why it matters |
|-----------|---------------|
| `workflow_id` | Different workflows have different steps â€” obviously separate |
| `geo` | `geo='EU'` filters out services with non-EU geo_restrictions; different geo = different candidates |
| `pricing_model` | `pricing_model='usage'` filters by pricing type; different model = different candidates |
| `agent_did` | The `can_disclose` flag per candidate depends on the agent's Layer 4 profile â€” different agent = different flags |

**Anonymous vs. identified:** If no `agent_did` is provided, the cache segment is `"anonymous"` and `can_disclose` is always `True` (no profile to check against). A request with an `agent_did` gets its own cache key â€” the profile-based `can_disclose` flags are agent-specific.

**Cache TTL: 60 seconds.** Service trust scores change infrequently. A 60-second staleness window is acceptable for a ranking endpoint. When a workflow is published or quality_score changes, `invalidate_workflow_caches()` is called â€” but it does NOT invalidate rank caches (`workflow:rank:*`). Rank caches expire naturally with their TTL.

---

## `get_workflow_rank()` â€” Cache-First, Cache-on-Miss

```python
# api/services/workflow_ranker.py:384â€“411
async def get_workflow_rank(workflow_id, *, geo, pricing_model, agent_did, db, redis) -> WorkflowRankResponse:
    cache_key = rank_cache_key(workflow_id, geo, pricing_model, agent_did)
    cached = await _cache_get_rank(redis, cache_key)
    if cached is not None:
        return cached                           # pure Redis hit: no DB queries

    response = WorkflowRankResponse(
        workflow_id=workflow_id,
        ranked_steps=await rank_workflow_steps(...)  # runs DB queries
    )
    await _cache_set_rank(redis, cache_key, response)  # store for next 60s
    return response
```

**Why p95=24ms in the load test:** The load test ran 100 concurrent users against the same `workflow_id` with no `geo`, `pricing_model`, or `agent_did`. All 100 users shared the same cache key. The first request (or the first few during cache stampede) ran the DB queries. Every subsequent request within the 60-second window returned the cached `WorkflowRankResponse` â€” serialized JSON, no SQL. The DB was hit at most a handful of times in 30 seconds, producing near-zero latency for all but the first request.

---

## `rank_workflow_steps()` â€” Per-Step Iteration

```python
# api/services/workflow_ranker.py:351â€“381
async def rank_workflow_steps(workflow_id, geo, pricing_model, db, agent_did=None, redis=None):
    steps = await _load_published_workflow_steps(db, workflow_id)  # 1 query
    profile = await _load_agent_profile_for_rank(db, agent_did, redis=redis)  # 0 or 1 query

    ranked_steps = []
    for step in steps:
        candidates = await _rank_candidates_for_step(
            db=db, step=dict(step), geo=geo,
            pricing_model=pricing_model, profile=profile
        )  # 1 query per step
        ranked_steps.append(RankedStep(..., candidates=candidates))
    return ranked_steps
```

**Query count without cache:** 1 (workflow + steps) + 0 or 1 (profile) + N (one per step). A 3-step workflow makes 4â€“5 DB queries. With 100 concurrent users and a cache hit rate of ~99% (all users share the same key), the actual DB load is negligible.

**`_load_agent_profile_for_rank()`:** Calls `context_profiles.get_active_profile()` â€” which itself checks Redis first. If no profile exists for the agent DID, returns a default deny profile rather than raising 404.

---

## `_rank_candidates_for_step()` â€” The SQL

```sql
SELECT
    s.id AS service_id,
    s.name,
    s.domain,
    s.trust_score,
    s.trust_tier,
    ot.domain AS ontology_domain,
    sp.pricing_model
FROM service_capabilities sc
JOIN services s ON s.id = sc.service_id
JOIN ontology_tags ot ON ot.tag = sc.ontology_tag
LEFT JOIN service_operations so ON so.service_id = s.id
LEFT JOIN service_pricing sp ON sp.service_id = s.id
WHERE sc.ontology_tag = :ontology_tag
  AND s.trust_tier >= :min_trust_tier
  AND s.trust_score >= :min_trust_score
  AND s.is_active = true
  AND s.is_banned = false
  [AND (so.geo_restrictions IS NULL OR cardinality(so.geo_restrictions) = 0 OR :geo = ANY(so.geo_restrictions))]
  [AND sp.pricing_model = :pricing_model]
ORDER BY s.trust_score DESC, s.id ASC
LIMIT 10
```

**The five baseline filters:**

| Filter | Purpose |
|--------|---------|
| `sc.ontology_tag = :ontology_tag` | Must be capable of this step's required capability |
| `s.trust_tier >= :min_trust_tier` | Must meet the step's minimum trust tier |
| `s.trust_score >= :min_trust_score` | Must meet the step's minimum trust score |
| `s.is_active = true` | Must be currently active in Layer 1 |
| `s.is_banned = false` | Must not be banned |

**Geo filter:** `cardinality(so.geo_restrictions) = 0` handles the case where a service has an empty geo_restrictions array (equivalent to no restrictions). `NULL` also means no restrictions.

**Sort:** `trust_score DESC` â€” highest-trust services appear first. `s.id ASC` breaks ties deterministically.

**LIMIT 10:** Returns at most 10 candidates per step. This bound prevents the response from growing unbounded for popular ontology tags with many capable services.

---

## `rank_score = trust_score / 100`

```python
ServiceCandidate(
    ...
    rank_score=round(trust_score / 100.0, 4),
    ...
)
```

The rank score is simply the normalized trust score. Unlike the workflow quality score formula, there is no multi-component rank score for individual service candidates. This is intentional:

- The step already filters by `min_trust_tier` and `min_trust_score` â€” candidates have already cleared the bar
- Among qualifying candidates, the primary differentiator is Layer 3 trust score
- Adding price or response time signals would require per-request aggregation from logs, increasing latency

**Recommendation for v0.2:** A composite rank score that includes Layer 1 uptime, Layer 4 context fit score, and Layer 3 trust â€” but only after benchmarking confirms it stays within the p95 < 200ms target.

---

## `_candidate_can_disclose()` â€” Layer 4 Integration

```python
# api/services/workflow_ranker.py:250â€“268
def _candidate_can_disclose(*, profile, step, service) -> bool:
    if profile is None:
        return True
    for field in list(step.get("context_fields_required") or []):
        decision = evaluate_profile(
            profile.rules,
            field,
            service,
            profile.default_policy,
        )
        if decision not in {"permit", "commit"}:
            return False
    return True
```

**What it checks:** For each required field in the step, runs the agent's Layer 4 profile rules against the candidate service. If any required field would be withheld (decision = `"withhold"`), `can_disclose = False` â€” the agent cannot proceed with this service without losing a required field.

**Reusing Layer 4's `evaluate_profile()`:** The ranking engine imports `evaluate_profile` directly from `context_matcher.py`. No separate implementation â€” the same profile rule engine that powers the Layer 4 match endpoint powers the rank endpoint's context fit check.

**`can_disclose` is advisory, not enforced.** The rank endpoint returns `can_disclose=False` for candidates the agent cannot fully disclose to. It does not remove those candidates from the list. The agent platform uses this flag to indicate to the user "this service won't receive your frequent flyer ID because your profile blocks it" â€” the agent can still choose to proceed knowing the field will be withheld.

---

## Exercise 1 â€” Call the Rank Endpoint

After publishing a workflow (Lesson 43â€“44):

```bash
WORKFLOW_ID="<published-workflow-uuid>"

curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID/rank" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

**Expected:** A `ranked_steps` array with one entry per workflow step. Each step has a `candidates` list sorted by `trust_score DESC`. Empty `candidates` list means no services currently meet the step's trust thresholds.

---

## Exercise 2 â€” Verify Redis Caching

```bash
# Call once (cache miss â†’ DB queries)
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID/rank" \
  -H "X-API-Key: dev-local-only" > /dev/null

# Check Redis
docker exec agentledger-redis-1 redis-cli KEYS "workflow:rank:*"
docker exec agentledger-redis-1 redis-cli TTL "workflow:rank:$WORKFLOW_ID:any:any:anonymous"
```

**Expected:** One Redis key with TTL â‰¤ 60.

---

## Exercise 3 â€” Agent Profile and can_disclose

Call the rank endpoint with an agent_did that has a restrictive profile:

```bash
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID/rank?agent_did=did:key:z6MkTestContextAgent" \
  -H "X-API-Key: dev-local-only" | python -c "
import sys, json
data = json.load(sys.stdin)
for step in data.get('ranked_steps', []):
    print(f\"Step {step['step_number']}:\")
    for c in step.get('candidates', []):
        print(f\"  {c['name']}: can_disclose={c['can_disclose']}\")
"
```

**Expected:** Each candidate shows `can_disclose=true` if the agent's profile permits all required step fields to flow to that service.

---

## Best Practices

**Keep the rank cache TTL short.** At 60 seconds, a trust tier change or revocation on a pinned service propagates to rank results within a minute. Longer TTLs would mean a revoked service appears as a valid candidate longer than acceptable. The 60-second window is the same as the Layer 4 profile cache â€” both sides of the context fit check use the same staleness budget.

**Recommended (not implemented here):** A cache invalidation hook in the Layer 3 revocation flow â€” when `dispatch_revocation_pushes()` fires, also delete `workflow:rank:*` keys for workflows that pin the revoked service. This would give instant invalidation for affected workflows rather than waiting up to 60 seconds.

---

## Interview Q&A

**Q: Why does the rank endpoint use `LIMIT 10` rather than returning all qualifying services?**
A: The rank response is cached per unique `(workflow_id, geo, pricing_model, agent_did)` tuple. An unbounded result set would make the cached JSON arbitrarily large for popular ontology tags. Ten candidates is sufficient for an agent platform to present options to a user â€” ranking by trust score ensures the best options are in the top 10.

**Q: How does `can_disclose` interact with the workflow execution flow?**
A: The rank endpoint provides `can_disclose` as an advisory signal before execution. The agent platform uses it to warn users ("this service won't receive your location") or filter out services where required fields would be withheld. The actual context disclosure enforcement happens at the Layer 4 match endpoint â€” `can_disclose` is a planning signal, not an enforcement gate.

**Q: Why doesn't the rank cache get invalidated when a workflow's quality_score changes?**
A: The rank response contains per-step service candidates ranked by Layer 3 trust score â€” it does not include the workflow quality_score. Quality score changes affect the workflow list ordering but not which services are candidates for each step. The two caches serve different purposes and have different invalidation triggers.

---

## Key Takeaways

- Cache key: `workflow:rank:{workflow_id}:{geo}:{pricing_model}:{agent_did}` â€” four dimensions
- Cache TTL: 60 seconds; not invalidated on quality_score changes (different concern)
- `_rank_candidates_for_step()`: five filters (tag, trust_tier, trust_score, active, not banned) + optional geo/pricing
- `rank_score = trust_score / 100` â€” normalized Layer 3 trust score, no multi-signal composite
- `can_disclose`: reuses Layer 4's `evaluate_profile()` â€” no separate implementation
- p95=24ms achieved because 100 concurrent users shared one cache key (same workflow, no optional filters)

---

## Next Lesson

**Lesson 47 â€” The One-Stop Approval: Context Bundle Integration** covers `workflow_context.py` â€” how a workflow-level context bundle aggregates required fields across all steps under a single user approval, how `_apply_scoped_overrides()` works, and the bundle approval flow that transitions the bundle from `pending` to `approved`.
