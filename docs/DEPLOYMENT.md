# Deployment

## Deployment Status

Current status: Local proof of concept.

The documented deployment target for v0.1.0 is local Docker Compose. This repository has not been documented as production-hardened.

## Local Deployment

```bash
docker compose up -d --build
```

Services:

| Service | Purpose |
|---|---|
| `app` | FastAPI application on port 8000. |
| `db` | PostgreSQL 15 with pgvector. |
| `redis` | Redis cache/broker. |
| `worker` | Celery worker. |
| `beat` | Celery scheduled task runner. |

## Environment Variables

See `.env.example` and `spec/ENV_MATRIX.md`.

Critical variables:

- `DATABASE_URL`
- `DATABASE_URL_SYNC`
- `REDIS_URL`
- `API_KEYS`
- `ADMIN_API_KEYS`
- `ISSUER_PRIVATE_JWK` for full Layer 2 issuance
- `CHAIN_*` and contract variables for Layer 3 chain integration

## Secrets Handling

- Do not commit `.env`.
- Do not commit private keys, API keys, signer keys, database dumps, or production logs.
- Use a secret manager for any production-like environment.
- Rotate local POC keys before any shared deployment.

## Network Exposure

The local compose file exposes:

- API: `localhost:8000`
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

Production exposure rules are TODO and require owner review.

## Scaling

Local compose sets `UVICORN_WORKERS` through environment configuration. Scaling behavior beyond local compose is not yet measured.

## Rollback

For local POC:

```bash
docker compose down
git checkout <previous_commit>
docker compose up -d --build
```

Database rollback requires migration-specific review:

```bash
docker compose exec app alembic downgrade -1
```

Do not downgrade a database with important data without a backup.

## Production Readiness Gaps

- TODO: production hosting target.
- TODO: TLS and ingress configuration.
- TODO: authentication strategy beyond local API keys.
- TODO: secret management plan.
- TODO: backup/restore plan.
- TODO: monitoring and alerting implementation.
- TODO: incident response process.
- TODO: legal/security review.
