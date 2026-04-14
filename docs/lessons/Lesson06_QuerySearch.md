# Lesson 06: The Search Engine -- Structured Queries and Semantic Search

## Welcome Back, Systems Engineer!

Services are registered. The database is populated with manifests, capabilities, embeddings, pricing, and context requirements. Now comes the payoff: how does an AI agent **find** the right service? Today we dissect the two query paths -- structured queries (exact ontology tag match) and semantic search (natural language similarity via pgvector) -- plus the ranking metadata that scores results.

**Goal:** Understand how both query paths work, how the ranking algorithm scores results, and how Redis caching prevents redundant computation.
**Time:** 90 minutes
**Prerequisites:** Lessons 01-05
**Why this matters:** Discovery is the core value proposition of AgentLedger. If search doesn't return the right service, nothing else matters.

---

## Learning Objectives

- Trace a structured query from HTTP request through SQL to the response payload
- Trace a semantic search from query embedding through pgvector cosine distance to ranked response
- Explain the six-factor ranking algorithm and its weight distribution
- Understand the embedder's dual-mode architecture (model vs hash)
- Describe the Redis caching strategy and its fail-open behavior
- Explain why semantic search overfetches candidates before re-ranking

---

## File Map

```
api/routers/
|-- ontology.py       # GET /ontology (16 lines)
|-- services.py       # GET /services, GET /services/{id} (50 lines)
|-- search.py         # POST /search (27 lines)

api/services/
|-- registry.py       # query_services(), search_services(), get_service_detail()
|-- embedder.py       # embed_text(), embed_batch(), hash fallback (139 lines)
|-- ranker.py         # compute_rank_score(), compute_trust_score() (80 lines)
```

---

## Code Walkthrough: The Ontology Helpers

Before any query runs, the ontology must be loaded. Three cached functions handle this.

```python
# api/services/registry.py

@lru_cache
def load_ontology_payload() -> dict[str, Any]:
    """Load the ontology source-of-truth file."""
    return json.loads(_ONTOLOGY_PATH.read_text(encoding="utf-8"))

@lru_cache
def load_ontology_index() -> dict[str, dict[str, Any]]:
    """Index ontology tags by tag string."""
    payload = load_ontology_payload()
    return {tag["tag"]: tag for tag in payload["tags"]}

def build_ontology_response() -> OntologyResponse:
    """Build the GET /ontology response payload."""
    payload = load_ontology_payload()
    tags = [OntologyTagRecord(**tag) for tag in payload["tags"]]
    by_domain: dict[str, list[OntologyTagRecord]] = {}
    for tag in tags:
        by_domain.setdefault(tag.domain, []).append(tag)
    return OntologyResponse(
        ontology_version=payload["ontology_version"],
        total_tags=len(tags),
        domains=payload["domains"],
        tags=tags,
        by_domain=by_domain,
    )
```

Key details:

1. **`@lru_cache`** -- Both `load_ontology_payload()` and `load_ontology_index()` are decorated with `@lru_cache`. They execute once per process and are cached forever. Since the ontology file doesn't change at runtime, this is safe and eliminates file I/O on every request.

2. **`load_ontology_index()`** -- Builds a `{tag_string: tag_data}` lookup dict. Used by `ensure_ontology_tag_exists()` and `register_manifest()` for O(1) tag validation.

3. **`build_ontology_response()`** -- NOT cached with `@lru_cache` because it returns a Pydantic model (unhashable). But the underlying data comes from cached functions, so it's still fast.

4. **`by_domain` grouping** -- Pre-groups tags by domain for client convenience. An agent exploring capabilities can iterate `by_domain["TRAVEL"]` without client-side filtering.

The ontology router is trivial:

```python
# api/routers/ontology.py -- entire file (16 lines)
router = APIRouter(dependencies=[Depends(require_api_key)])

@router.get("/ontology", response_model=OntologyResponse)
async def get_ontology() -> OntologyResponse:
    return build_ontology_response()
```

---

## Code Walkthrough: Structured Query (`query_services`)

The structured query endpoint finds services by exact ontology tag match with optional filters.

### The Router

```python
# api/routers/services.py
@router.get("/services", response_model=ServiceSearchResponse)
async def list_services(
    ontology: str,
    trust_min: float = 0,
    trust_tier_min: int = 1,
    geo: str | None = None,
    pricing_model: str | None = None,
    latency_max_ms: int | None = None,
    limit: int = 10,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ServiceSearchResponse:
    return await registry.query_services(
        db=db, redis=redis, ontology=ontology,
        trust_min=trust_min, trust_tier_min=trust_tier_min,
        geo=geo, pricing_model=pricing_model,
        latency_max_ms=latency_max_ms, limit=limit, offset=offset,
    )
```

