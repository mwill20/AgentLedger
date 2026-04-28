# AgentLedger Process Doc

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
