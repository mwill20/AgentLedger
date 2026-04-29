# Troubleshooting

## Docker Services Do Not Start

Check service status:

```bash
docker compose ps
docker compose logs app --tail 100
```

Common causes:

- Docker Desktop is not running.
- Ports `8000`, `5432`, or `6379` are already in use.
- Environment variables in `.env` override safe local defaults.

## Database Migrations Fail

Run:

```bash
docker compose exec app alembic current
docker compose exec app alembic upgrade head
```

If using a disposable local database, reset volumes only when you are comfortable deleting local state:

```bash
docker compose down -v
docker compose up -d --build
```

## Protected Endpoint Returns 401

Use the local API key:

```bash
curl -H "X-API-Key: dev-local-only" http://localhost:8000/v1/ontology
```

If you changed `API_KEYS`, use one of the configured values.

## Layer 2 Issuance Fails

Full credential/session issuance requires `ISSUER_PRIVATE_JWK`. Leave this blank for basic local smoke tests, or configure a valid Ed25519 private JWK for full issuance.

## Layer 3 Chain Calls Do Not Write To Testnet

Layer 3 testnet deployment is deferred in v0.1.0. Live writes require:

- RPC URL.
- signer private key.
- deployed contract addresses.
- funded testnet wallet.

Use `GET /v1/chain/status` for local smoke checks.

## Host-Side Pytest Plugin Conflict

If unrelated third-party pytest plugins fail during test startup, run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytest_asyncio tests -q
```

Windows PowerShell:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
pytest -p pytest_asyncio tests -q
```

## Redis Or Celery Is Unavailable

POC behavior is fail open for several Redis-backed features. The API should still respond, but caches, rate-limit enforcement, and queued background work may degrade.

Production deployments should revisit this behavior.

## Search Results Differ Between Environments

`EMBEDDING_MODE=hash` is deterministic and fast but less semantically rich. `EMBEDDING_MODE=model` uses sentence-transformers when available and may require model downloads or cached model files.