All filter parameters have defaults. Only `ontology` is required. The router passes every parameter through to the service layer -- thin router, thick service.

### The Service Function

```python
# api/services/registry.py — query_services()

async def query_services(
    db: AsyncSession,
    ontology: str,
    trust_min: float = 0,
    trust_tier_min: int = 1,
    geo: str | None = None,
    pricing_model: str | None = None,
    latency_max_ms: int | None = None,
    limit: int = 10,
    offset: int = 0,
    redis=None,
) -> ServiceSearchResponse:
    ensure_ontology_tag_exists(ontology)

    # Redis cache check
    cache_key = f"query:{sha256(json.dumps({...}, sort_keys=True).encode()).hexdigest()}"
    if redis is not None:
        cached = await _cache_get(redis, cache_key)
        if cached is not None:
            return ServiceSearchResponse.model_validate_json(cached)
```

Step by step:

1. **`ensure_ontology_tag_exists()`** -- Raises 422 if the tag isn't in the ontology. This prevents queries for nonexistent capabilities.

2. **Cache key construction** -- SHA-256 of all sorted parameters. Two queries with identical parameters hit the same cache entry. The `sort_keys=True` ensures deterministic JSON serialization.

### The SQL Query

```sql
SELECT
    s.id AS service_id, s.name, s.domain,
    s.trust_tier, s.trust_score, s.is_active,
    c.ontology_tag, c.description,
    c.avg_latency_ms, c.success_rate_30d, c.is_verified,
    p.pricing_model
FROM services s
JOIN service_capabilities c ON c.service_id = s.id
LEFT JOIN service_operations o ON o.service_id = s.id
LEFT JOIN service_pricing p ON p.service_id = s.id
WHERE c.ontology_tag = :ontology
  AND s.is_active = true
  AND s.is_banned = false
  AND s.trust_score >= :trust_min
  AND s.trust_tier >= :trust_tier_min
  AND (CAST(:pricing_model AS TEXT) IS NULL OR p.pricing_model = :pricing_model)
  AND (CAST(:latency_max_ms AS INTEGER) IS NULL OR c.avg_latency_ms IS NULL
       OR c.avg_latency_ms <= :latency_max_ms)
  AND (CAST(:geo AS TEXT) IS NULL OR o.geo_restrictions IS NULL
       OR COALESCE(array_length(o.geo_restrictions, 1), 0) = 0
       OR :geo = ANY(o.geo_restrictions))
ORDER BY s.trust_score DESC, s.name ASC
LIMIT :limit OFFSET :offset
```

Key SQL patterns:

- **`CAST(:param AS TEXT) IS NULL OR ...`** -- This is the "optional filter" pattern. When `pricing_model` is None, the CAST produces NULL, the IS NULL check is true, and the filter is skipped. When it has a value, the second condition is evaluated. This avoids dynamic SQL generation.

- **`LEFT JOIN`** on operations and pricing -- Some services may not have these records. LEFT JOIN ensures they still appear in results with NULL values.

- **`COALESCE(array_length(o.geo_restrictions, 1), 0) = 0`** -- If geo_restrictions is empty or NULL, don't filter by geo. Services without restrictions serve all geos.

- **Ordering** -- Primary sort by trust_score DESC (highest trust first), secondary by name ASC (alphabetical tiebreaker).

### Result Ranking

```python
results = [_service_summary_from_row(row, match_score=1.0) for row in rows]
```

For structured queries, `match_score` is always 1.0 -- an exact ontology tag match is a perfect capability match. The response still includes a computed `rank_score`, but the current implementation does not re-sort the SQL results by that field; it preserves the database ordering of `trust_score DESC, name ASC`.

---

## Code Walkthrough: Semantic Search (`search_services`)

Semantic search uses natural language queries against vector embeddings.

### The Router

```python
# api/routers/search.py
@router.post("/search", response_model=ServiceSearchResponse)
async def search_services(
    request: SearchRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> ServiceSearchResponse:
    if not request.query:
        raise HTTPException(status_code=400, detail="query must not be empty")
    return await registry.search_services(db=db, redis=redis, request=request)
```

### The Service Function

```python
# Step 1: Embed the query
query_embedding = serialize_embedding(embed_text(request.query))

# Step 2: Overfetch candidates
candidate_limit = max(request.limit * 5, 50)
```

