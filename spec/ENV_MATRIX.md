# AgentLedger - Environment Variable Matrix
**Version:** v0.1.0 (POC)

---

## Required Variables

These must be set for the application to start.

| Variable | Example | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:password@db:5432/agentledger` | Async PostgreSQL connection string |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection - cache, rate limiting, Celery broker |
| `API_KEYS` | `dev-key-1,dev-key-2` | Comma-separated valid API keys for `X-API-Key` auth |

---

## Optional Variables (with defaults)

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODE` | `model` | `model` = sentence-transformers (slow, accurate); `hash` = fast deterministic fallback (recommended for POC/local) |
| `UVICORN_WORKERS` | `4` | App process count |
| `IP_RATE_LIMIT` | `100` | Max requests per IP per window |
| `IP_RATE_WINDOW_SECONDS` | `60` | Rate limit window in seconds |
| `WORKFLOW_VERIFY_SYNC` | `false` | `true` = run workflow execution verification synchronously (use in tests only) |
| `LOG_LEVEL` | `info` | Uvicorn log level |

---

## Layer-Specific Variables

| Variable | Layer | Purpose |
|---|---|---|
| `ALCHEMY_RPC_URL` | L3 | Polygon Amoy RPC endpoint for blockchain ops (not required until L3 deployed) |
| `DEPLOYER_PRIVATE_KEY` | L3 | Hardhat deployer wallet key (never commit - set via secret manager) |
| `CHAIN_ID` | L3 | `80002` for Polygon Amoy |

---

## Secrets - Never Commit

| Secret | Where to Set |
|---|---|
| `DEPLOYER_PRIVATE_KEY` | `.env` (gitignored) or secret manager |
| `DATABASE_URL` (with password) | `.env` (gitignored) |
| Any production `API_KEYS` | `.env` (gitignored) or secret manager |

`.env` is in `.gitignore`. Never commit real credentials to the repository.

---

## Local Docker Compose Defaults

`docker-compose.yml` sets these automatically for local use:

```
DATABASE_URL=postgresql+asyncpg://postgres:password@db:5432/agentledger
REDIS_URL=redis://redis:6379/0
EMBEDDING_MODE=hash
API_KEYS=dev-key-changeme
```

Copy `.env.example` to `.env` and override as needed.

---

## Test Environment

Tests use in-process fixtures and do not require `.env`. Exception:

```powershell
# Windows: avoid .coverage permission issues
$env:COVERAGE_FILE = Join-Path $env:TEMP 'agentledger.coverage'
```
