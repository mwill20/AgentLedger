# Monitoring And Maintenance

## Current Status

Monitoring is documented for local proof-of-concept operation only. Production monitoring is not yet implemented.

## Health Checks

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

## Logs

Local Docker logs:

```bash
docker compose logs app --tail 100
docker compose logs worker --tail 100
docker compose logs beat --tail 100
docker compose logs db --tail 100
docker compose logs redis --tail 100
```

## Monitoring Plan

| Area | What To Monitor | Why It Matters | Status |
|---|---|---|---|
| API health | `/v1/health` status | Detect app availability failures. | Local only. |
| API errors | 4xx/5xx rate by route | Detect validation, auth, or server issues. | TODO. |
| Latency | p50/p95/p99 by route | Detect performance regressions. | TODO. |
| Database | connection count, slow queries, migration state | Protect source-of-truth storage. | TODO. |
| Redis | availability and latency | Cache, broker, and rate-limit behavior. | TODO. |
| Celery | queue length, task failures | Background verification/crawler reliability. | TODO. |
| Security | auth failures, rate-limit events, revocation events | Detect misuse. | TODO. |
| Context disclosures | mismatch events and erasure requests | Privacy and audit integrity. | TODO. |
| Liability claims | filed/determined/resolved counts | Dispute workflow health. | TODO. |

## Maintenance Tasks

| Task | Frequency | Owner |
|---|---|---|
| Dependency review | TODO | TODO |
| Security review | Deferred for POC | TODO |
| Legal review | Deferred for POC | TODO |
| Database backup verification | TODO | TODO |
| Test suite run | Before release tags | Maintainer |
| Layer 3 testnet deployment retry | When faucet/testnet credentials are available | TODO |

## Drift Monitoring

No trained model is included. If `EMBEDDING_MODE=model` is used, monitor semantic search relevance and document model version/cache source in `docs/MODEL_CARD.md`.
