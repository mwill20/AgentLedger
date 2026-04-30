# Lesson 05: The Filing Cabinet -- Manifest Registration (Ingest)

> **Beginner frame:** Manifest ingestion turns a service's self-description into durable, searchable records. Think of it like filing a business license and indexing it so future agents can find, compare, and audit the service.

## Welcome Back, Systems Engineer!

You have validated the blueprint (Lesson 04). Now what? The validated `ServiceManifest` needs to be written into **six database tables** in a single atomic transaction -- services, manifests, capabilities with vector embeddings, pricing, context requirements, and operations. Today we dissect that entire pipeline, from the 28-line router that receives the request to the 320-line `register_manifest()` function that does the heavy lifting.

**Goal:** Trace every write that happens during manifest registration and explain why each design choice was made.
**Time:** 90 minutes
**Prerequisites:** Lessons 01-04
**Why this matters:** This is the single most important write path in AgentLedger. Every service in the registry enters through this function. Understanding it means understanding how the registry grows, how it stays consistent, and how it performs under load.

---

## Learning Objectives

- Explain the "thin router, thick service" pattern and why AgentLedger uses it
- Trace the 15 steps inside `register_manifest()` from ontology validation to response
- Describe the idempotency short-circuit and why it matters for load testing
- Explain the typosquat detection flow and why warnings are advisory, not blocking
- Identify how 6 tables are written in a single transaction with rollback guarantees
- Understand batch embedding and why it outperforms per-capability embedding

---

## File Map

```
api/
|-- routers/
|   `-- manifests.py       # 28 lines -- the thin router
`-- services/
    `-- registry.py        # register_manifest() at lines 185-507
                           # helper functions at lines 110-151
```

---

## Code Walkthrough: `api/routers/manifests.py` (The Thin Router)

This is the entire file. All 28 lines.

```python
# api/routers/manifests.py