**Why overfetch?** The pgvector query sorts by cosine distance alone. But the final ranking uses six factors (capability match, trust, latency, cost, reliability, context). A service that's #20 by pure cosine similarity might be #3 after trust and latency are factored in. Fetching 5x candidates ensures the re-ranking has enough material.

### The pgvector Query

```sql
SELECT
    ...,
    1.0 - (c.embedding <=> CAST(:query_embedding AS vector)) AS cosine_similarity
FROM services s
JOIN service_capabilities c ON c.service_id = s.id
...
WHERE s.is_active = true
  AND s.is_banned = false
  AND s.trust_score >= :trust_min
  AND c.embedding IS NOT NULL
ORDER BY c.embedding <=> CAST(:query_embedding AS vector)
LIMIT :candidate_limit
```

Key details:

- **`<=>`** is pgvector's cosine distance operator. It returns a distance (0 = identical, 2 = opposite), not a similarity.
- **`1.0 - distance`** converts distance to similarity (1.0 = identical, -1.0 = opposite).
- **`c.embedding IS NOT NULL`** -- Filters out capabilities without embeddings (shouldn't exist, but defensive).
- **Ordering by distance** -- pgvector can use its IVFFlat index for this ORDER BY, making it efficient.

### Service Grouping

```python
service_map: dict[UUID, ServiceSummary] = {}
for row in result.mappings().all():
    match_score = max(0.0, min(1.0, float(row["cosine_similarity"])))
    if match_score <= 0:
        continue
    sid = UUID(str(row["service_id"]))
    if sid not in service_map:
        service_map[sid] = _service_summary_from_row(row, match_score=match_score)
    else:
        # Append this capability to the existing service
        cap = MatchedCapability(...)
        service_map[sid].matched_capabilities.append(cap)
        # Update rank_score using the best capability match
        best_match = max(c.match_score for c in service_map[sid].matched_capabilities)
        service_map[sid].rank_score = compute_rank_score(
            capability_match=best_match, ...
        )
```

One service may have multiple capabilities that match the query. The grouping logic:

1. First occurrence creates the ServiceSummary entry
2. Subsequent capabilities are appended to `matched_capabilities`
3. The rank_score is recalculated using the **best** capability match (not average, not sum)

**Why best match?** A service with one highly relevant capability and one marginally relevant capability should rank based on its strongest match, not be penalized by averaging.

### Final Sort and Pagination

```python
ranked = sorted(service_map.values(), key=lambda item: item.rank_score, reverse=True)
sliced = ranked[request.offset : request.offset + request.limit]
```

Sort by rank_score DESC, then apply pagination. This means the ranking is computed over ALL candidates, then paginated -- so page 2 gets the next-best results, not a random slice.

---

## Code Walkthrough: The Embedder (`api/services/embedder.py`)

### Dual-Mode Architecture

```python
EMBEDDING_DIMENSION = 384
MODEL_NAME = "all-MiniLM-L6-v2"

_model: SentenceTransformer | None = None
_model_load_attempted = False

def _get_model() -> SentenceTransformer | None:
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True

    from api.config import settings
    if settings.embedding_mode == "hash":
        return _model  # remains None

    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    except ImportError:
        logger.warning("sentence-transformers not installed")
    except Exception:
        logger.exception("Failed to load embedding model")
    return _model
```

The lazy loading pattern:
- **`_model_load_attempted`** prevents retrying a failed load on every request
- **`EMBEDDING_MODE=hash`** skips loading entirely -- returns None, falls through to hash embedder
- **Import inside function** -- `sentence_transformers` is only imported when needed, so the app starts without it

### Hash-Based Fallback

```python
def _hash_embed(text: str, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    vector = [0.0] * dimension
    tokens = _tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        token_hash = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big")
        vector[token_hash % dimension] += 1.0
    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude == 0:
        return vector
    return [v / magnitude for v in vector]
```

The hash embedder is deterministic and dependency-free:

1. **Tokenize** -- Extract lowercase alphanumeric tokens
2. **Normalize** -- Simple stemming: remove trailing 's', convert 'ies' to 'y'
3. **Hash scatter** -- Each token's SHA-256 hash determines which dimension gets +1.0
4. **L2 normalize** -- Divide by magnitude to create a unit vector

**Why SHA-256 instead of Python's `hash()`?** Python's `hash()` is randomized per process (PYTHONHASHSEED). Two uvicorn workers would produce different embeddings for the same text. SHA-256 is deterministic across all processes and restarts.

**Why does this work?** Two texts with overlapping tokens will have overlapping non-zero dimensions, producing a positive cosine similarity. It's crude but sufficient for testing and CI.

### Batch Embedding

```python
def embed_batch(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    if model is not None:
        embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [e.tolist() for e in embeddings]
    return [_hash_embed(t) for t in texts]
```

With the real model, `encode()` processes 32 texts at a time on the GPU/CPU. This is significantly faster than calling `embed_text()` N times because the model parallelizes matrix operations across the batch.

### Serialization for pgvector

```python
def serialize_embedding(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
```

pgvector expects the text format `[0.123456,0.234567,...]`. Six decimal places provide sufficient precision for cosine similarity.

---

## Code Walkthrough: The Ranker (`api/services/ranker.py`)

### The Ranking Algorithm

```python
def compute_rank_score(
    capability_match, trust_score, latency_score,
    cost_score, reliability_score, context_fit,
) -> float:
    score = (
        capability_match * 0.35
        + trust_score * 0.25
        + latency_score * 0.15
        + cost_score * 0.10
        + reliability_score * 0.10
        + context_fit * 0.05
    )
    return round(_clamp(score), 6)
```

The weights reflect AgentLedger's priorities:
- **Capability match (35%)** -- Does the service do what you need?
- **Trust score (25%)** -- Is the service trustworthy?
- **Latency (15%)** -- Is it fast?
- **Cost (10%)** -- Is it affordable?
- **Reliability (10%)** -- Does it stay up?
- **Context fit (5%)** -- Does it match context requirements? (Currently always 1.0 -- reserved for future use)

### The Trust Score Formula

```python
def compute_trust_score(
    capability_probe_score, attestation_score,
    operational_score, reputation_score,
) -> float:
    raw = (
        capability_probe_score * 0.35
        + attestation_score * 0.30
        + operational_score * 0.20
        + reputation_score * 0.15
    )
    return round(_clamp(raw) * 100.0, 2)
```

Currently in Layer 1, `capability_probe_score`, `attestation_score`, and `reputation_score` are all 0.0. Only `operational_score` (derived from uptime SLA) contributes. Layers 2-3 will populate the other scores.

### Helper Normalizers

```python
PRICING_MODEL_SCORES = {
    "free": 1.0, "freemium": 0.8, "subscription": 0.6, "per_transaction": 0.5,
}

def compute_latency_score(avg_latency_ms: int | None) -> float:
    if avg_latency_ms is None:
        return 0.5  # neutral when unknown
    return _clamp(1.0 - (avg_latency_ms / 10000.0))

def compute_cost_score(pricing_model: str | None) -> float:
    if pricing_model is None:
        return 0.5  # neutral when unknown
    return PRICING_MODEL_SCORES.get(pricing_model, 0.5)

def compute_reliability_score(success_rate_30d: float | None) -> float:
    if success_rate_30d is None:
        return 0.5  # neutral when unknown
    if success_rate_30d > 1:
        return _clamp(success_rate_30d / 100.0)  # handle percentage format
    return _clamp(success_rate_30d)
```

The pattern: when data is unknown, return 0.5 (neutral). This prevents services without latency data from being penalized or rewarded unfairly.

`compute_reliability_score` handles both formats: `0.95` (fraction) and `95.0` (percentage). The `> 1` check auto-detects which format was provided.

---

## Code Walkthrough: Service Detail (`get_service_detail`)

```python
async def get_service_detail(db: AsyncSession, service_id: UUID) -> ServiceDetail:
```

This function runs 6 sequential queries to build the full service record:

1. **Service base** -- `SELECT ... FROM services WHERE id = :service_id` (raises 404 if not found)
2. **Current manifest** -- `SELECT raw_json FROM manifests WHERE service_id = :service_id AND is_current = true`
3. **Capabilities** -- `SELECT ... FROM service_capabilities WHERE service_id = :service_id`
4. **Pricing** -- `SELECT ... FROM service_pricing WHERE service_id = :service_id`
5. **Context requirements** -- `SELECT ... FROM service_context_requirements WHERE service_id = :service_id`
6. **Operations** -- `SELECT ... FROM service_operations WHERE service_id = :service_id`

Each query returns data for a different section of the `ServiceDetail` response model. The raw manifest JSON is included as-is in `current_manifest`, giving clients access to any field that AgentLedger doesn't explicitly index.

---

## Redis Caching Strategy

Both `query_services` and `search_services` use identical caching:

```python
# Cache helpers
async def _cache_get(redis, key: str) -> str | None:
    try:
        return await redis.get(key)
    except Exception:
        return None  # fail-open

async def _cache_set(redis, key: str, value: str) -> None:
    try:
        await redis.set(key, value, ex=CACHE_TTL_SECONDS)  # 60s TTL
    except Exception:
        pass  # cache is best-effort
```

Properties:
- **60-second TTL** -- Short enough that new registrations appear quickly, long enough to absorb burst traffic
- **Fail-open** -- Redis errors are silently swallowed. The query falls through to the database.
- **No cache invalidation** -- Registrations don't purge cache. The 60s TTL handles staleness.
- **SHA-256 cache keys** -- Deterministic, collision-resistant, fixed length regardless of parameter count

---

## Hands-On Exercises

### Exercise 1: Trace a Structured Query

```powershell
curl -H "X-API-Key: dev-local-only" "http://localhost:8000/v1/services?ontology=travel.air.book&trust_min=5&limit=5"
```

Observe the response: each result has `rank_score`, `trust_score`, `trust_tier`, and `matched_capabilities`.

### Exercise 2: Trace a Semantic Search

```powershell
curl -X POST -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" `
  -d '{"query": "I need to book a flight with seat selection"}' `
  http://localhost:8000/v1/search
```

Compare `match_score` values across results. Higher scores mean the capability description was semantically closer to your query.

### Exercise 3: Compare Embedding Modes

```python
from api.services.embedder import embed_text, _hash_embed

# Hash mode
hash_vec = _hash_embed("book a flight")
print(f"Non-zero dimensions: {sum(1 for v in hash_vec if v != 0)}")

# If model is loaded
model_vec = embed_text("book a flight")
print(f"Non-zero dimensions: {sum(1 for v in model_vec if v != 0)}")
# Model vectors have ALL dimensions non-zero (dense)
# Hash vectors have only a few non-zero (sparse)
```

---

## Interview Prep

**Q: How does AgentLedger's semantic search work?**

**A:** The query text is embedded into a 384-dimensional vector using sentence-transformers (all-MiniLM-L6-v2). This vector is passed to pgvector's cosine distance operator (`<=>`), which finds the closest capability embeddings. We overfetch 5x the requested limit to allow re-ranking with a six-factor algorithm (capability match, trust, latency, cost, reliability, context fit). Results are grouped by service -- one service may match multiple capabilities -- and the best capability match drives the rank score.

---

**Q: Why does the semantic search overfetch candidates?**

**A:** pgvector sorts by cosine distance alone, but the final ranking uses six factors. A service ranked #20 by pure similarity could be #3 after trust and latency adjustments. Overfetching ensures the re-ranking algorithm has a sufficient candidate pool. The 5x multiplier (minimum 50) was chosen empirically during Phase 5 load testing.

---

**Q: What happens when Redis is unavailable?**

**A:** Queries fall through to the database without caching. The `_cache_get` and `_cache_set` functions catch all exceptions and return None / no-op respectively. This fail-open design prioritizes availability over performance -- an agent should always get results, even if they're slightly slower.

---

## Key Takeaways

- Two query paths: structured (exact tag) and semantic (natural language via pgvector)
- Ranking algorithm: capability_match(0.35) + trust(0.25) + latency(0.15) + cost(0.10) + reliability(0.10) + context(0.05)
- Embedder has model mode (all-MiniLM-L6-v2) and hash mode (deterministic fallback)
- Hash mode uses SHA-256 for cross-process consistency, not Python's randomized hash()
- Semantic search overfetches 5x candidates then re-ranks with the full algorithm
- Redis caching is best-effort with 60s TTL and no explicit invalidation
- Ontology is loaded once per process via @lru_cache

---

## Summary Reference Card

| Component | Function | Key Detail |
|-----------|----------|------------|
| `query_services()` | Structured query | Exact ontology tag match, `match_score=1.0`, SQL ordered by trust score/name |
| `search_services()` | Semantic search | pgvector cosine distance, overfetch 5x |
| `get_service_detail()` | Single service | 6 sequential queries, includes raw manifest |
| `embed_text()` | Single embedding | 384-dim, model or hash mode |
| `embed_batch()` | Batch embedding | batch_size=32 for GPU efficiency |
| `compute_rank_score()` | Ranking | 6 weighted factors, clamped to [0,1] |
| `compute_trust_score()` | Trust | 4 weighted factors, scaled to 0-100 |
| `_cache_get/_set` | Redis cache | 60s TTL, fail-open, SHA-256 keys |

---

## Ready for Lesson 07?

Next up, we'll explore **The Watchdog** -- the Celery crawler that periodically re-fetches manifests and the DNS verification system that promotes services from trust tier 1 to tier 2. Get ready to see how background workers maintain the integrity of the registry!

*Remember: Registration fills the registry, but search makes it valuable. A registry nobody can search is just a database!*
