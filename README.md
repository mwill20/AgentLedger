# AgentLedger — Layer 1: Manifest Registry

Trust & Discovery Infrastructure for the Autonomous Agent Web.

## Quick Start

```bash
docker compose up --build
```

The default local API key is `dev-local-only` unless you override `API_KEYS`.

Once running:

- **Health check:** `GET http://localhost:8000/v1/health`
- **Ontology:** `GET http://localhost:8000/v1/ontology` with `X-API-Key: dev-local-only`
- **Register manifest:** `POST http://localhost:8000/v1/manifests` with `X-API-Key: dev-local-only`
- **Structured query:** `GET http://localhost:8000/v1/services?ontology=travel.air.book` with `X-API-Key: dev-local-only`
- **Semantic query:** `POST http://localhost:8000/v1/search` with `X-API-Key: dev-local-only`
- **Service detail:** `GET http://localhost:8000/v1/services/{service_id}` with `X-API-Key: dev-local-only`
- **Manual DNS verification:** `POST http://localhost:8000/v1/services/{service_id}/verify` with `X-API-Key: dev-local-only`
- **API docs:** `http://localhost:8000/docs`

## Architecture

Layer 1 provides three capabilities:

1. **Ingest** — crawl and index agent manifests from `/.well-known/agent-manifest.json`
2. **Store** — searchable index of verified service manifests
3. **Serve** — REST API for agent queries (structured + semantic)

## Tech Stack

| Component | Technology |
|-----------|-----------|
| API | FastAPI (Python 3.11+) |
| Database | PostgreSQL 15 + pgvector |
| Cache | Redis 7 |
| Crawler | Celery + Redis |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2) |

## Configuration

Environment variables are loaded from `.env` via `pydantic-settings`. Start with [.env.example](c:\Projects\AgentLedger\.env.example:1).

Important knobs:

- `API_KEYS`: comma-separated accepted API keys. The Docker default is `dev-local-only`.
- `IP_RATE_LIMIT`: per-IP limit for the ASGI rate limiter. Set `0` or lower to disable it.
- `IP_RATE_WINDOW_SECONDS`: time window for the per-IP limit.
- `EMBEDDING_MODE`: `model` for sentence-transformers, `hash` for fast CPU-only CI/load-test runs.
- `UVICORN_WORKERS`: app worker count used by [entrypoint.sh](c:\Projects\AgentLedger\entrypoint.sh:1).

## Testing

Run the full test suite from repo root:

```bash
pytest -q
```

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

The reusable load harness lives in [tests/load/locustfile.py](c:\Projects\AgentLedger\tests\load\locustfile.py:1). It supports endpoint profiles for `health`, `ontology`, `services`, `search`, `service_detail`, `manifests`, and `mixed`.

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
- The canonical implementation spec lives at [spec/LAYER1_SPEC.md](c:\Projects\AgentLedger\spec\LAYER1_SPEC.md:1).
- Mintlify docs live under `docs/`, with research assets in `docs/research/` and internal planning notes in `docs/internal/`.
