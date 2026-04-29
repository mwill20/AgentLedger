# Lesson 57: The Contract — Data Models & API Routes

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/models/liability.py`, `api/routers/liability.py`
**Prerequisites:** Lessons 51–56
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A legal contract is only as strong as the precision of its language. Vague terms invite disputes; precise terms prevent them. Layer 6's Pydantic models are that contract language — they define exactly what each API endpoint accepts, validates, and returns, with field-level constraints that prevent malformed evidence from entering the accountability record.

This lesson traces the key Pydantic models, explains the validation constraints that enforce the integrity of the attribution system, and maps each model to the endpoint that uses it.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Map each Pydantic request/response model to its endpoint
- Explain the `_SanitizedModel` base class and why null bytes are rejected at the model layer
- Describe the DID format validation in `_validate_did()` and which fields it applies to
- Trace the `ClaimCreateRequest` model and its five claim type constraints
- Explain the `AttributionFactor` model and how it links factors to evidence IDs
- Describe the compliance export endpoint's query parameter pattern and why it returns `Response` not a Pydantic model

---

## The `_SanitizedModel` Base Class

```python
# api/models/liability.py:25–38
class _SanitizedModel(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def sanitize_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict):
            null_fields = check_null_bytes_recursive(data)
            if null_fields:
                raise ValueError(f"null bytes are not allowed in: {', '.join(null_fields)}")
            return strip_strings_recursive(data)
        return data
```

All Layer 6 request models inherit from `_SanitizedModel`, which applies the same two-pass sanitization used in Layers 1–5: reject null bytes (which can corrupt text storage or cause SQL issues) and strip leading/trailing whitespace from all string fields. This is a cross-layer convention, not a Layer 6 invention.

---

## DID Validation

```python
# api/models/liability.py:41–45
def _validate_did(value: str) -> str:
    if not value.startswith("did:"):
        raise ValueError("DID values must start with did:")
    return value
```

All `claimant_did`, `determined_by`, `resolved_by`, and `appellant_did` fields that appear in requests run through this validator via `@field_validator`. This prevents freeform strings from being stored as actor identifiers — only valid DID-format strings are accepted.

---

## Key Request Models

### `ClaimCreateRequest`

```python
class ClaimCreateRequest(_SanitizedModel):
    execution_id: UUID
    claimant_did: str                            # validated: must start with "did:"
    claim_type: ClaimType                        # Literal["service_failure", "data_misuse",
                                                 #         "wrong_outcome", "unauthorized_action",
                                                 #         "workflow_design_flaw"]
    description: str = Field(min_length=10, max_length=2000)
    harm_value_usd: float | None = Field(None, ge=0.0)
```

`description` has a minimum length of 10 characters — "bad" is not an acceptable claim description. Maximum 2000 characters prevents malicious actors from embedding large payloads in the evidence record. `harm_value_usd` is optional but must be non-negative (`ge=0.0`) if provided.

### `DetermineRequest`

```python
class DetermineRequest(_SanitizedModel):
    determined_by: str    # DID of the human reviewer authorizing determination
```

One field. The complex work (loading context, running the attribution engine, writing the determination) happens in the service layer. The router's job is thin: validate the reviewer DID and pass it to `determine_attribution()`.

### `ResolveRequest`

```python
class ResolveRequest(_SanitizedModel):
    resolution_note: str = Field(min_length=10, max_length=5000)
    resolved_by: str      # DID of the person closing the claim
```

Resolution notes must be at least 10 characters (prevents meaningless closures) and at most 5000 characters (the longest regulatory notes need significant space).

### `AppealRequest`

```python
class AppealRequest(_SanitizedModel):
    appeal_reason: str = Field(min_length=10, max_length=2000)
    appellant_did: str    # must match original claimant_did
```

The `appellant_did` is checked against `claim.claimant_did` in the service layer — not in the model. The model ensures the format is valid; the service ensures the identity matches.

---

## Key Response Models

### `LiabilitySnapshotRecord`

```python
class LiabilitySnapshotRecord(BaseModel):
    snapshot_id: UUID
    execution_id: UUID
    workflow_id: UUID
    agent_did: str
    captured_at: datetime
    workflow_quality_score: float
    workflow_author_did: str
    workflow_validator_did: str | None
    workflow_validation_checklist: dict[str, Any] | None
    step_trust_states: list[SnapshotStepTrustState]
    context_summary: SnapshotContextSummary
    critical_mismatch_count: int
    agent_profile_default_policy: str | None
    created_at: datetime
