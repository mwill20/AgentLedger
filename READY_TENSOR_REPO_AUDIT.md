# Ready Tensor Repository Audit

## Summary

AgentLedger is an agentic AI infrastructure/API service repository with Docker, FastAPI, PostgreSQL, Redis, Celery, Solidity contracts, tests, and extensive layer specs. The repository already has strong implementation notes and test coverage artifacts, but the reviewer-facing documentation is incomplete for Ready Tensor-style publication: there is no root security policy, no license file, no examples directory, and several required reviewer questions are only partially answered in `README.md`.

This audit was created before documentation changes in Documentation Fix mode.

## Scorecard

| Area | Status | Priority | Notes |
|---|---|---|---|
| Purpose and audience | PARTIAL | High | README explains the project but does not clearly identify intended audience and out-of-scope users near the top. |
| Installation and quickstart | PARTIAL | High | Docker command exists, but clone/setup/env/migration checks and expected success signals need clearer documentation. |
| Usage examples | PARTIAL | High | API surface is listed, but runnable request examples and sample inputs/outputs are missing. |
| Architecture documentation | PARTIAL | High | Existing `docs/architecture/*.mdx` and specs are useful, but there is no reviewer-oriented `docs/ARCHITECTURE.md` with system overview, data flow, trust boundaries, and trade-offs. |
| Dependencies and environment | PARTIAL | High | `.env.example`, `requirements.txt`, `package.json`, Dockerfile, and compose exist; a consolidated environment/dependency guide is needed. |
| Evaluation and results | PARTIAL | High | README states test and load-test results, but evaluation procedure, reproducibility commands, and unmeasured areas need a dedicated document. |
| Dataset documentation | PARTIAL | Medium | The project uses an ontology file, not a training dataset; this should be documented explicitly. |
| Model documentation | PARTIAL | Medium | The project uses sentence-transformers optionally and hash embeddings locally; no model card explains source, mode, limitations, or missing license facts. |
| Security documentation | FAIL | High | No root `SECURITY.md`; agent, context, identity, revocation, and liability surfaces need threat-model and reporting guidance. |
| Deployment documentation | PARTIAL | Medium | Docker compose exists and release notes say local-only POC; a deployment document should spell out target, env vars, secrets, and production gaps. |
| Monitoring/maintenance | PARTIAL | Medium | Logs, health check, Redis fail-open behavior, and metrics to monitor are not consolidated. |
| Limitations and trade-offs | PARTIAL | High | Some limitations appear in release notes/specs; reviewer-facing limitations should be central and explicit. |
| License and usage rights | FAIL | High | No root `LICENSE` file was found. Usage rights are unclear until the owner selects a license. |
| Support/contact | FAIL | Medium | README does not clearly define issue/support/security reporting paths. |
| Visual demo/assets | PARTIAL | Medium | Existing docs include architecture prose; no screenshots/assets directory. A text diagram is acceptable for now. |

## Current Strengths

- Multi-service local stack exists through `docker-compose.yml`.
- API, worker, database migrations, ontology seed, Solidity contracts, and tests are present.
- `.env.example` exists with safe placeholder/default local values.
- Layer specs and completion summaries exist under `spec/`.
- Existing README lists API surface and core architecture capabilities.
- Existing docs include architecture MDX pages, threat model MDX, ontology docs, and long-form lessons.
- Tests exist under `tests/`, including API, crawler, integration, and load-test harnesses.

## Missing Files Or Gaps

- `LICENSE` is missing. Do not infer a license automatically.
- `SECURITY.md` is missing.
- `examples/` is missing.
- Reviewer-focused docs are missing: `docs/ARCHITECTURE.md`, `docs/INSTALLATION.md`, `docs/USAGE.md`, `docs/EVALUATION.md`, `docs/LIMITATIONS.md`, `docs/DEPLOYMENT.md`, `docs/MONITORING.md`, `docs/TROUBLESHOOTING.md`, `docs/DATASET.md`, and `docs/MODEL_CARD.md`.
- README needs a clearer purpose, audience, requirements, access/availability, inputs/outputs, evaluation, limitations, security, license, support, and references.
- Existing README completion-summary list is incomplete: it omits Layer 4 and Layer 6 completion links even though those files exist under `spec/`.

## Reproducibility Gaps

- The Docker quickstart lacks complete expected-success checks in one place.
- Host Python test runs may need `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` due third-party pytest plugin interactions in some environments.
- Fresh database migration verification is not documented in README.
- Layer 2 full issuance requires `ISSUER_PRIVATE_JWK`; this is not prominently called out in quickstart.
- Layer 3 testnet deployment is deferred; local/status smoke behavior should be documented without implying live chain deployment.

## Security And Licensing Gaps

- No root security policy or vulnerability reporting path exists.
- No explicit security assumptions, threat boundaries, secrets handling, or abuse cases are in root documentation.
- `.env` exists locally but is ignored; documentation should warn never to commit it.
- No license file exists. Until the project owner selects and adds a license, reuse rights are unclear.

## Recommended File Changes

1. Update `README.md` to answer Ready Tensor reviewer questions in the first few minutes.
2. Add `SECURITY.md` with assumptions, threat model summary, sensitive data handling, abuse cases, and reporting TODOs.
3. Add reviewer-focused docs under `docs/`: installation, usage, architecture, evaluation, limitations, deployment, monitoring, troubleshooting, dataset, and model card.
4. Add `examples/` sample manifest input and representative response shape.
5. Add license TODO references without creating or choosing a license.

## Priority Order For Fixes

1. README purpose, audience, quickstart, usage.
2. License and access status.
3. Dependency/environment documentation.
4. Architecture documentation.
5. Evaluation documentation.
6. Security and limitations documentation.
7. Dataset/model documentation.
8. Deployment and monitoring documentation.
9. Examples and text diagrams.
