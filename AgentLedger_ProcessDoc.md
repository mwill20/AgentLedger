# AgentLedger Process Doc

## Layer 5 Completion Log

**Date:** 2026-04-28
**Milestone:** Layer 5 complete - Orchestration & Taste
**Verification:** 319 tests passed; 81% coverage across workflow services; cached rank p95 4.49ms at 100 concurrent requests

Layer 5 is complete across all six phases:

1. Phase 1 - workflow registry CRUD, migration 006, workflow steps, and test fixtures.
2. Phase 2 - human validation queue, validator assignment, deterministic spec hash, and published-spec immutability.
3. Phase 3 - ranking engine, quality score computation, trust-tier filtering, and compound Redis rank cache keys.
4. Phase 4 - workflow context bundles, scoped profile overrides, approval flow, and Layer 4 can_disclose integration.
5. Phase 5 - execution outcome reporting, atomic counters, async verification against Layer 4 disclosure evidence, and quality recompute.
6. Phase 6 - workflow read caching, cache invalidation, workflow query rate limits, Layer 3 revocation-triggered re-validation, coverage, and load verification.

## Layer 5 Key Decisions

- **Nullable `workflow_steps.service_id`:** Steps remain flexible by default. A null `service_id` means any service capable of the step's ontology tag can be ranked; a non-null value pins the workflow to a specific service and participates in revocation-triggered re-validation.
- **Optional pinning with required-step revocation cascade:** If Layer 3 revokes a pinned service used by a required workflow step, Layer 5 flags the published workflow for re-validation and clears stale workflow caches.
- **Compound rank cache key:** `GET /workflows/{id}/rank` caches by `workflow_id`, `geo`, `pricing_model`, and `agent_did`. This prevents filtered ranking results from contaminating unfiltered or agent-specific cache hits.
- **`WORKFLOW_VERIFY_SYNC` test mode:** Execution verification runs asynchronously in production but can run inline in tests so verification and quality-score changes are deterministic.
- **Scoped profile FK fix:** `workflow_context_bundles.scoped_profile_id` references `workflow_scoped_profiles(id)`, not base `context_profiles(id)`, because scoped overrides are workflow-local records.
- **Atomic execution counters:** Execution, success, and failure counts use a single SQL `UPDATE ... CASE` statement rather than application read-modify-write.
- **Layer 4 evidence over reporter trust:** Reported outcomes start unverified and only gain verification weight when Layer 4 disclosure evidence exists for required workflow steps.

## Closed Layer 5 Questions

- **Should workflow steps require a fixed service?** Closed. `service_id` is nullable with optional pinning to preserve ranking flexibility while still supporting deterministic, pinned workflows.
- **How should filtered rank results cache?** Closed. Cache keys include all ranking filters plus `agent_did`.
- **How should async verification be testable?** Closed. Use the `WORKFLOW_VERIFY_SYNC` environment/test flag to execute verification inline.
- **Where should scoped profile bundles point?** Closed. Bundle `scoped_profile_id` points to `workflow_scoped_profiles`.
- **What happens when a pinned service is revoked?** Closed. Required pinned-service revocation moves the workflow back to review and invalidates caches.
- **Can unverified success reports inflate workflow quality?** Closed. The verification-rate cap keeps quality_score at or below 70.0 when verification_rate is below 0.5.

## Layer 5 Follow-Up Boundaries

- Layer 6 should treat `workflow_executions`, `workflow_context_bundles`, and Layer 4 `context_disclosures` as stable liability inputs.
- Layer 6 should use workflow quality_score as a risk signal, not as a final liability determination.
- Workflow execution remains outside AgentLedger. Agent platforms execute; AgentLedger validates, ranks, audits, and scores.

## Layer 4 Completion Log

**Date:** 2026-04-28
**Milestone:** Layer 4 complete - Context Matching
**Verification:** 273 tests passed

Layer 4 is complete across all six phases:

1. Phase 1 - schema, profile CRUD, profile rules, and default test fixtures.
2. Phase 2 - mismatch detection before profile evaluation, persistence, listing, and resolution flow.
3. Phase 3 - matching engine, trust-tier gates, HMAC commitments, and Redis match caching.
4. Phase 4 - selective disclosure, nonce release, Redis-to-Postgres match fallback, audit trail, and erasure flow.
5. Phase 5 - compliance PDF export using in-memory `BytesIO` generation.
6. Phase 6 - hardening, profile cache TTL, match rate limiting, disclose-time trust cache checks, coverage, and load verification.

## Key Decisions

- **HMAC-SHA256 commitments for v0.1:** Layer 4 uses `HMAC-SHA256(key=nonce, msg=field_value)` commitments instead of full zero-knowledge proofs for the first implementation. This keeps v0.1 auditable, testable, and easy for services to verify while preserving a later upgrade path to ZK disclosure proofs.
- **Nonce remains server-side until disclosure:** Match returns commitment identifiers and hashes, not plaintext sensitive values. Disclosure releases nonces only after the service trust tier is rechecked.
- **Redis-to-Postgres fallback:** Redis is used as the fast path for match/profile/trust caches, but Postgres remains the source of truth. A Redis miss cannot create a false 410 for disclosure if unexpired commitments still exist in Postgres.
- **Trust recheck at disclosure:** Disclosure is blocked with 403 if service trust has dropped below the required tier since match. It is not degraded into a withheld-field response.
- **BytesIO PDF pattern:** Compliance exports are generated entirely in memory with ReportLab and `BytesIO`, then returned as `application/pdf`. No temp files are written.
- **Append-only disclosure records:** Erasure marks records as erased and clears field metadata, but the audit row remains for integrity.

## Closed Layer 4 Questions

- **HMAC vs. full ZKP for v0.1:** Closed. Use HMAC-SHA256 commitments now; defer full ZKP proof systems to a later privacy-hardening milestone.
- **Cache authority:** Closed. Redis is a cache only. Postgres remains authoritative for profiles, commitments, disclosures, mismatches, and compliance exports.
- **Expired or unknown match behavior:** Closed. Disclosure returns 410 Gone when no valid match can be found for the agent/service pair.
- **Required vs. optional trust failures:** Closed. Insufficient trust for required fields returns 403; optional fields are withheld during match. At disclosure time, insufficient trust hard-blocks.
- **Compliance export storage:** Closed. Generate PDFs in memory with `BytesIO`; do not write export artifacts to disk.

## Follow-Up Boundaries

- Layer 5 should consume Layer 4 through the public context endpoints rather than reading Layer 4 tables directly.
- Full ZKP selective disclosure remains a future upgrade, not a v0.1 blocker.
- Layer 6 can rely on context disclosure audit rows and compliance exports as stable inputs.
