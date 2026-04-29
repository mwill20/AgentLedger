# Evaluation

## Evaluation Objective

The repository should demonstrate that AgentLedger can run locally, expose the expected API surface, migrate its database, and pass its automated tests for the six-layer proof of concept.

## Evaluation Questions

1. Can the local Docker stack start and expose the API?
2. Do database migrations apply through the current head revision?
3. Do the core endpoints respond?
4. Do automated tests pass?
5. Are Layer 1-6 smoke paths documented and reproducible?

## Automated Test Procedure

From the repository root:

```bash
pytest tests -q
```

If host-side pytest plugin autoload causes unrelated third-party plugin conflicts:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytest_asyncio tests -q
```

Windows PowerShell:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
pytest -p pytest_asyncio tests -q
```

## Current Recorded Results

| Check | Result | Notes |
|---|---|---|
| Full test suite | 346 passed | Observed in this workspace before this documentation update. |
| `/v1/health` | HTTP 200 | Observed in local Docker stack. |
| `/docs` | HTTP 200 | Observed in local Docker stack. |
| `/v1/ontology` | HTTP 200, 65 tags | Observed with `X-API-Key: dev-local-only`. |
| Database migrations | Head `007` | Observed during local Docker smoke test. |

Do not treat these as production benchmarks. They are local POC validation results.

## Load And Performance Evidence

Existing README/release notes record local hardening/load-test observations for Layer 5 and Layer 6. Re-run those load tests before using the numbers in a publication or external claim.

| Metric | Current Status |
|---|---|
| End-to-end production latency | Not yet measured. |
| Memory usage | Not yet measured. |
| Sustained throughput | Not yet measured. |
| Failure recovery time | Not yet measured. |
| Hosted deployment performance | Not applicable for local-only v0.1.0 POC. |

## Manual Review Procedure

1. Read `README.md`.
2. Run the Docker quickstart in `docs/INSTALLATION.md`.
3. Run one request from `docs/USAGE.md`.
4. Inspect `/docs` for OpenAPI schemas.
5. Run `pytest tests -q`.
6. Review `docs/LIMITATIONS.md` and `SECURITY.md` before making any production or legal claim.

## Known Failure Cases

- Full Layer 2 issuance fails unless `ISSUER_PRIVATE_JWK` is configured.
- Layer 3 testnet writes require RPC configuration, deployed contracts, and funded testnet credentials.
- Host-side tests may need pytest plugin autoload disabled depending on the Python environment.
- Redis unavailability degrades cache/rate-limit behavior in fail-open POC mode.

## Reproducibility Notes

- Use Docker Compose for the most reproducible local stack.
- Use `.env.example` as the starting point for environment configuration.
- Do not reuse local `.env` secrets or generated keys across environments.
- If publishing results, record host OS, Python version, Docker version, commit SHA, environment variables, and exact commands.
