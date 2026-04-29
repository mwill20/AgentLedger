# Usage

AgentLedger exposes a FastAPI service under `/v1`. Local interactive docs are available at:

```text
http://localhost:8000/docs
```

Protected endpoints require an API key unless the endpoint documentation says otherwise:

```http
X-API-Key: dev-local-only
```

## Health Check

```bash
curl http://localhost:8000/v1/health
```

Expected success shape:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "timestamp": "..."
}
```

## Read The Ontology

```bash
curl -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
```

Expected success signal:

```text
HTTP 200 with ontology_version "0.1" and total_tags 65.
```

## Register A Service Manifest

Use the sample manifest:

```bash
curl -X POST http://localhost:8000/v1/manifests \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-local-only" \
  --data @examples/service_manifest.sample.json
```

Representative response shape:

```json
{
  "service_id": "00000000-0000-4000-8000-000000000101",
  "trust_tier": 1,
  "trust_score": 0.0,
  "status": "registered",
  "capabilities_indexed": 1,
  "typosquat_warnings": []
}
```

Actual `trust_score`, status, and warnings depend on the manifest and current database state.

## Search Services

```bash
curl -X POST http://localhost:8000/v1/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-local-only" \
  -d '{"query":"book a flight","limit":5}'
```

Expected success signal:

```text
HTTP 200 with a results array. Results depend on registered manifests and embedding mode.
```

## Layer Smoke Paths

| Layer | Minimal local smoke action |
|---|---|
| Layer 1 | `POST /v1/manifests` with `examples/service_manifest.sample.json`. |
| Layer 2 | `GET /v1/identity/.well-known/did.json`. Full issuance requires `ISSUER_PRIVATE_JWK`. |
| Layer 3 | `GET /v1/chain/status`. Testnet writes require configured RPC, contracts, and funded key. |
| Layer 4 | `POST /v1/context/profiles` or `GET /v1/context/profiles/{agent_did}` for a seeded profile. |
| Layer 5 | `POST /v1/workflows` or `GET /v1/workflows`. |
| Layer 6 | `GET /v1/liability/snapshots/{execution_id}` after a workflow execution report creates a snapshot. |

## Inputs And Outputs

| Input / Output | Format | Location / Endpoint |
|---|---|---|
| Ontology source | JSON | `ontology/v0.1.json` |
| Service manifest input | JSON | `POST /v1/manifests`; sample in `examples/service_manifest.sample.json` |
| Workflow spec input | JSON | `POST /v1/workflows`; see `spec/LAYER5_SPEC.md` |
| Context profile input | JSON | `POST /v1/context/profiles`; see `spec/LAYER4_SPEC.md` |
| Liability/compliance exports | PDF | Layer 4 and Layer 6 export endpoints |
| API responses | JSON | FastAPI endpoints under `/v1` |

## API Reference

Use the local OpenAPI UI for complete request and response schemas:

```text
http://localhost:8000/docs
```
