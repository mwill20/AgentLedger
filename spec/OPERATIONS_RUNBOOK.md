# AgentLedger - Operations Runbook
**Version:** v0.1.0 (POC)
**Last Updated:** April 2026

---

## Local Startup

```bash
# Clone and enter repo
git clone https://github.com/mwill20/AgentLedger.git
cd AgentLedger

# Copy environment config
cp .env.example .env
# Edit .env - set DATABASE_URL, REDIS_URL, API_KEYS at minimum

# Start full stack (app + postgres + redis + celery worker + beat)
docker compose up --build

# Verify health
curl http://localhost:8000/v1/health
# Expected: {"status": "ok", "version": "0.1.0", ...}

# Verify ontology loaded
curl -H "X-API-Key: <your_key>" http://localhost:8000/v1/ontology
# Expected: {"total_tags": 65, ...}
```

---

## Migration Execution

### Fresh database (first run)
```bash
# Migrations run automatically on container startup via entrypoint.sh
# To run manually:
docker compose exec app alembic upgrade head

# Verify
docker compose exec app alembic current
# Expected: 007 (head)

# Verify ontology seeded
docker compose exec app python db/seed_ontology.py
# Expected: "Seeded 65 ontology tags"
```

### Existing database (upgrade)
```bash
# Check current migration state
docker compose exec app alembic current

# Apply any pending migrations
docker compose exec app alembic upgrade head

# Rollback one migration if needed
docker compose exec app alembic downgrade -1
```

### Migration order
```
001_initial_schema          Layer 1 - core registry tables
002_layer2_identity         Layer 2 - agent identities, session assertions
003_layer3_trust            Layer 3 - trust ledger, attestation events
004_layer3_contracts        Layer 3 - smart contract integration stubs
005_layer4_context          Layer 4 - context profiles, disclosures, mismatches
006_layer5_workflows        Layer 5 - workflows, steps, executions, bundles
007_layer6_liability        Layer 6 - snapshots, claims, evidence, determinations
```

---

## Test Commands

```bash
# Run full test suite (no Docker required - uses test DB fixtures)
pytest tests -q

# Run specific layer tests
pytest tests/test_api/test_context_profiles.py -q    # Layer 4
pytest tests/test_api/test_workflow_registry.py -q   # Layer 5
pytest tests/test_api/test_liability_claims.py -q    # Layer 6

# Run with coverage report
$env:COVERAGE_FILE = Join-Path $env:TEMP 'agentledger.coverage'  # Windows
pytest tests -q --cov=api --cov-report=term-missing

# Run load test (requires running Docker stack)
# Set EMBEDDING_MODE=hash, UVICORN_WORKERS=4, IP_RATE_LIMIT=100000 first
locust -f tests/load/locustfile.py --headless -u 100 -r 20 --run-time 60s \
  --host http://localhost:8000
```

---

## Common Failure Modes

| Symptom | Likely Cause | Fix |
|---|---|---|
| `alembic upgrade head` fails | Migration dependency missing | Check `alembic current`, apply missing migrations in order |
| `/v1/ontology` returns 0 tags | Seed script not run | `docker compose exec app python db/seed_ontology.py` |
| `POST /manifests` returns 500 | pgvector extension missing | `docker compose exec db psql -U postgres -d agentledger -c "CREATE EXTENSION vector;"` |
| Celery worker not processing | Redis broker unreachable | Check `REDIS_URL` in `.env`, verify Redis container is healthy |
| Embedding slow or failing | `EMBEDDING_MODE=model` + no GPU | Set `EMBEDDING_MODE=hash` for local/POC use |
| `pytest` coverage fails | `.coverage` file permissions | Use `$env:COVERAGE_FILE` pattern (Windows) or run from `/tmp` |
| `.git/index.lock` error | Interrupted Git operation | `del .git\index.lock` or `rm .git/index.lock` |

---

## Redis Failure Behavior (POC Decision: Fail Open)

The following features degrade gracefully when Redis is unavailable:

| Feature | Redis Down Behavior |
|---|---|
| Query result caching (L1, L3, L4, L5) | Cache miss on every request - slower but functional |
| Rate limiting (per-IP, per-API-key) | Rate limit not enforced - requests pass through |
| Claim filing rate limit (L6) | Rate limit not enforced - claims file normally |
| Claim status cache (L6) | DB read on every status check - slower but accurate |
| Match result cache (L4) | Cache miss - matcher re-runs every time |

**Production note:** For a production deployment, fail-closed rate limiting should be
evaluated. For this POC, fail-open is the correct posture.

---

## Layer 3 Blockchain Status

Layer 3 is **code-complete** but **not deployed to testnet** as of v0.1.0.

- Testnet: Polygon Amoy (chainId 80002)
- Blocked on: Alchemy faucet cooldown for test MATIC
- Resume steps: `spec/LAYER3_DEPLOYMENT_HANDOFF.md`
- Impact on running system: None - blockchain integration stubs return
  graceful responses. Trust scores function using off-chain data only.

---

## Backup and Retention (POC Guidance)

This is a POC. No production backup infrastructure is required. For reference:

| Data | Retention Consideration |
|---|---|
| PostgreSQL | All tables are append-only or versioned - no data is deleted |
| `liability_snapshots` | Permanent - evidentiary anchor for disputes |
| `liability_evidence` | Permanent - may contain GDPR-erased tombstones |
| `context_disclosures` | GDPR right to erasure supported via `erased` flag |
| `compliance_exports` | Log records only - PDF bytes not stored server-side |
| Redis | Cache only - no durable data. Safe to flush at any time |

---

## Observability Checklist (Production Readiness - Future)

Items to add before a production deployment:

- [ ] Structured JSON logging for all API requests
- [ ] Claim filing rate-limit events logged to alerting
- [ ] Layer 6 claim lifecycle transitions logged (filed -> determined -> resolved)
- [ ] Failed PDF export attempts logged with scope details
- [ ] Snapshot creation failures logged as critical (currently fail-closed
      - execution rolls back if snapshot fails)
- [ ] Trust score computation failures logged per service

---

*This runbook covers POC operation. Production deployment requires additional
infrastructure, secrets management, backup configuration, and monitoring.*