```

`SnapshotStepTrustState` is a nested model with `trust_score: float | None` — `None` when no service was identified for the step. `SnapshotContextSummary` always has the three field lists (possibly empty) and `mismatch_count`.

### `ClaimDetailResponse`

```python
class ClaimDetailResponse(BaseModel):
    claim_id: UUID
    ...all ClaimResponse fields...
    evidence: list[EvidenceRecord]
    determination: LiabilityDeterminationRecord | None
```

`ClaimDetailResponse` is the full claim with evidence attached — returned by `GET /liability/claims/{claim_id}`. The evidence list is fetched with a separate query in the service layer and joined at response construction time.

### `EvidenceRecord`

```python
class EvidenceRecord(BaseModel):
    evidence_id: UUID
    claim_id: UUID
    evidence_type: str    # one of the 8 evidence type strings
    source_table: str
    source_id: UUID
    source_layer: int     # 1–6
    summary: str
    raw_data: dict[str, Any]    # JSONB copy
    gathered_at: datetime
    created_at: datetime
```

`raw_data` is typed as `dict[str, Any]` — JSONB contents. The shape varies by evidence type: a `context_disclosure` record has `fields_disclosed`, `fields_withheld`; a `trust_attestation` has `evidence_hash`, `attestation_type`.

### `LiabilityDeterminationRecord`

```python
class LiabilityDeterminationRecord(BaseModel):
    determination_id: UUID
    claim_id: UUID
    determination_version: int
    agent_weight: float
    service_weight: float
    workflow_author_weight: float
    validator_weight: float
    agent_did: str
    service_id: UUID | None
    workflow_author_did: str | None
    validator_did: str | None
    attribution_factors: list[AttributionFactor]
    confidence: float
    determined_by: str
    determined_at: datetime
    created_at: datetime
```

The four weights are `float` — the normalization guarantee (`sum = 1.0`) is enforced by `_normalize_weights()` in the service layer, not by a model validator. Model validators on floats are fragile due to floating-point representation; enforcing the invariant in the service ensures it holds before insertion.

### `AttributionFactor`

```python
class AttributionFactor(BaseModel):
    factor: str               # one of the 11 factor names
    actor: str                # "agent", "service", "workflow_author", or "validator"
    weight_contribution: float
    evidence_ids: list[UUID]  # IDs of liability_evidence records that triggered this factor
```

`evidence_ids` links each factor to the specific evidence records that caused it to fire. A human reviewer reading the determination can click through from "service_revoked_before_execution fired" to the exact revocation evidence record.

---

## The Nine Routes

```python
# api/routers/liability.py
router = APIRouter(prefix="/liability")
```

| Method | Path | Auth | Response model |
|--------|------|------|---------------|
| `GET` | `/compliance/export` | API key | `Response` (PDF bytes) |
| `POST` | `/claims` | API key | `ClaimResponse` (201) |
| `GET` | `/claims/{claim_id}` | API key | `ClaimDetailResponse` |
| `POST` | `/claims/{claim_id}/gather` | API key | `EvidenceGatherResponse` |
| `POST` | `/claims/{claim_id}/determine` | API key | `DeterminationResponse` |
| `POST` | `/claims/{claim_id}/resolve` | API key | `ClaimResponse` |
| `POST` | `/claims/{claim_id}/appeal` | API key | `ClaimResponse` |
| `GET` | `/snapshots/{execution_id}` | API key | `LiabilitySnapshotRecord` |
| `GET` | `/snapshots` | Admin API key | `LiabilitySnapshotListResponse` |

**The compliance export returns `Response`, not a Pydantic model:**
```python
@router.get("/compliance/export")
async def export_liability_compliance(...) -> Response:
    pdf_bytes, filename = await liability_compliance.generate_...(...)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

PDF bytes can't be modeled as a Pydantic schema — they're binary content. The `Response` object bypasses FastAPI's serialization and returns raw bytes with the correct `Content-Type: application/pdf` header. Clients receive the file as a download.

**The snapshot list is admin-only:**
```python
@router.get("/snapshots", response_model=LiabilitySnapshotListResponse)
async def list_liability_snapshots(
    api_key: str = Depends(require_admin_api_key),  # admin key required
    ...
)
```

