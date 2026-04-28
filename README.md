# AgentLedger

Discovery, identity, trust, context, and workflow registry infrastructure for the autonomous agent web.

AgentLedger is infrastructure, not an orchestration runtime. It registers services, verifies identities, computes trust signals, controls context disclosure, and publishes validated workflow specifications that agent platforms can execute.

## Current Status

Layers 1-5 are implemented and tested.

| Layer | Capability | Status |
|---|---|---|
| Layer 1 | Manifest registry, ontology discovery, structured search, semantic search | Complete |
| Layer 2 | Agent identity, verifiable credentials, session assertions, HITL approval | Complete |
| Layer 3 | Auditor network, attestations, revocations, audit chain, trust scoring | Complete |
| Layer 4 | Context profiles, mismatch detection, matching, selective disclosure, compliance PDF export | Complete |
| Layer 5 | Workflow registry, validation queue, ranking, context bundles, execution outcome quality loop | Complete |

Latest local verification:

```bash
pytest tests -q
# 319 passed
```

Layer 5 Phase 6 verification:

- Workflow service coverage: 81% total across Layer 5 service modules
- Cached `GET /v1/workflows`: p95 5.12ms at 100 concurrent requests
- Cached `GET /v1/workflows/{id}/rank`: p95 4.49ms at 100 concurrent requests

## Quick Start

```bash
docker compose up --build
```

Default local keys unless overridden:

- API key: `dev-local-only`
- Admin API key: `dev-local-admin`

For full Layer 2 credential issuance, configure `ISSUER_PRIVATE_JWK` in `.env`.

Useful local URLs:

- API docs: `http://localhost:8000/docs`
- Health: `GET http://localhost:8000/v1/health`
- Ontology: `GET http://localhost:8000/v1/ontology`

Protected endpoints require `X-API-Key: dev-local-only` unless using bearer credential auth where noted.

## API Surface

### Layer 1: Registry and Discovery

- `POST /v1/manifests`
- `GET /v1/services`
- `GET /v1/services/{service_id}`
- `POST /v1/search`
- `GET /v1/ontology`
- `POST /v1/services/{service_id}/verify`

### Layer 2: Identity and Session Assertions

- `GET /v1/identity/.well-known/did.json`
- `POST /v1/identity/agents/register`
- `POST /v1/identity/agents/verify`
- `GET /v1/identity/agents/{did}`
- `POST /v1/identity/agents/{did}/revoke`
- `POST /v1/identity/sessions/request`
- `GET /v1/identity/sessions/{request_id}`
- `POST /v1/identity/sessions/redeem`
- `GET /v1/identity/authorization/pending`
- `POST /v1/identity/authorization/{request_id}/approve`
- `POST /v1/identity/authorization/{request_id}/deny`

### Layer 3: Trust and Verification

- `POST /v1/auditors/register`
- `GET /v1/auditors`
- `GET /v1/auditors/{did}`
- `POST /v1/attestations`
- `POST /v1/attestations/revoke`
- `GET /v1/attestations/{service_id}`
- `GET /v1/attestations/{service_id}/verify`
- `POST /v1/audit/records`
- `GET /v1/audit/records`
- `GET /v1/audit/records/{record_id}/verify`
- `GET /v1/federation/blocklist`
- `POST /v1/federation/revocations/submit`
- `GET /v1/chain/status`
- `GET /v1/chain/events`

### Layer 4: Context Matching and Disclosure

- `POST /v1/context/profiles`
- `GET /v1/context/profiles/{agent_did}`
- `PUT /v1/context/profiles/{agent_did}`
- `POST /v1/context/match`
- `GET /v1/context/mismatches`
- `POST /v1/context/mismatches/{id}/resolve`
- `POST /v1/context/disclose`
- `GET /v1/context/disclosures/{agent_did}`
- `POST /v1/context/revoke/{disclosure_id}`
- `GET /v1/context/compliance/export/{agent_did}`

### Layer 5: Workflow Registry and Quality Signals

- `POST /v1/workflows`
- `PUT /v1/workflows/{workflow_id}`
- `GET /v1/workflows`
- `GET /v1/workflows/{workflow_id}`
- `GET /v1/workflows/slug/{slug}`
- `POST /v1/workflows/{workflow_id}/validate`
- `PUT /v1/workflows/{workflow_id}/validation`
- `GET /v1/workflows/{workflow_id}/rank`
- `POST /v1/workflows/context/bundle`
- `POST /v1/workflows/context/bundle/{bundle_id}/approve`
- `POST /v1/workflows/{workflow_id}/executions`

## Architecture

The current build provides these core capabilities:

