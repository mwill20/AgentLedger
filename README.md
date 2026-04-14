# AgentLedger

Discovery and identity infrastructure for the autonomous agent web.

## Quick Start

```bash
docker compose up --build
```

The default local API key is `dev-local-only` unless you override `API_KEYS`.
The default local admin key is `dev-local-admin` unless you override `ADMIN_API_KEYS`.
For full Layer 2 credential issuance, configure `ISSUER_PRIVATE_JWK` in `.env`.

Once running:

- **Health check:** `GET http://localhost:8000/v1/health`
- **Ontology:** `GET http://localhost:8000/v1/ontology` with `X-API-Key: dev-local-only`
- **Register manifest:** `POST http://localhost:8000/v1/manifests` with `X-API-Key: dev-local-only`
- **Structured query:** `GET http://localhost:8000/v1/services?ontology=travel.air.book` with `X-API-Key: dev-local-only`
- **Semantic query:** `POST http://localhost:8000/v1/search` with `X-API-Key: dev-local-only`
- **Service detail:** `GET http://localhost:8000/v1/services/{service_id}` with `X-API-Key: dev-local-only`
- **Manual DNS verification:** `POST http://localhost:8000/v1/services/{service_id}/verify` with `X-API-Key: dev-local-only`
- **Issuer DID:** `GET http://localhost:8000/v1/identity/.well-known/did.json`
- **Register agent:** `POST http://localhost:8000/v1/identity/agents/register` with `X-API-Key: dev-local-only`
- **Verify agent credential:** `POST http://localhost:8000/v1/identity/agents/verify`
- **Request session assertion:** `POST http://localhost:8000/v1/identity/sessions/request` with `Authorization: Bearer <credential_jwt>`
- **API docs:** `http://localhost:8000/docs`

## Current Architecture

The current build provides five core capabilities:

1. **Ingest** — register and update service manifests from `/.well-known/agent-manifest.json`
2. **Discover** — query the registry with structured ontology filters or semantic search
3. **Verify services** — confirm domain ownership and activate signed `did:web` identity
4. **Verify agents** — issue and validate JWT verifiable credentials
5. **Authorize transactions** — mint short-lived session assertions with optional HITL approval

## Tech Stack

| Component | Technology |
|-----------|-----------|
| API | FastAPI (Python 3.11+) |
| Database | PostgreSQL 15 + pgvector |
| Cache | Redis 7 |
| Crawler | Celery + Redis |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2) |

## Configuration

Environment variables are loaded from `.env` via `pydantic-settings`. Start with [.env.example](.env.example).

Important knobs:

- `API_KEYS`: comma-separated accepted API keys. The Docker default is `dev-local-only`.
- `ADMIN_API_KEYS`: comma-separated admin keys for revocation and approval endpoints.
- `IP_RATE_LIMIT`: per-IP limit for the ASGI rate limiter. Set `0` or lower to disable it.
- `IP_RATE_WINDOW_SECONDS`: time window for the per-IP limit.
- `ISSUER_DID`: DID used as the VC issuer.
- `ISSUER_PRIVATE_JWK`: Ed25519 private JWK used to sign credentials and assertions.
- `SESSION_ASSERTION_TTL_SECONDS`: standard session lifetime.
- `APPROVED_SESSION_TTL_SECONDS`: approved HITL session lifetime.
- `AUTHORIZATION_WEBHOOK_URL`: optional webhook target for pending approvals.
- `EMBEDDING_MODE`: `model` for sentence-transformers, `hash` for fast CPU-only CI/load-test runs.
- `UVICORN_WORKERS`: app worker count used by [entrypoint.sh](entrypoint.sh).

## Testing

Run the full test suite from repo root:

```bash
pytest -q
```

Current local verification:

- full suite: `213 passed`
- Layer 2-focused suite: `34 passed`

Coverage:

```bash
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

Windows note:

```powershell
$env:COVERAGE_FILE = Join-Path $env:TEMP 'agentledger.coverage'
pytest tests --cov=api --cov=crawler --cov-report=term -q
```

## Load Testing

The reusable load harness lives in [tests/load/locustfile.py](tests/load/locustfile.py). It supports endpoint profiles for `health`, `ontology`, `services`, `search`, `service_detail`, `manifests`, `identity_verify`, `identity_lookup`, `identity_mixed`, and `mixed`.

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

- Runtime code lives under `api/`, `crawler/`, `db/`, `ontology/`, and `tests/`.
- The canonical specs live at [spec/LAYER1_SPEC.md](spec/LAYER1_SPEC.md) and [spec/LAYER2_SPEC.md](spec/LAYER2_SPEC.md).
- Completion snapshots live at [spec/LAYER1_COMPLETION.md](spec/LAYER1_COMPLETION.md) and [spec/LAYER2_COMPLETION.md](spec/LAYER2_COMPLETION.md).
- The long-form lessons live under [docs/lessons/00_Index.md](docs/lessons/00_Index.md).
- Mintlify docs live under `docs/`, with research assets in `docs/research/` and internal planning notes in `docs/internal/`.
