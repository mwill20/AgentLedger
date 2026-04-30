# Installation

This guide describes the local proof-of-concept setup for AgentLedger.

## Requirements

| Requirement | Version / Notes |
|---|---|
| Python | 3.11+ is used by the Docker image. Host tests in this workspace were run with Python 3.12. |
| Docker | Required for the recommended local stack. |
| Docker Compose | Required for PostgreSQL, Redis, API, worker, and beat services. |
| PostgreSQL | Provided by Docker Compose using `pgvector/pgvector:pg15`. |
| Redis | Provided by Docker Compose using `redis:7-alpine`. |
| Node.js/npm | Required only for Layer 3 Solidity contract development and tests. Observed locally with Node.js `v22.20.0` and npm `10.9.3`. |
| GPU | Not required for local POC mode. |
| External APIs | Not required for local POC mode. Layer 3 testnet deployment requires RPC access and funded testnet credentials. |

## Clone

```bash
git clone https://github.com/mwill20/AgentLedger.git
cd AgentLedger
```

## Environment

Start from the example environment file:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Important local defaults:

| Variable | Local Default | Notes |
|---|---|---|
| `API_KEYS` | `dev-local-only` | Used as `X-API-Key` for protected endpoints. |
| `ADMIN_API_KEYS` | `dev-local-admin` | Used for admin-only flows. |
| `EMBEDDING_MODE` | `hash` in Docker Compose | Fast deterministic fallback for local POC and tests. |
| `ISSUER_PRIVATE_JWK` | blank | Required for full Layer 2 credential issuance. |
| `CHAIN_MODE` | `auto` unless overridden | Local/status flows work without a live testnet write. |

Do not commit `.env`, private keys, API keys, database dumps, or local logs.

## Docker Quickstart

```bash
docker compose up -d --build
```

Expected success signals:

```bash
docker compose ps
curl http://localhost:8000/v1/health
curl -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
```

Expected high-level results:

- Docker services are running.
- `GET /v1/health` returns `{"status":"ok", ...}`.
- `GET /v1/ontology` returns `total_tags: 65`.

## Database Migrations

Migrations run automatically through `entrypoint.sh` when the app container starts.

Manual migration commands:

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic current
```

Expected migration head for v0.1.0:

```text
007 (head)
```

Seed ontology manually if needed:

```bash
docker compose exec app python db/seed_ontology.py
```

## Host Python Setup

The Docker path is recommended for reviewers. If running host-side tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If third-party pytest plugins conflict with the test run, use:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytest_asyncio tests -q
```

Windows PowerShell:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
pytest -p pytest_asyncio tests -q
```

## Contract Tooling

Install Node dependencies only if you need Layer 3 contract development:

```bash
npm install
npm run contracts:test
```

Layer 3 testnet deployment is deferred for this POC. See `spec/LAYER3_DEPLOYMENT_HANDOFF.md` if present, or the Layer 3 spec and completion notes under `spec/`.