"""POST /manifests endpoint."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_api_key
from api.models.manifest import ServiceManifest
from api.models.query import ManifestRegistrationResponse
from api.services import registry
from crawler.tasks.verify_domain import enqueue_domain_verification

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post(
    "/manifests",
    response_model=ManifestRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_manifest(
    manifest: ServiceManifest,
    db: AsyncSession = Depends(get_db),
) -> ManifestRegistrationResponse:
    """Register or update a service manifest."""
    response = await registry.register_manifest(db=db, manifest=manifest)
    enqueue_domain_verification(manifest.domain, response.service_id)
    return response
```

Line-by-Line:

1. **`router = APIRouter(dependencies=[Depends(require_api_key)])`** -- Every route on this router requires a valid API key. The dependency is applied at the router level, not the individual route, so there is no way to accidentally add an unauthenticated endpoint to this file.

2. **`manifest: ServiceManifest`** -- FastAPI automatically parses the JSON request body through the Pydantic model we studied in Lesson 04. By the time this line executes, null-byte checking, whitespace stripping, FQDN validation, and capability deduplication have already happened.

3. **`response = await registry.register_manifest(db=db, manifest=manifest)`** -- One call to the service layer. The router does not contain a single SQL statement, a single business rule, or a single database import. That is the pattern.

4. **`enqueue_domain_verification(manifest.domain, response.service_id)`** -- After the database writes succeed, the router enqueues an asynchronous domain verification task. This is a fire-and-forget call -- the HTTP response does not wait for DNS verification to complete.

5. **`status_code=status.HTTP_201_CREATED`** -- Returns 201, not 200, even for updates. This is a pragmatic choice: the endpoint always creates a new manifest version row, so 201 is technically accurate.

**Insight:**
The "thin router, thick service" pattern means the router's only jobs are (1) declare the HTTP contract (path, method, status code, response model), (2) inject dependencies, and (3) call the service. This makes unit testing trivial -- you can test `registry.register_manifest()` directly without spinning up an HTTP server.

---

## Code Walkthrough: Helper Functions (Lines 110-151)

Before diving into the main function, let's understand the four helpers it calls.

### `_resolve_context_rows()` -- Normalizing Flexible Input

```python
def _resolve_context_rows(fields: list[ContextField], is_required: bool) -> list[dict[str, Any]]:
    """Map manifest context fields into DB rows."""
    rows: list[dict[str, Any]] = []
    for index, field in enumerate(fields, start=1):
        rows.append(
            {
                "field_name": field.resolved_name(index),
                "field_type": field.resolved_type(),
                "is_required": is_required,
                "sensitivity": field.sensitivity,
            }
        )
    return rows
```

This function bridges the gap between the flexible `ContextField` model (which accepts `name`, `field_name`, or `id`) and the stable database schema (which needs exactly one `field_name` string). The `resolved_name(index)` call walks the fallback chain (`field_name` -> `name` -> `id` -> `context_field_{index}`), so the database always receives a non-null identifier.

The `is_required` parameter comes from the caller: required context fields pass `True`, optional ones pass `False`. The function itself does not know which list the field came from -- that separation happens at the call site.

### `_manifest_hash()` -- Deterministic Change Detection

```python
def _manifest_hash(manifest: ServiceManifest) -> str:
    """Hash the manifest payload for change tracking."""
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()
```

Two critical details:

1. **`sort_keys=True`** -- Without this, `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` would produce different hashes even though they represent the same manifest. Sorting keys makes the serialization deterministic.

2. **`model_dump(mode="json")`** -- Pydantic's JSON mode converts UUIDs, datetimes, and URLs into their string representations before serialization. This means the hash is computed over the wire-format representation, not Python object internals.

The returned SHA-256 hex digest is 64 characters long and stored in the `manifests.manifest_hash` column. It is compared on subsequent registrations to detect whether anything actually changed.

### `_manifest_url()` -- The Well-Known Convention

```python
def _manifest_url(domain: str) -> str:
    """Build the canonical manifest URL."""
    return f"https://{domain}/.well-known/agent-manifest.json"
```

AgentLedger follows the `.well-known` URI convention (RFC 8615). Every service is expected to host its manifest at this predictable path. The domain verification crawler (Vector B) will later fetch this URL to confirm that the service actually controls the domain it claims.

### `_status_for_manifest()` -- Sensitivity-Based Review Flagging

```python
def _status_for_manifest(manifest: ServiceManifest) -> str:
    """Flag sensitive manifests for manual review."""
    ontology = load_ontology_index()
    sensitive = any(
        ontology[capability.ontology_tag]["sensitivity_tier"] >= 3
        for capability in manifest.capabilities
    )
    return "pending_review" if sensitive else "registered"
```

The ontology defines a `sensitivity_tier` for each tag (1-5). If any of the manifest's capabilities touch a high-sensitivity domain (tier 3 or above -- think financial transactions, healthcare data, identity verification), the manifest goes into `pending_review` instead of `registered`. In the current build, that flags the service as inactive at ingest time; it does not yet create a manifest-approval queue or automatic reviewer workflow.

**Insight:**
Notice that this function does not validate whether the ontology tags exist -- that already happened in `register_manifest()` before this function is called. By the time `_status_for_manifest()` runs, every `capability.ontology_tag` is guaranteed to be a valid key in `load_ontology_index()`. This separation of concerns prevents redundant validation.

### `_trust_score_for_manifest()` -- Initial Score from Operational Metadata

```python
def _trust_score_for_manifest(manifest: ServiceManifest) -> float:
    """Derive an initial trust score for a newly registered service."""
    uptime = manifest.operations.uptime_sla_percent
    operational_score = 0.5 if uptime is None else min(max(uptime / 100.0, 0.0), 1.0)
    return compute_trust_score(0.0, 0.0, operational_score, 0.0)
```

A brand-new service has no verification history (`0.0`), no community endorsements (`0.0`), and no reliability track record (`0.0`). The only signal available at registration time is the self-reported uptime SLA. If the service claims 99.9% uptime, the operational score is `0.999`. If no uptime is provided, the default is `0.5` (no data, so assume middle ground). The `compute_trust_score()` function applies weighting (studied in a later lesson).

---

## Code Walkthrough: `register_manifest()` (Lines 185-507)

This is the heart of the ingest pipeline. We will walk through it in 15 steps, matching the order of execution.

### Step 1: Ontology Tag Validation (Lines 190-200)

```python
invalid_tags = [
    capability.ontology_tag
    for capability in manifest.capabilities
    if capability.ontology_tag not in load_ontology_index()
]
if invalid_tags:
    joined = ", ".join(sorted(set(invalid_tags)))
    raise HTTPException(
        status_code=422,
        detail=f"unknown ontology_tag values: {joined}",
    )
```

The function's first act is a guard clause. Every capability's `ontology_tag` must exist in the ontology index (the `v0.1.json` file loaded at startup). Unknown tags get a 422 response listing all invalid values.

Why not validate this in the Pydantic model? Because the ontology is loaded from a file at runtime, not from a static enum. Pydantic's `Literal` type requires compile-time values. The service layer has access to `load_ontology_index()`, and the model layer does not.

### Step 2: Pre-Computation (Lines 202-207)

```python
manifest_hash = _manifest_hash(manifest)
raw_manifest_json = json.dumps(manifest.model_dump(mode="json"))
trust_score = _trust_score_for_manifest(manifest)
status_name = _status_for_manifest(manifest)
is_active = status_name != "pending_review"
typosquat_warnings: list[str] = []
```

Before touching the database, the function computes everything it needs:
- The manifest hash for idempotency checking
- The raw JSON for archival storage
- The trust score and status for the services row
- The `is_active` flag derived from the status (pending_review services are inactive)
- An empty warnings list that may be populated later

### Step 3: Existing Service Lookup (Lines 210-234)

```python
existing_result = await db.execute(
    text("""
        SELECT s.id, s.domain, m.manifest_hash
        FROM services s
        LEFT JOIN manifests m ON m.service_id = s.id AND m.is_current = true
        WHERE s.id = :service_id OR s.domain = :domain
    """),
    {"service_id": manifest.service_id, "domain": manifest.domain},
)
existing_rows = existing_result.mappings().all()
existing_service = next(
    (row for row in existing_rows if row["id"] == manifest.service_id), None,
)
existing_domain = next(
    (row for row in existing_rows if row["domain"] == manifest.domain), None,
)
if existing_domain and existing_domain["id"] != manifest.service_id:
    raise HTTPException(
        status_code=422,
        detail="domain is already registered to a different service_id",
    )
```

A single query checks two conditions simultaneously: does this `service_id` already exist, and does this `domain` already exist? The `LEFT JOIN` on manifests fetches the current manifest hash in the same round trip.

The domain ownership check prevents service B from claiming service A's domain. This is a hard block -- the request fails with 422 if a domain collision is detected.

### Step 4: Flags (Lines 241-242)

```python
is_update = existing_service is not None
domain_changed = not is_update or existing_service["domain"] != manifest.domain
```

Two boolean flags that drive the rest of the function:
- `is_update`: True if the service already exists (we are updating, not creating)
- `domain_changed`: True if this is a new service OR if an existing service is changing its domain

### Step 5: Idempotency Short-Circuit (Lines 244-258)

```python
if (
    is_update
    and not domain_changed
    and existing_service["manifest_hash"] == manifest_hash
):
    await db.rollback()
    return ManifestRegistrationResponse(
        service_id=manifest.service_id,
        trust_tier=1,
        trust_score=trust_score,
        status="updated",
        capabilities_indexed=len(manifest.capabilities),
        typosquat_warnings=[],
    )
```

This is the performance optimization that makes load testing viable. If the service already exists, the domain has not changed, and the manifest hash is identical, **nothing has changed**. The function rolls back the implicit transaction (releasing the connection cleanly) and returns immediately without writing a single row.

**Insight:**
Without this short-circuit, re-submitting the same manifest would delete and re-insert capabilities, re-generate embeddings, and create a new manifest version row -- all producing identical results. The hash comparison costs one SHA-256 computation and one string comparison. The avoided work costs 6+ SQL statements and an embedding API call. During load tests that replay the same manifests, this optimization reduces database load by orders of magnitude.

### Step 6: Typosquat Detection (Lines 260-277)

```python
if domain_changed:
    all_domains_result = await db.execute(
        text("SELECT domain FROM services WHERE id != :service_id"),
        {"service_id": manifest.service_id},
    )
    all_domains = [row["domain"] for row in all_domains_result.mappings().all()]
    typosquat_matches = find_similar_domains(manifest.domain, all_domains)
    typosquat_warnings = [
        f"domain '{manifest.domain}' is similar to existing domain "
        f"'{m['domain']}' (edit distance {m['distance']})"
        for m in typosquat_matches
    ]
    if typosquat_warnings:
        logger.warning(
            "Typosquat warning for %s: %s",
            manifest.domain,
            "; ".join(typosquat_warnings),
        )
```

When a new domain enters the registry (new service or domain change), AgentLedger fetches all existing domains and runs `find_similar_domains()` to detect potential typosquatting. The function uses edit distance to find domains that look suspiciously similar (e.g., `examp1e.com` vs `example.com`).

Key design decision: warnings are **advisory, not blocking**. The registration still succeeds. The warnings are returned in the response so the caller can act on them, and they are logged for operator review. Blocking on similarity would create false positives that prevent legitimate registrations.

### Step 7: Service Upsert (Lines 279-314)

```python
await db.execute(
    text("""
        INSERT INTO services (
            id, name, domain, legal_entity, manifest_url, public_key,
            trust_tier, trust_score, is_active, created_at, updated_at, first_seen_at
        )
        VALUES (
            :service_id, :name, :domain, :legal_entity, :manifest_url, :public_key,
            :trust_tier, :trust_score, :is_active, NOW(), NOW(), NOW()
        )
        ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            domain = EXCLUDED.domain,
            legal_entity = EXCLUDED.legal_entity,
            manifest_url = EXCLUDED.manifest_url,
            public_key = EXCLUDED.public_key,
            trust_tier = EXCLUDED.trust_tier,
            trust_score = EXCLUDED.trust_score,
            is_active = EXCLUDED.is_active,
            last_crawled_at = NOW(),
            updated_at = NOW()
    """),
    {
        "service_id": manifest.service_id,
        "name": manifest.name,
        "domain": manifest.domain,
        "legal_entity": manifest.legal_entity,
        "manifest_url": _manifest_url(manifest.domain),
        "trust_tier": 1,
        "trust_score": trust_score,
        "is_active": is_active,
    },
)
```

A single `INSERT ... ON CONFLICT DO UPDATE` handles both creation and update. This is PostgreSQL's upsert pattern.

Key details:
- On insert: `created_at`, `updated_at`, and `first_seen_at` are all set to `NOW()`
- On update: only `updated_at` and `last_crawled_at` are refreshed -- `created_at` and `first_seen_at` are preserved
- `EXCLUDED` refers to the values that would have been inserted, allowing the update clause to use the same parameter bindings
- `trust_tier` starts at 1 for all new services (the lowest tier)

### Step 8: Manifest Versioning (Lines 316-348)

```python
await db.execute(
    text("""
        UPDATE manifests
        SET is_current = false
        WHERE service_id = :service_id AND is_current = true
    """),
    {"service_id": manifest.service_id},
)
await db.execute(
    text("""
        INSERT INTO manifests (
            service_id, raw_json, manifest_hash, manifest_version, is_current, crawled_at
        )
        VALUES (
            :service_id,
            CAST(:raw_json AS JSONB),
            :manifest_hash,
            :manifest_version,
            true,
            NOW()
        )
    """),
    {
        "service_id": manifest.service_id,
        "raw_json": raw_manifest_json,
        "manifest_hash": manifest_hash,
        "manifest_version": manifest.manifest_version,
    },
)
```

Two statements implement a version chain:
1. Mark all existing manifests for this service as `is_current = false`
2. Insert the new manifest as `is_current = true`

This preserves full history. You can query any previous manifest version by its `manifest_hash`. The raw JSON is stored as JSONB, so it is queryable via PostgreSQL JSON operators if needed.

**Insight:**
The `UPDATE` before `INSERT` pattern (rather than a single upsert) is deliberate. Manifests don't have a natural unique key other than their auto-generated ID. The `is_current` flag acts as a "pointer" to the latest version. A partial unique index (`WHERE is_current = true`) could enforce this at the database level, but the two-statement approach is simpler and equally correct within the same transaction.

### Step 9: Capability Embedding (Lines 350-398)

```python
await db.execute(
    text("DELETE FROM service_capabilities WHERE service_id = :service_id"),
    {"service_id": manifest.service_id},
)
capability_rows = []
capability_embeddings = embed_batch(
    [capability.description for capability in manifest.capabilities]
)
for capability, embedding_vector in zip(
    manifest.capabilities,
    capability_embeddings,
    strict=True,
):
    capability_rows.append(
        {
            "service_id": manifest.service_id,
            "ontology_tag": capability.ontology_tag,
            "description": capability.description,
            "embedding": serialize_embedding(embedding_vector),
            "input_schema_url": (
                str(capability.input_schema_url) if capability.input_schema_url else None
            ),
            "output_schema_url": (
                str(capability.output_schema_url) if capability.output_schema_url else None
            ),
        }
    )
if capability_rows:
    await db.execute(
        text("""
            INSERT INTO service_capabilities (
                service_id, ontology_tag, description, embedding, input_schema_url,
                output_schema_url, is_verified, created_at
            )
            VALUES (
                :service_id, :ontology_tag, :description,
                CAST(:embedding AS vector),
                :input_schema_url, :output_schema_url,
                false, NOW()
            )
        """),
        capability_rows,
    )
```

This is the most complex step. Let's break it apart:

1. **Delete-then-insert pattern** -- All existing capabilities for the service are deleted first. This is simpler than diffing individual capabilities and handles removed, added, and modified capabilities uniformly.

2. **`embed_batch()`** -- Generates 384-dimensional vectors for all capability descriptions in a single call. If a service has 10 capabilities, this is one embedding API call, not ten. The batch approach is critical for both latency and cost.

3. **`zip(..., strict=True)`** -- The `strict` flag raises an error if the capabilities list and the embeddings list have different lengths. This is a safety net against embedding API bugs.

4. **`serialize_embedding()`** -- Converts the Python list of floats into the format PostgreSQL's `pgvector` extension expects.

5. **`CAST(:embedding AS vector)`** -- The raw embedding string is cast to the `vector` type at insert time.

6. **`is_verified = false`** -- All capabilities start unverified. Verification happens later through the crawler pipeline.

**Insight:**
Why delete-then-insert instead of upsert for capabilities? Because capabilities don't have a stable primary key across manifest versions. A service might rename a capability's `id`, change its `ontology_tag`, or reorder the list. The delete-then-insert pattern is O(n) regardless of what changed, and it guarantees no stale capabilities remain. The trade-off is that auto-generated capability IDs are not stable across updates, but nothing in the system depends on capability ID stability.

### Step 10: Pricing Upsert (Lines 400-427)

```python
await db.execute(
    text("""
        WITH deleted AS (
            DELETE FROM service_pricing
            WHERE service_id = :service_id
        )
        INSERT INTO service_pricing (
            service_id, pricing_model, tiers, billing_method, currency, created_at, updated_at
        )
        VALUES (
            :service_id, :pricing_model, CAST(:tiers AS JSONB),
            :billing_method, 'USD', NOW(), NOW()
        )
    """),
    {
        "service_id": manifest.service_id,
        "pricing_model": manifest.pricing.model,
        "tiers": json.dumps(manifest.pricing.tiers),
        "billing_method": manifest.pricing.billing_method,
    },
)
```

A CTE (Common Table Expression) combines the delete and insert into a single SQL statement. The `WITH deleted AS (DELETE ...)` runs the deletion, and the outer `INSERT` runs in the same statement. This is a PostgreSQL idiom for atomic replace.

Why a CTE instead of two statements like capabilities? Stylistic variation, but the CTE has a minor advantage: it is one round trip to the database instead of two. For capabilities, the batch insert requires a separate `execute()` call anyway (because SQLAlchemy handles the list of parameter dicts differently), so the CTE pattern does not apply there.

### Step 11: Context Requirements (Lines 429-453)

```python
await db.execute(
    text("DELETE FROM service_context_requirements WHERE service_id = :service_id"),
    {"service_id": manifest.service_id},
)
context_rows = [
    {"service_id": manifest.service_id, **row}
    for row in (
        _resolve_context_rows(manifest.context.required, True)
        + _resolve_context_rows(manifest.context.optional, False)
    )
]
if context_rows:
    await db.execute(
        text("""
            INSERT INTO service_context_requirements (
                service_id, field_name, field_type, is_required, sensitivity, created_at
            )
            VALUES (
                :service_id, :field_name, :field_type, :is_required, :sensitivity, NOW()
            )
        """),
        context_rows,
    )
```

The same delete-then-insert pattern as capabilities. The `_resolve_context_rows()` helper is called twice -- once for required fields (with `is_required=True`) and once for optional fields (with `is_required=False`). The two lists are concatenated with `+` and each row gets the `service_id` prepended via dict merging (`**row`).

### Step 12: Operations Upsert (Lines 454-487)

```python
await db.execute(
    text("""
        INSERT INTO service_operations (
            service_id, uptime_sla_percent, rate_limit_rpm, rate_limit_rpd, sandbox_url,
            created_at, updated_at
        )
        VALUES (
            :service_id, :uptime_sla_percent, :rate_limit_rpm,
            :rate_limit_rpd, :sandbox_url, NOW(), NOW()
        )
        ON CONFLICT (service_id) DO UPDATE
        SET uptime_sla_percent = EXCLUDED.uptime_sla_percent,
            rate_limit_rpm = EXCLUDED.rate_limit_rpm,
            rate_limit_rpd = EXCLUDED.rate_limit_rpd,
            sandbox_url = EXCLUDED.sandbox_url,
            updated_at = NOW()
    """),
    {
        "service_id": manifest.service_id,
        "uptime_sla_percent": manifest.operations.uptime_sla_percent,
        "rate_limit_rpm": manifest.operations.rate_limits.rpm,
        "rate_limit_rpd": manifest.operations.rate_limits.rpd,
        "sandbox_url": (
            str(manifest.operations.sandbox_url) if manifest.operations.sandbox_url else None
        ),
    },
)
```

Operations use a true `ON CONFLICT` upsert (like services) rather than delete-then-insert. This is possible because `service_operations` has a `UNIQUE` constraint on `service_id` -- there is exactly one operations row per service, so the conflict target is clear.

### Step 13: Transaction Commit (Line 489)

```python
await db.commit()
```

A single commit at the end of the `try` block. All six table writes -- services, manifests, capabilities, pricing, context requirements, and operations -- are committed atomically. If the server crashes between the capabilities insert and the pricing insert, nothing is committed. The registry never contains a half-written manifest.

### Step 14: Error Handling (Lines 490-498)

```python
except HTTPException:
    await db.rollback()
    raise
except SQLAlchemyError as exc:
    await db.rollback()
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"failed to register manifest: {exc.__class__.__name__}",
    ) from exc
```

Two exception handlers:

1. **`HTTPException`** -- Business logic errors (422 for bad ontology tags, 422 for domain conflicts). These are rolled back and re-raised as-is. The client gets the original error message.

2. **`SQLAlchemyError`** -- Database errors (connection failure, constraint violation, timeout). These are wrapped in a generic 500 response. The exception class name is included for debugging, but the full traceback is not exposed to the client (security best practice).

Both handlers call `db.rollback()` before raising. This is critical: without an explicit rollback, the session could be returned to the connection pool in a dirty state.

### Step 15: Response (Lines 500-507)

```python
return ManifestRegistrationResponse(
    service_id=manifest.service_id,
    trust_tier=1,
    trust_score=trust_score,
    status="updated" if is_update else status_name,
    capabilities_indexed=len(manifest.capabilities),
    typosquat_warnings=typosquat_warnings,
)
```

The response tells the caller:
- `service_id` -- Echoed back for confirmation
- `trust_tier` -- Always 1 for new/updated services
- `trust_score` -- The computed initial score
- `status` -- `"updated"` for re-registrations, `"registered"` or `"pending_review"` for new services
- `capabilities_indexed` -- How many capabilities were embedded and stored
- `typosquat_warnings` -- Empty list if no similar domains found, or a list of human-readable warnings

---

## The Six-Table Transaction Map

```
register_manifest()
  |
  |-- [1] services           INSERT ... ON CONFLICT (id) DO UPDATE
  |-- [2] manifests          UPDATE is_current=false, then INSERT
  |-- [3] service_capabilities   DELETE all, then batch INSERT
  |-- [4] service_pricing        CTE: DELETE + INSERT in one statement
  |-- [5] service_context_requirements   DELETE all, then INSERT
  |-- [6] service_operations     INSERT ... ON CONFLICT (service_id) DO UPDATE
  |
  `-- COMMIT (single atomic commit)
```

Three different write strategies are used depending on the table's constraints:

| Strategy | Tables | When to Use |
|----------|--------|-------------|
| `ON CONFLICT DO UPDATE` | services, service_operations | Table has a stable unique key (service_id) |
| Delete-then-insert | service_capabilities, service_context_requirements | No stable unique key across versions |
| CTE delete+insert | service_pricing | Single-row replace, one round trip |

---

## Data Flow Diagram

```
POST /manifests (JSON body)
        |
        v
  [Pydantic validation]  ----422----> Client (bad input)
        |
        v
  [Ontology tag check]   ----422----> Client (unknown tags)
        |
        v
  [Existing service lookup]
        |
   .----+----.
   |         |
 New?    Existing?
   |         |
   |    [Hash match?]---yes---> Return immediately (idempotent)
   |         |no
   |         |
   '----+----'
        |
  [Domain changed?]---yes---> [Typosquat check] --warnings--> response
        |
        v
  [Service upsert]
  [Manifest version]
  [Capabilities + embeddings]
  [Pricing]
  [Context requirements]
  [Operations]
        |
        v
     COMMIT
        |
        v
  [Enqueue domain verification]
        |
        v
  201 Created + ManifestRegistrationResponse
```

---

## Hands-On Exercises

### Exercise 1: Trace the Idempotency Path

Read `register_manifest()` and answer these questions without running the code:

1. What is the minimum number of SQL statements executed when a manifest is re-submitted with no changes?
2. What is the maximum number of SQL statements executed on a first-time registration with 5 capabilities?
3. If the idempotency check passes, is `enqueue_domain_verification()` still called? (Hint: look at where it is called -- the router or the service?)

Answers:
1. Two: the `SELECT` to check the existing service and hash, then `db.rollback()`. No writes.
2. Eight: SELECT existing, INSERT service, UPDATE old manifests, INSERT new manifest, DELETE capabilities, INSERT capabilities, CTE pricing, DELETE context, INSERT context, INSERT operations. (Count each `db.execute()` call.)
3. Yes. The router calls `enqueue_domain_verification()` after `register_manifest()` returns, regardless of whether the service function short-circuited. The domain verification is re-enqueued every time.

### Exercise 2: Add a New Table Write

Imagine you need to store `manifest.compliance_certifications` (a list of strings like `["SOC2", "HIPAA"]`) in a new `service_certifications` table. Using the patterns in `register_manifest()`, write the SQL and Python code to:

1. Delete existing certifications for the service
2. Insert the new certifications

Which pattern would you follow -- the capabilities pattern (delete-then-insert with two statements) or the pricing pattern (CTE)?

```python
# Your answer should look something like this:
await db.execute(
    text("DELETE FROM service_certifications WHERE service_id = :service_id"),
    {"service_id": manifest.service_id},
)
cert_rows = [
    {"service_id": manifest.service_id, "certification": cert}
    for cert in manifest.compliance_certifications
]
if cert_rows:
    await db.execute(
        text("""
            INSERT INTO service_certifications (service_id, certification, created_at)
            VALUES (:service_id, :certification, NOW())
        """),
        cert_rows,
    )
```

The capabilities pattern (delete + batch insert) is correct here because there may be multiple rows per service. The CTE pattern works best for single-row replacement.

### Exercise 3: Test Hash Determinism

```python
import json
from hashlib import sha256
from api.models.manifest import ServiceManifest

# Create two identical manifests with fields in different dict order
# (Pydantic normalizes the order, so the hash should match)
manifest_a = ServiceManifest(**your_test_data)
manifest_b = ServiceManifest(**your_test_data)  # same data

hash_a = sha256(json.dumps(manifest_a.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
hash_b = sha256(json.dumps(manifest_b.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

assert hash_a == hash_b, "Hashes should be identical for identical manifests"
```

---

## Interview Prep

**Q: Why does `register_manifest()` use a single commit at the end instead of committing after each table write?**

**A:** Atomicity. If the function committed after inserting the service row but crashed before inserting capabilities, the registry would contain a service with no capabilities -- a corrupt state. A single commit at the end ensures all six tables are updated together or not at all. This is the fundamental property of database transactions (the "A" in ACID). The rollback handlers guarantee cleanup on any failure path.

---

**Q: Why is the idempotency short-circuit important for production systems?**

**A:** Three reasons: (1) **Performance under load** -- load tests and monitoring systems often re-submit the same manifest. Without the short-circuit, each submission triggers 6+ SQL statements and an embedding API call. With it, the cost is one SELECT and one string comparison. (2) **Reduced write amplification** -- unnecessary writes generate WAL entries, trigger replication, and consume IOPS. (3) **Embedding cost** -- embedding APIs charge per call. Skipping redundant `embed_batch()` calls saves real money at scale.

---

**Q: Why are typosquat warnings advisory instead of blocking?**

**A:** Because edit distance produces false positives. Legitimate domains like `agent-a.com` and `agent-b.com` have low edit distance but are not typosquats. Blocking registration would require manual intervention for every similar domain, creating friction that discourages adoption. The advisory approach lets the system flag suspicious cases while allowing the registration to proceed. Operators can review the warnings asynchronously.

---

**Q: What is the difference between the upsert pattern (`ON CONFLICT DO UPDATE`) and the delete-then-insert pattern? When would you choose each?**

**A:** The upsert pattern requires a stable unique key to detect conflicts. Services have `id`, operations have `service_id` -- these never change, so `ON CONFLICT` works cleanly. Capabilities and context requirements lack a stable unique key across manifest versions (a capability's `ontology_tag` might change, be removed, or be reordered). Delete-then-insert avoids the key stability problem entirely: remove everything, insert the current state. The trade-off is that delete-then-insert generates more WAL traffic and resets auto-generated IDs.

---

## Key Takeaways

- The router is 28 lines because all business logic lives in the service layer ("thin router, thick service")
- `register_manifest()` writes to 6 tables in a single atomic transaction
- The idempotency short-circuit skips all writes when the manifest hash has not changed
- Typosquat warnings are advisory -- they inform but do not block registration
- `embed_batch()` generates all capability embeddings in one call, not N individual calls
- Three write strategies are used: `ON CONFLICT` upsert, delete-then-insert, and CTE delete+insert
- Error handling distinguishes business errors (HTTPException, re-raised) from infrastructure errors (SQLAlchemyError, wrapped in 500)
- `_manifest_hash()` uses `sort_keys=True` for deterministic serialization
- `_status_for_manifest()` flags high-sensitivity capabilities (tier >= 3) as `pending_review` and keeps the service inactive until a separate activation path runs
- Domain verification is enqueued after the response, making it asynchronous

---

## Summary Reference Card

| Component | Location | Purpose |
|-----------|----------|---------|
| `manifests.py` router | `api/routers/manifests.py` | HTTP contract: POST /manifests, 201 Created, auth required |
| `register_manifest()` | `api/services/registry.py:185-507` | Orchestrates all 6 table writes in one transaction |
| `_manifest_hash()` | `api/services/registry.py:125-128` | SHA-256 of sorted JSON for change detection |
| `_manifest_url()` | `api/services/registry.py:131-133` | Builds `https://{domain}/.well-known/agent-manifest.json` |
| `_status_for_manifest()` | `api/services/registry.py:136-143` | Flags sensitivity_tier >= 3 as `pending_review` and sets the initial service status to inactive |
| `_trust_score_for_manifest()` | `api/services/registry.py:146-150` | Derives initial trust from uptime SLA |
| `_resolve_context_rows()` | `api/services/registry.py:110-122` | Normalizes flexible ContextField into stable DB rows |
| `find_similar_domains()` | `api/services/typosquat.py` | Edit-distance typosquat detection |
| `embed_batch()` | `api/services/embedder.py` | Batch 384-dim vector generation for capabilities |
| `enqueue_domain_verification()` | `crawler/tasks/verify_domain.py` | Async domain ownership verification (fire-and-forget) |

| Table Written | Write Strategy | Key Detail |
|---------------|----------------|------------|
| `services` | ON CONFLICT upsert | Preserves `created_at` and `first_seen_at` on update |
| `manifests` | UPDATE + INSERT | Version chain via `is_current` flag |
| `service_capabilities` | Delete + batch INSERT | Embeddings generated via `embed_batch()` |
| `service_pricing` | CTE delete+insert | Single statement, one round trip |
| `service_context_requirements` | Delete + INSERT | Resolved from flexible ContextField |
| `service_operations` | ON CONFLICT upsert | Stable 1:1 relationship with service |

---

## Ready for Lesson 06?

Next up, we will explore **semantic search** -- how AgentLedger takes a natural-language query, embeds it into the same 384-dimensional vector space as the capabilities we just stored, and uses cosine similarity to find the best-matching services. You have seen how the embeddings get in; now you will see how they come back out.

*Remember: This one function writes to six tables, generates vector embeddings, detects typosquats, and handles idempotency -- and it does it all inside a single transaction boundary. That is the power of thoughtful orchestration.*
