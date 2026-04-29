# AgentLedger v0.1.0 - Release Notes

**Release Date:** April 2026
**Type:** Proof of Concept
**Branch:** `main`
**Tests:** 346 passed, 0 failures

---

## What This Is

AgentLedger v0.1.0 is a complete proof-of-concept implementation of a six-layer
trust and discovery infrastructure for the autonomous agent web. It demonstrates
the full architecture from manifest discovery through liability attribution.

This is not a production deployment. It is a working, tested, documented reference
implementation of the AgentLedger architecture.

---

## What's Included

### Layer 1 - Manifest Registry
Universal discovery API for agent-native services. Structured and semantic queries,
capability ontology (5 domains, 20 branches, 65 tags), trust tiering, crawler
infrastructure.

### Layer 2 - Identity & Attestation
Agent identity via W3C DID standards (did:key, did:web). JWT-based Verifiable
Credentials, Ed25519 keypairs, human-in-the-loop interrupt for sensitivity_tier >= 3.

### Layer 3 - Trust & Verification
Blockchain-anchored trust attestation design. **Code-complete. Testnet deployment
deferred** - blocked on Polygon Amoy faucet. Off-chain trust scoring fully operational.

### Layer 4 - Context Matching
Privacy-preserving context disclosure. HMAC-SHA256 commitment scheme, profile-based
field classification (permit/withhold/commit), mismatch detection, GDPR right-to-erasure,
compliance PDF export.

### Layer 5 - Orchestration & Taste
Workflow registry with human validation queue. Context bundles, scoped profiles,
quality score computation with anti-gaming cap (70.0 ceiling on unverified outcomes),
Layer 4 audit trail cross-verification.

### Layer 6 - Liability
Synchronous liability snapshots at execution time, 8-source evidence gathering,
11-factor attribution engine (weights always sum to 1.0), EU AI Act / HIPAA / SEC
compliance PDF export.

---

## Architecture Numbers

| Metric | Value |
|---|---|
| Database tables | 40+ across 7 migrations |
| API endpoints | 50+ across 8 routers |
| Test files | 30+ |
| Tests passing | 346 |
| Capability ontology tags | 65 |
| Attribution factors | 11 |
| Layers | 6 of 6 |

---

## Known Deferred Items

| Item | Status |
|---|---|
| Layer 3 testnet deployment | Blocked on Polygon Amoy faucet. Resume steps in `spec/LAYER3_DEPLOYMENT_HANDOFF.md` |
| Full ZKP circuits (circom/snark.js) | Deferred to v0.2 - Layer 4 uses HMAC-SHA256 commitments |
| OAuth2 auth | Deferred to v0.2 - API key auth in v0.1 |
| Licensed insurance underwriting | Out of scope - requires licensed insurers |
| Binding legal determinations | Out of scope - Layer 6 produces evidence, not rulings |
| Smart contract escrow | Requires Layer 3 blockchain deployment |
| Production deployment | Not in scope for POC |

---

## Legal Scope

AgentLedger v0.1.0 is evidence infrastructure. It:

- **Does** produce attribution evidence for disputes
- **Does not** issue binding legal rulings
- **Does not** underwrite insurance products
- **Does not** process payment settlements
- **Does not** operate regulated financial escrow
- **Does not** constitute legal or financial advice

---

## How to Run

```bash
git clone https://github.com/mwill20/AgentLedger.git
cd AgentLedger
cp .env.example .env
docker compose up --build
curl http://localhost:8000/v1/health
```

See `OPERATIONS_RUNBOOK.md` for full operational guidance.
