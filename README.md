# AgentLedger — Layer 1: Manifest Registry

Trust & Discovery Infrastructure for the Autonomous Agent Web.

## Quick Start

```bash
docker compose up --build
```

Once running:

- **Health check:** `GET http://localhost:8000/v1/health`
- **Ontology:** `GET http://localhost:8000/v1/ontology` with `X-API-Key: dev-local-only`
- **Register manifest:** `POST http://localhost:8000/v1/manifests` with `X-API-Key: dev-local-only`
- **Structured query:** `GET http://localhost:8000/v1/services?ontology=travel.air.book` with `X-API-Key: dev-local-only`
- **Semantic query:** `POST http://localhost:8000/v1/search` with `X-API-Key: dev-local-only`
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

## Project Structure

- Runtime code lives under `api/`, `crawler/`, `db/`, `ontology/`, and `tests/`.
- The canonical implementation spec lives at `spec/LAYER1_SPEC.md`.
- Mintlify docs now live under `docs/`, with research assets in `docs/research/` and internal planning notes in `docs/internal/`.