All other routes accept a standard API key. The snapshot list is admin-only because it can expose all executions across all agents — a privacy concern if accessible without elevated authorization.

---

## Exercise 1 — Read the Snapshot Model

After creating a snapshot (Lesson 52):

```bash
EXECUTION_ID="<execution-uuid>"
curl -s "http://localhost:8000/v1/liability/snapshots/$EXECUTION_ID" \
  -H "X-API-Key: dev-local-only" | python -c "
import sys, json
d = json.load(sys.stdin)
print('snapshot_id:', d['snapshot_id'])
print('workflow_quality_score:', d['workflow_quality_score'])
print('critical_mismatch_count:', d['critical_mismatch_count'])
print('step_trust_states count:', len(d['step_trust_states']))
for step in d['step_trust_states']:
    print(f\"  Step {step['step_number']}: {step['ontology_tag']} | trust_score={step['trust_score']}\")
"
```

---

## Exercise 2 — Verify DID Validation

Submit a claim request with an invalid `claimant_did`:

```bash
curl -s -X POST "http://localhost:8000/v1/liability/claims" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"execution_id\": \"$EXECUTION_ID\",
    \"claimant_did\": \"not-a-did\",
    \"claim_type\": \"service_failure\",
    \"description\": \"Test invalid DID\"
  }" | python -m json.tool
```

**Expected:** 422 with validation error: "DID values must start with did:"

---

## Exercise 3 — Inspect Raw Routes

```bash
curl -s "http://localhost:8000/openapi.json" | python -c "
import sys, json
spec = json.load(sys.stdin)
liability_paths = {k: v for k, v in spec['paths'].items() if 'liability' in k}
for path, methods in sorted(liability_paths.items()):
    for method in methods:
        print(f'{method.upper():6} {path}')
" | sort
```

**Expected:** All nine Layer 6 routes, each prefixed with `/v1/liability`.

---

## Interview Q&A

**Q: Why does the compliance export use `Response` instead of a Pydantic model as the return type?**
A: FastAPI serializes Pydantic response models as JSON. PDF content is binary data that cannot be represented as JSON — it would need to be base64-encoded, adding size overhead and requiring the client to decode it. Using a raw `Response` with `media_type="application/pdf"` and a `Content-Disposition` header sends the PDF directly as a file download, which is what regulatory consumers need.

**Q: Why is the weight sum invariant enforced in the service layer rather than as a Pydantic `model_validator`?**
A: A `model_validator` would need to check that `agent_weight + service_weight + workflow_author_weight + validator_weight == 1.0`. Due to floating-point representation, `0.25 + 0.25 + 0.25 + 0.25` is exactly `1.0`, but after factor application and normalization, the sum might be `0.9999999999999998`. A strict equality check would fail. The `_normalize_weights()` function in the service layer handles this precisely by computing the residual and assigning it to the highest-weight actor before the values are stored.

**Q: Why does the `AppealRequest` model accept `appellant_did` from the request rather than reading it from the authenticated API key?**
A: Layer 6 uses API keys for authentication, not agent DIDs. An agent platform might use one API key to file claims on behalf of multiple agents (each with their own DID). The `appellant_did` in the request identifies which agent is appealing, separate from which API key is making the request. The service layer then verifies that the `appellant_did` matches the original `claimant_did` from the stored claim.

---

## Key Takeaways

- All request models inherit from `_SanitizedModel` — null bytes rejected, strings stripped
- DID format validation (`starts with "did:"`) applied at the model layer for actor identifiers
- `ClaimCreateRequest`: `description` min/max length, `harm_value_usd >= 0.0`, `ClaimType` literal constraint
- `AttributionFactor` links factor names to `evidence_ids` — enables click-through from determination to evidence
- Compliance export returns raw `Response(content=pdf_bytes, media_type="application/pdf")` — not a Pydantic model
- Snapshot list (`GET /snapshots`) requires admin API key; all other routes accept standard API key

---

## Next Lesson

**Lesson 58 — Hardening the Trust Layer: Rate Limits, Caching & Load Testing** covers the Layer 6 hardening design — the 10-claim/hour rate limit, the Redis claim status cache, the load test results (p95 < 200ms @ 100 concurrent snapshot reads), and the five key design decisions that prevent the liability layer from becoming an attack surface.