1. Ingest service manifests from `/.well-known/agent-manifest.json`.
2. Discover services with ontology filters and semantic search.
3. Verify service domains and activate `did:web` identities.
4. Register agents, issue JWT verifiable credentials, and redeem session assertions.
5. Register auditors, record attestations and revocations, and compute trust scores.
6. Match service context requests against agent-controlled context profiles.
7. Generate HMAC-SHA256 commitments for sensitive context disclosure.
8. Release disclosure nonces only after trust re-verification and append-only audit logging.
9. Export context compliance records as PDF.
10. Publish human-validated workflow specs with ranking and outcome quality signals.

## Tech Stack

| Component | Technology |
|---|---|
| API | FastAPI, Python 3.11+ |
| Database | PostgreSQL 15 + pgvector |
| Cache | Redis 7 |
| Workers | Celery + Redis |
| Embeddings | sentence-transformers, with hash mode for fast tests/load runs |
| Identity crypto | Ed25519, PyJWT |
| Chain integration | Solidity, Hardhat, web3.py, Polygon Amoy/local chain mode |
| PDF export | reportlab |

## Configuration

Environment variables are loaded from `.env` via `pydantic-settings`. Start with [.env.example](.env.example).

Important settings:

- `API_KEYS`: comma-separated accepted API keys.
- `ADMIN_API_KEYS`: comma-separated admin keys for revocation and approval endpoints.
- `DATABASE_URL`: async PostgreSQL connection string.
- `REDIS_URL`: Redis connection string.
- `IP_RATE_LIMIT`: per-IP limit for the ASGI middleware. Set `0` or lower to disable.
- `IP_RATE_WINDOW_SECONDS`: per-IP rate-limit window.
- `ISSUER_DID`: DID used as the VC issuer.
- `ISSUER_PRIVATE_JWK`: Ed25519 private JWK used to sign credentials and assertions.
- `SESSION_ASSERTION_TTL_SECONDS`: standard session lifetime.
- `APPROVED_SESSION_TTL_SECONDS`: approved HITL session lifetime.
- `AUTHORIZATION_WEBHOOK_URL`: optional webhook target for pending approvals.
- `EMBEDDING_MODE`: `model` for sentence-transformers, `hash` for CPU-only CI/load runs.
- `UVICORN_WORKERS`: app worker count used by [entrypoint.sh](entrypoint.sh).
- `CHAIN_MODE`: local or web3-backed chain mode.

## Testing

Run the full test suite from repo root:

```bash
pytest tests -q
```

Run Layer 5 workflow tests:

```bash
pytest tests/test_api/test_workflow_*.py -q
```

Coverage example:

```bash
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

Windows coverage note:

```powershell
$env:COVERAGE_FILE = Join-Path $env:TEMP 'agentledger.coverage'
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

## Load Testing

The reusable load harness lives in [tests/load/locustfile.py](tests/load/locustfile.py).

Documented load-test app mode:

```powershell
$env:API_KEYS='load-test-key'
$env:IP_RATE_LIMIT='100000'
$env:EMBEDDING_MODE='hash'
$env:UVICORN_WORKERS='4'
docker compose up -d --build app
```

Example profile run:

```powershell
$env:LOAD_PROFILE='manifests'
$env:LOAD_API_KEY='load-test-key'
$env:LOAD_FLUSH_RATE_LIMITS='0'
locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 60s --host http://localhost:8000
```

## Project Structure

- Runtime API code: [api/](api/)
- Background workers: [crawler/](crawler/)
- Database migrations and seed scripts: [db/](db/)
- Solidity contracts and scripts: [contracts/](contracts/)
- Ontology source: [ontology/v0.1.json](ontology/v0.1.json)
- Implementation specs and completion summaries: [spec/](spec/)
- Lessons and documentation: [docs/](docs/)
- Tests: [tests/](tests/)
- Docker stack: [docker-compose.yml](docker-compose.yml)

Canonical specs:

- [Layer 1 spec](spec/LAYER1_SPEC.md)
- [Layer 2 spec](spec/LAYER2_SPEC.md)
- [Layer 3 spec](spec/LAYER3_SPEC.md)
- [Layer 4 spec](spec/LAYER4_SPEC.md)
- [Layer 5 spec](spec/LAYER5_SPEC.md)

Completion summaries:

- [Layer 1 completion](spec/LAYER1_COMPLETION.md)
- [Layer 2 completion](spec/LAYER2_COMPLETION.md)
- [Layer 3 completion](spec/LAYER3_COMPLETION.md)
- [Layer 5 completion](spec/LAYER5_COMPLETION.md)

## Layer 6 Handoff

Layer 6 is expected to build on:

- `workflow_executions` for liability attribution.
- `workflow_context_bundles` plus Layer 4 `context_disclosures` for regulatory packages.
- `workflows.quality_score` for risk and insurance underwriting.
- `workflow_validations.validator_did` for validator accountability.
- pinned service revocation state from Layer 3 for dispute resolution.
