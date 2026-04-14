# AgentLedger — Layer 3 Architecture Design
## Trust & Verification: Blockchain-Anchored Trust Ledger + Audit Chain

**Version:** 0.1  
**Status:** In Design  
**Author:** Michael Williams  
**Last Updated:** April 2026

---

## Layer Context

Layer 1 answered: *"What services exist and what can they do?"*  
Layer 2 answered: *"Who is making this call, and are they who they claim to be?"*  
Layer 3 answers: **"Has this service actually earned trust — and can you prove it without asking us?"**

Layer 3 has two parallel concerns that must be designed and built together:

- **The Trust Ledger** — on-chain attestations from a registered auditor network
- **The Audit Chain** — tamper-proof records of agent transactions, hash-anchored on-chain

Both live in Layer 3 because they share the same blockchain infrastructure. The Audit Chain cannot be retroactively constructed — it must be present from the first agent transaction.

---

## The Central Design Decision: Chain Selection

This was the open question from the process document. Closed here with a full decision.

### Evaluation Grid

| Chain | Write Cost | Finality | Decentralization | EVM Compatible | Verdict |
|---|---|---|---|---|---|
| Ethereum mainnet | ~$2–10/tx | 12 sec | Maximum | Yes | ❌ Too expensive at scale |
| Hyperledger Fabric | $0 (permissioned) | <1 sec | Minimal (you run it) | No | ❌ Defeats the trust argument |
| Solana | ~$0.00025/tx | 0.4 sec | Good | No | ⚠️ Ecosystem mismatch |
| Polygon PoS | ~$0.001/tx | 2 sec | Good | Yes | ✅ v0.1 target |
| OP Stack rollup | ~$0.0001/tx | 2 sec | Good + Ethereum security | Yes | ✅ NorthStar target |

### Decision

**Polygon PoS for v0.1, with a defined migration path to a custom OP Stack rollup.**

**Rationale:** Polygon PoS is battle-tested, EVM-compatible (all Ethereum tooling works), has 2-second finality, and costs fractions of a cent per transaction. The custom OP Stack rollup is the NorthStar — a chain purpose-built for AgentLedger attestation throughput with Ethereum as the settlement layer — but it requires validator infrastructure that doesn't make sense until we have transaction volume to justify it. Polygon gets us to production. The rollup is a Phase 2 upgrade that doesn't break any contracts.

### On-Chain Tooling Stack

| Component | Technology | Reason |
|---|---|---|
| Smart contract language | Solidity | EVM standard, maximum tooling support |
| Development framework | Hardhat | Testing, deployment, scripting |
| Contract library | OpenZeppelin | Access control, upgradeability, audited |
| Python client | web3.py | Matches existing FastAPI stack |
| Contract pattern | UUPS Upgradeable Proxy | Fix bugs without redeploying; history preserved |
| Testnet | Polygon Mumbai | Free, Polygon-compatible, mirrors mainnet behavior |
| Mainnet | Polygon PoS (chain ID 137) | Production deployment target |

---

## Smart Contract Architecture

Layer 3 deploys two contracts — one per major concern. Both use the UUPS upgradeable proxy pattern via OpenZeppelin.

### Contract 1: `AttestationLedger.sol`

Stores the three event types. Append-only — no deletions, ever.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";

contract AttestationLedger is AccessControlUpgradeable, UUPSUpgradeable {

    bytes32 public constant AUDITOR_ROLE = keccak256("AUDITOR_ROLE");
    bytes32 public constant ADMIN_ROLE   = keccak256("ADMIN_ROLE");

    // Minimal on-chain state — events are the real storage
    mapping(bytes32 => bytes32) public latestManifestHash;   // serviceId => manifestHash
    mapping(bytes32 => bool)    public isGloballyRevoked;    // serviceId => bool

    // ── Event Types ──────────────────────────────────────────────────────────

    event AttestationRecorded(
        bytes32 indexed serviceId,      // keccak256(domain)
        bytes32 indexed auditorDid,     // keccak256(auditor DID)
        string  ontologyScope,          // "travel.*" or specific tag
        string  certificationRef,       // external cert ID (SOC2, ISO, etc.)
        uint256 expiresAt,
        bytes32 evidenceHash            // hash of off-chain evidence package
    );

    event RevocationRecorded(
        bytes32 indexed serviceId,
        bytes32 indexed auditorDid,
        string  reasonCode,             // "capability_failure|security_incident|fraud"
        bytes32 evidenceHash
    );

    event VersionRecorded(
        bytes32 indexed serviceId,
        bytes32 manifestHash,           // keccak256 of manifest raw_json
        uint256 recordedAt
    );

    // ── Write Functions ───────────────────────────────────────────────────────

    function recordAttestation(
        bytes32 serviceId,
        string calldata ontologyScope,
        string calldata certificationRef,
        uint256 expiresAt,
        bytes32 evidenceHash
    ) external onlyRole(AUDITOR_ROLE) { ... }

    function recordRevocation(
        bytes32 serviceId,
        string calldata reasonCode,
        bytes32 evidenceHash
    ) external onlyRole(AUDITOR_ROLE) {
        isGloballyRevoked[serviceId] = true;
        emit RevocationRecorded(serviceId, keccak256(abi.encodePacked(msg.sender)), reasonCode, evidenceHash);
    }

    function recordVersion(
        bytes32 serviceId,
        bytes32 manifestHash
    ) external onlyRole(ADMIN_ROLE) {
        latestManifestHash[serviceId] = manifestHash;
        emit VersionRecorded(serviceId, manifestHash, block.timestamp);
    }
}
```

**Key design principle:** Events are the storage. The contract emits events; off-chain indexers (custom Celery listener or The Graph protocol) build the queryable state. On-chain state is minimal — only the two hot-path mappings (`latestManifestHash` and `isGloballyRevoked`) are stored in contract storage. This keeps gas costs minimal.

---

### Contract 2: `AuditChain.sol`

Anchors transaction hashes. Never stores PII or raw records — only cryptographic commitments.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract AuditChain is AccessControlUpgradeable, UUPSUpgradeable {

    bytes32 public constant ANCHOR_ROLE = keccak256("ANCHOR_ROLE");

    // ── Anchoring Strategy: Batch Merkle Tree ────────────────────────────────
    // Individual records accumulate off-chain.
    // Every 60 seconds, a Celery job computes a Merkle root over all
    // unanchored records and writes a single BatchAnchorCommitted event.
    // Individual proofs can be verified against the batch root.

    event BatchAnchorCommitted(
        bytes32 indexed batchId,        // uuid of the batch
        bytes32 merkleRoot,             // Merkle root of all record hashes in batch
        uint256 recordCount,
        uint256 anchoredAt
    );

    event AuditRecordAnchored(
        bytes32 indexed agentDid,       // keccak256(agent DID)
        bytes32 indexed serviceId,      // keccak256(domain)
        string  ontologyTag,
        bytes32 recordHash,             // keccak256(full off-chain audit JSON)
        bytes32 sessionAssertionId,     // links to Layer 2 session
        uint256 anchoredAt
    );

    function commitBatch(
        bytes32 batchId,
        bytes32 merkleRoot,
        uint256 recordCount
    ) external onlyRole(ANCHOR_ROLE) {
        emit BatchAnchorCommitted(batchId, merkleRoot, recordCount, block.timestamp);
    }
}
```

**Anchoring strategy decision — Batch Merkle Tree (not individual records):**

Individual anchoring costs ~$0.001 per record. At scale (1M records/day), that's $1,000/day in gas. Batch anchoring with a Merkle tree commits 100+ records in a single transaction for ~$0.001 total — a 100x cost reduction. Individual record integrity is still provable via Merkle inclusion proof. This is how every production audit chain operates (Certificate Transparency logs use exactly this pattern).

---

## Auditor Network Design

Layer 3 introduces a new actor type: **Auditors** — third parties (security firms, compliance bodies, domain experts) who sign attestations that get written to the ledger.

### Auditor Registration Flow

```
1. Auditor applies via POST /auditors/register
   → presents: organization DID (did:web), scope of authority
     (which ontology domains they are qualified to audit),
     accreditation proof (SOC2 cert URL, ISO cert, HIPAA attestation, etc.)

2. AgentLedger admin reviews and issues AuditorCredential (JWT VC)
   → scoped to specific ontology domains (a travel auditor cannot attest health)
   → expires annually, requires re-certification

3. Auditor's Ethereum address is granted AUDITOR_ROLE in AttestationLedger.sol
   (admin-only write to the contract)

4. Auditor can now call POST /attestations to submit signed attestation events
   → their Ethereum address is validated on-chain before the event is accepted
   → unregistered address: transaction reverts, no gas wasted on AgentLedger side
```

**Why auditors cannot self-issue:** The smart contract validates that `msg.sender` maps to an address with `AUDITOR_ROLE` before accepting any attestation event. An unregistered auditor's transaction reverts at the EVM level. No API key compromise, no database manipulation — the enforcement is cryptographic and on-chain.

### Auditor Credential Scoping

Auditors are scoped to ontology domains that match their accreditation:

| Auditor Type | Allowed Ontology Scope | Example Credential |
|---|---|---|
| Security firm (general) | `*` (all domains) | SOC2 Type II |
| Healthcare compliance body | `health.*` | HIPAA Business Associate |
| Financial compliance body | `finance.*` | PCI-DSS QSA |
| Travel industry body | `travel.*` | IATA certification |
| Commerce/retail body | `commerce.*` | PCI-DSS |

### Trust Tier 4 — Quorum Requirement

**Trust Tier 4 (Ledger Attested) requires ≥2 independent attestations from ≥2 different auditor organizations.**

Rationale: A single auditor being compromised, coerced, or colluding with a fraudulent service should not be sufficient to grant top-tier trust. Two independent organizations from different corporate entities must independently attest the same service. This is the cryptographic equivalent of a two-person integrity rule for high-value operations.

---

## Cross-Registry Blocklist Federation

This is the most strategically important feature in Layer 3 and the one that establishes AgentLedger as neutral infrastructure rather than just another registry.

### The Federation Protocol

```
AgentLedger publishes the federated blocklist via three paths:
  GET  /federation/blocklist                    → paginated JSON (pull)
  GET  /federation/blocklist/stream             → Server-Sent Events (live push)
  GET  /.well-known/agentledger-blocklist.json  → lightweight discovery endpoint

Other registries subscribe by:
  POST /federation/registries/subscribe
    → provides: registry name, endpoint URL, webhook URL, 
      Ed25519 public key (for verifying their submitted revocations)

When a service is globally revoked (RevocationRecorded on-chain):
  1. RevocationEvent emitted on Polygon PoS
  2. Chain event listener picks it up within ~2 seconds (Polygon finality)
  3. 20-block confirmation window enforced before treating as final (~40 seconds)
  4. AgentLedger signs the revocation notice with its own Ed25519 key
  5. Signed notice pushed to all active subscriber webhooks
  6. Subscribing registries update their local state
  7. Cross-registry ban propagated end-to-end in < 60 seconds
     (NorthStar target was < 24 hours — we beat it by 1440x)

Federated registries can also SUBMIT revocations to AgentLedger:
  POST /federation/revocations/submit
    → signed by the registry's registered Ed25519 public key
    → AgentLedger reviews (automated + manual for severity) 
    → If validated: anchored on-chain and federated to all other subscribers
    → If rejected: reason returned, no propagation
```

**The strategic value:** Every registry that integrates the blocklist feed becomes dependent on AgentLedger for safety data. The more registries subscribe, the more valuable the blocklist becomes, the more registries subscribe. This is the network effect moat — and it compounds with adoption.

---

## Database Schema — New Tables

Layer 3 adds five tables. All existing Layer 1 and Layer 2 tables are untouched.

```sql
-- ── Auditor Registry ─────────────────────────────────────────────────────────

CREATE TABLE auditors (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    did TEXT UNIQUE NOT NULL,               -- did:web:auditfirm.com
    name TEXT NOT NULL,
    ontology_scope TEXT[] NOT NULL,         -- domains they can attest: ['health.*', 'finance.*']
    accreditation_refs JSONB DEFAULT '[]',  -- [{"type": "SOC2", "url": "...", "expires": "..."}]
    chain_address TEXT,                     -- Ethereum address holding AUDITOR_ROLE
    credential_hash TEXT,                   -- hash of issued AuditorCredential VC
    is_active BOOLEAN NOT NULL DEFAULT true,
    approved_at TIMESTAMPTZ,
    credential_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Off-Chain Attestation Records (mirrors on-chain events) ──────────────────

CREATE TABLE attestation_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    service_id UUID NOT NULL REFERENCES services(id),
    auditor_id UUID NOT NULL REFERENCES auditors(id),
    ontology_scope TEXT NOT NULL,           -- scope of this attestation
    certification_ref TEXT,                 -- external cert reference number
    evidence_hash TEXT NOT NULL,            -- must match on-chain evidenceHash
    tx_hash TEXT NOT NULL UNIQUE,           -- Polygon transaction hash
    block_number BIGINT NOT NULL,
    chain_id INTEGER NOT NULL DEFAULT 137,  -- 137 = Polygon PoS mainnet
    is_confirmed BOOLEAN NOT NULL DEFAULT false,  -- true after 20-block confirmation
    confirmed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT true,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX attestation_records_service ON attestation_records(service_id, is_active);
CREATE INDEX attestation_records_auditor ON attestation_records(auditor_id);
CREATE INDEX attestation_records_unconfirmed ON attestation_records(is_confirmed, block_number)
    WHERE is_confirmed = false;

-- ── Audit Chain Records ───────────────────────────────────────────────────────
-- Off-chain full records. On-chain: only the Merkle root of each batch.

CREATE TABLE audit_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_did TEXT NOT NULL,
    service_id UUID NOT NULL REFERENCES services(id),
    ontology_tag TEXT NOT NULL REFERENCES ontology_tags(tag),
    session_assertion_id UUID REFERENCES session_assertions(id),
    action_context JSONB NOT NULL,          -- PII-redacted: capability invoked, input types, not values
    outcome TEXT NOT NULL,                  -- 'success'|'failure'|'timeout'|'rejected'
    outcome_details JSONB DEFAULT '{}',
    record_hash TEXT NOT NULL,              -- keccak256(canonical JSON of this record)
    batch_id UUID,                          -- which anchor batch this record belongs to
    merkle_proof JSONB,                     -- Merkle inclusion proof for this record in its batch
    tx_hash TEXT,                           -- Polygon tx anchoring the batch
    block_number BIGINT,
    is_anchored BOOLEAN NOT NULL DEFAULT false,
    anchored_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX audit_records_agent ON audit_records(agent_did, created_at DESC);
CREATE INDEX audit_records_service ON audit_records(service_id, created_at DESC);
CREATE INDEX audit_records_unanchored ON audit_records(is_anchored, created_at)
    WHERE is_anchored = false;
CREATE INDEX audit_records_batch ON audit_records(batch_id);

-- Anchor batches
CREATE TABLE audit_batches (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    merkle_root TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    tx_hash TEXT UNIQUE,
    block_number BIGINT,
    chain_id INTEGER NOT NULL DEFAULT 137,
    status TEXT NOT NULL DEFAULT 'pending',   -- 'pending'|'submitted'|'confirmed'|'failed'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ
);

-- ── Federated Registry Subscriptions ─────────────────────────────────────────

CREATE TABLE federated_registries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL UNIQUE,
    webhook_url TEXT,
    public_key_pem TEXT NOT NULL,           -- Ed25519 public key for verifying their submissions
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_push_at TIMESTAMPTZ,
    last_push_status TEXT,                  -- 'success'|'failed'|'timeout'
    push_failure_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── On-Chain Event Index ──────────────────────────────────────────────────────
-- Fast off-chain queries without re-scanning the chain.

CREATE TABLE chain_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type TEXT NOT NULL,               -- 'attestation'|'revocation'|'version'|'audit_batch'
    service_id UUID REFERENCES services(id),
    tx_hash TEXT NOT NULL UNIQUE,
    block_number BIGINT NOT NULL,
    chain_id INTEGER NOT NULL DEFAULT 137,
    is_confirmed BOOLEAN NOT NULL DEFAULT false,
    event_data JSONB NOT NULL,              -- decoded event parameters
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ
);

CREATE INDEX chain_events_service ON chain_events(service_id, event_type);
CREATE INDEX chain_events_block ON chain_events(block_number DESC);
CREATE INDEX chain_events_unconfirmed ON chain_events(is_confirmed, block_number)
    WHERE is_confirmed = false;
```

---

## API Specification

Base URL: `https://api.agentledger.io/v1` (unchanged from Layers 1 and 2)

All endpoints require `X-API-Key` or Bearer VC token except `/chain/status` and `/federation/blocklist`.

### Attestation Endpoints

```
POST   /attestations
       → Submit attestation for a service (requires AuditorCredential VC)
       Body: { service_domain, ontology_scope, certification_ref, expires_at, evidence_package }
       Response 201: { attestation_id, tx_hash, block_number }

GET    /attestations/{service_id}
       → All active attestations for a service, with auditor metadata
       Response 200: [{ attestation_id, auditor, scope, expires_at, tx_hash, is_confirmed }]

GET    /attestations/{service_id}/verify
       → Independent on-chain verification: re-reads from Polygon, 
         compares to local DB, flags any discrepancy
       Response 200: { on_chain_matches_db: bool, attestation_count, trust_tier_eligible }
```

### Auditor Endpoints

```
POST   /auditors/register
       → Apply for auditor status
       Body: { did, name, ontology_scope[], accreditation_refs[], chain_address }
       Response 202: { application_id, status: "pending_review" }

GET    /auditors
       → List all active auditors with their scopes and credential expiry
       Response 200: [{ did, name, ontology_scope, credential_expires_at }]

GET    /auditors/{did}
       → Resolve auditor DID + current credential status
       Response 200: { did, name, scope, chain_address, is_active, credential_expires_at }
```

### Audit Chain Endpoints

```
POST   /audit/records
       → Create an audit record after a completed agent transaction
         Called by the agent or the agent's platform after a session assertion is redeemed
       Body: { session_assertion_id, ontology_tag, action_context (PII-redacted), outcome }
       Response 201: { record_id, record_hash, status: "pending_anchor" }

GET    /audit/records/{id}
       → Retrieve full audit record (off-chain)
       Response 200: { record, record_hash, batch_id, tx_hash, is_anchored }

GET    /audit/records/{id}/verify
       → Verify on-chain hash matches off-chain record
         Recomputes keccak256 of current DB record, verifies Merkle inclusion proof
         against the batch root stored on-chain
       Response 200: { integrity_valid: bool, merkle_proof_valid: bool, tx_hash, block_number }
       Response 409: { integrity_valid: false, detail: "record hash mismatch — possible tampering" }

GET    /audit/records
       → Query audit history
       Params: agent_did, service_id, ontology_tag, from_date, to_date, outcome, limit, offset
       Response 200: { records: [...], total, page }
```

### Federation Endpoints

```
GET    /federation/blocklist
       → Current global revocation list
       Params: page, limit, since (ISO timestamp for incremental sync)
       Response 200: { revocations: [{ domain, reason, revoked_at, tx_hash }], total, next_page }

GET    /federation/blocklist/stream
       → Server-Sent Events live feed of new revocations
       Headers: Accept: text/event-stream

POST   /federation/registries/subscribe
       → Registry subscribes to blocklist push feed
       Body: { name, endpoint, webhook_url, public_key_pem }
       Response 201: { subscriber_id, status: "active" }

POST   /federation/revocations/submit
       → Federated registry submits a revocation for AgentLedger review
       Body: { domain, reason_code, evidence_url }
         (must be signed with the registry's registered Ed25519 private key)
       Response 202: { submission_id, status: "pending_review" }
```

### Chain State Endpoints

```
GET    /chain/status
       → Current chain connection status, latest block, contract addresses
       Response 200: { chain_id, network, latest_block, contracts: { attestation_ledger, audit_chain } }

GET    /chain/events
       → Query indexed on-chain events
       Params: service_id, event_type, from_block, to_block, limit
       Response 200: { events: [...], total }
```

---

## Ranking Algorithm — Layer 2 → Layer 3 Upgrades

All five Layer 2 integration points are activated or upgraded in Layer 3.

### attestation_score (30% of trust formula)

**Layer 2:** Derived from local session outcome data (a proxy).  
**Layer 3:** Weighted composite of on-chain auditor attestations.

```python
def compute_attestation_score(service_id: UUID) -> float:
    """
    Weighted score from confirmed on-chain attestations.
    More auditors = higher score. Wider scope = higher weight.
    Recency-weighted: attestations decay toward 0.5 over 12 months.
    """
    attestations = get_confirmed_attestations(service_id)  # from chain_events + attestation_records

    if not attestations:
        return 0.0

    score = 0.0
    unique_orgs = set()

    for att in attestations:
        # Scope weight: full domain ("health.*") = 1.0, specific tag = 0.6
        scope_weight = 1.0 if att.ontology_scope.endswith(".*") else 0.6
        # Recency weight: decays linearly from 1.0 to 0.5 over 365 days
        days_old = (now() - att.recorded_at).days
        recency_weight = max(0.5, 1.0 - (days_old / 365) * 0.5)
        score += scope_weight * recency_weight
        unique_orgs.add(att.auditor_org_id)

    # Quorum bonus: ≥2 independent orgs → score multiplied by 1.2, capped at 1.0
    if len(unique_orgs) >= 2:
        score *= 1.2

    return min(1.0, score / len(attestations))
```

### reputation_score (15% of trust formula)

**Layer 2:** Derived from local session redemption outcomes only.  
**Layer 3:** Local session outcomes + federated signals from subscribed registries.

```python
def compute_reputation_score(service_id: UUID) -> float:
    local_score  = get_local_session_outcome_rate(service_id)   # success/total (30d)
    fed_score    = get_federated_reputation_signals(service_id) # avg from federated registries
    is_blocklisted = check_federated_blocklist(service_id)

    if is_blocklisted:
        return 0.0

    # Weighted blend: local data is more trusted than federated (we can verify it)
    return (local_score * 0.70) + (fed_score * 0.30)
```

### Trust Tier 4 — Activation

**Layer 2:** Reserved, unused.  
**Layer 3:** Activated with quorum requirement.

```python
def evaluate_trust_tier_4(service_id: UUID) -> bool:
    """
    Trust Tier 4 (Ledger Attested) requires:
    - ≥2 confirmed on-chain attestations
    - From ≥2 different auditor organizations
    - At least one non-expired
    - Service not globally revoked on-chain
    """
    attestations = get_confirmed_attestations(service_id)
    if len(attestations) < 2:
        return False
    unique_orgs = {att.auditor_org_id for att in attestations if not att.is_expired}
    if len(unique_orgs) < 2:
        return False
    if is_globally_revoked_on_chain(service_id):
        return False
    return True
```

---

## Threat Model — Layer 3 Additions (10 → 14)

Layer 3 expands the threat model from 10 threats (Layers 1–2) to 14.

| # | Threat | Attack | Severity | Mitigation |
|---|---|---|---|---|
| 11 | Auditor Collusion | Two registered auditors from related organizations co-attest a fraudulent service | 🔴 Critical | Quorum requires ≥2 auditors from ≥2 *independent* organizations (checked at org level, not DID level); single-org quorum rejected |
| 12 | Chain Reorg Attack | Attacker causes a short reorg on Polygon to erase a RevocationRecorded event | 🟠 High | 20-block confirmation window enforced before treating any event as final; critical revocations additionally anchored to Ethereum L1 via proof submission |
| 13 | Blocklist Poisoning | Attacker submits fraudulent federated revocations to ban legitimate services | 🟠 High | Federated revocations require AgentLedger countersignature before propagation; automated abuse detection flags high-volume submitters |
| 14 | Audit Record Tampering | Off-chain audit record is modified after the on-chain batch anchor is committed | 🔴 Critical | `/audit/records/{id}/verify` recomputes record hash and validates Merkle inclusion proof against on-chain batch root; mismatch triggers immediate alert and incident response |

### Framework Alignment Additions

- **MITRE ATLAS:** Threat 11 → AML.T0017 (Compromise ML Model via insider threat); Threat 13 → AML.T0010 (Craft Adversarial Data via supply chain)
- **EU AI Act (enforcement August 2026):** The Audit Chain's Merkle-anchored records satisfy Article 12 (record-keeping for high-risk AI) and Article 17 (quality management system documentation requirements)
- **NIST AI RMF:** The federated blocklist maps to GOVERN 1.2 (organizational risk tolerance and information sharing)

---

## Build Phases

Same gate-based approach as Layers 1 and 2. Each phase stops and waits for confirmation before the next begins.

### Phase 1 — Smart Contract Foundation

Write and test `AttestationLedger.sol` and `AuditChain.sol` locally using Hardhat. Deploy to Polygon Mumbai testnet. Generate Python ABI bindings with web3.py.

**Deliverables:**
- `contracts/AttestationLedger.sol` with UUPS proxy, three event types, AUDITOR_ROLE access control
- `contracts/AuditChain.sol` with batch commit function
- `contracts/test/` — Hardhat test suite for all contract functions
- `contracts/scripts/deploy.py` — deployment script targeting Mumbai testnet
- `api/services/chain.py` — web3.py client wrapping contract calls
- Deployed contract addresses committed to `.env.example`

**Done when:** Hardhat tests pass for all event types; testnet deployment succeeds; Python `chain.py` can read a test event from Mumbai.

---

### Phase 2 — Chain Event Listener

Celery task polling Polygon for new events from both contracts. Indexes events into `chain_events` table. Handles reorgs via 20-block confirmation window.

**Deliverables:**
- `crawler/tasks/index_chain_events.py` — polls `eth_getLogs` every 5 seconds
- `crawler/tasks/confirm_chain_events.py` — promotes events to `is_confirmed=true` after 20 blocks
- Migration `004_layer3_chain.py` — all five new tables
- `db/seed_chain_config.py` — seeds contract addresses into config

**Done when:** An attestation event emitted on Mumbai appears in `chain_events` within 30 seconds with `is_confirmed=false`, then `is_confirmed=true` within 50 seconds.

---

### Phase 3 — Attestation API + Auditor Network

Auditor registration and credentialing. `POST /attestations` with on-chain write. Upgrade `attestation_score` and `reputation_score` in ranker. Trust Tier 4 activation gate.

**Deliverables:**
- `api/services/attestation.py` — submit, verify, compute attestation score
- `api/services/auditor.py` — registration, credential issuance, scope validation
- `api/routers/attestation.py` — all attestation and auditor endpoints
- Updated `api/services/ranker.py` — Layer 3 attestation and reputation score logic
- Updated `api/services/registry.py` — Tier 4 evaluation on trust score recompute

**Done when:** A registered auditor submits an attestation, the service's `trust_score` updates within 60 seconds, and when a second independent auditor attests the same service, `trust_tier` updates to 4.

---

### Phase 4 — Audit Chain

`POST /audit/records`, off-chain record storage, Celery batch anchor job (Merkle tree every 60 seconds), Merkle proof generation, hash verification endpoint.

**Deliverables:**
- `api/services/audit.py` — record creation, hash computation, Merkle proof verification
- `api/routers/audit.py` — all audit chain endpoints
- `crawler/tasks/anchor_audit_batch.py` — collects unanchored records, builds Merkle tree, writes batch to `AuditChain.sol`
- `api/services/merkle.py` — Merkle tree builder and proof generator

**Done when:** An agent posts an audit record; within 90 seconds `tx_hash` is populated, `is_anchored=true`, and `GET /audit/records/{id}/verify` returns `{ integrity_valid: true, merkle_proof_valid: true }`.

---

### Phase 5 — Cross-Registry Federation

Blocklist endpoint and SSE stream. Subscriber registration. Federated revocation push. Incoming federated revocation review queue. Automated push on new on-chain revocation events.

**Deliverables:**
- `api/services/federation.py` — blocklist generation, subscriber management, push dispatch
- `api/routers/federation.py` — all federation endpoints
- `crawler/tasks/push_revocations.py` — triggered by chain event listener on RevocationRecorded, pushes to all active subscribers
- `api/services/sse.py` — Server-Sent Events stream for live blocklist feed

**Done when:** A `RevocationRecorded` event on testnet triggers a signed push to a test subscriber webhook URL within 60 seconds of the 20-block confirmation.

---

### Phase 6 — Hardening

Mainnet deployment (Polygon PoS chain ID 137). 20-block confirmation enforcement in production. Multi-auditor quorum validation. Audit tamper detection alerts. Full test suite at 80%+ coverage. Load test all new endpoints at 100 concurrent users.

**Done when:** All 10 acceptance criteria pass against Polygon mainnet; all endpoints meet p95 latency targets; test coverage report shows ≥80%.

---

## Acceptance Criteria (10 Gates)

Layer 3 is not complete until all 10 pass against Polygon mainnet (not testnet).

```
[ ] Auditor registers, receives AuditorCredential VC, appears in GET /auditors
[ ] Auditor submits attestation → AttestationRecorded event confirmed on Polygon mainnet
[ ] Attested service moves to trust_tier=4 only after ≥2 attestations from ≥2 independent orgs
[ ] trust_score attestation_score component updates within 60s of on-chain confirmation
[ ] Revocation event anchored on-chain AND pushed to federated subscriber within 60s of confirmation
[ ] Audit record created → hash anchored in Merkle batch on-chain within 90s
[ ] GET /audit/records/{id}/verify confirms Merkle proof integrity: integrity_valid=true
[ ] Federated registry subscribe flow completes and receives live blocklist SSE push
[ ] Chain reorg simulation: events re-indexed correctly after reorg (20-block window holds)
[ ] All Layer 3 endpoints < 300ms p95 @ 100 concurrent users
    (chain read endpoints: 300ms target; off-chain endpoints: 200ms target)
```

---

## What Layer 3 Does NOT Include

Keep the gate clean. The following belong to later layers and must not be built in Layer 3:

- Zero-knowledge proofs for context disclosure (Layer 4)
- Payment-gated capability scopes (Layer 4)
- Privacy-preserving context routing (Layer 4)
- Workflow registry or human-validated orchestration patterns (Layer 5)
- Insurance products, dispute resolution, or liability attribution (Layer 6)
- Full decentralized validator network — requires adoption volume to justify infrastructure
- Cross-chain bridging — Polygon PoS is the only chain in v0.1
- OAuth2 or OpenID Connect for auditor auth — API key + VC token is sufficient for v0.1

---

## Layer 2 → Layer 3 Integration Points (All Five Activated)

| # | Integration Point | Layer 2 State | Layer 3 Change |
|---|---|---|---|
| 1 | `attestation_score` (30% of trust formula) | Derived from local session data (proxy) | Upgraded: weighted composite of confirmed on-chain auditor attestations |
| 2 | `reputation_score` (15% of trust formula) | Local session outcomes only | Upgraded: local outcomes (70%) + federated registry signals (30%) |
| 3 | `trust_tier=4` (Ledger Attested) | Reserved, unused | Activated: requires ≥2 on-chain attestations from ≥2 independent auditor organizations |
| 4 | `revocation_events` table | Local only | Layer 3 anchors revocations on-chain AND pushes signed notices to all federated subscribers |
| 5 | Layer 2 VC/DID contracts | Stable | Unchanged — Layer 3 uses `agent_did` and service domain as chain identifiers; no modifications to Layer 2 models |

---

## What the Architect Should Carry Into Layer 4

Layer 4 (Context Matching) will build on the following Layer 3 contracts — these must remain stable:

- Audit records in the `audit_records` table are the input data for context mismatch detection — if a service requests context fields beyond what its manifest declares, the audit record captures the discrepancy
- The `attestation_records` table is the input for context-gating: only Tier 3+ services should be eligible to receive sensitive context fields (medical, financial)
- The federated blocklist is a prerequisite for context routing — a service that is blocklisted must not receive any context, regardless of what the agent requests
- `AuditChain.sol` must support a new event type in Layer 4: `ContextDisclosureAnchored` — the contract should be extended (not redeployed) via the UUPS upgrade path

---

## Decisions Log — Layer 3

| Date | Decision | Rationale |
|---|---|---|
| Apr 2026 | Polygon PoS for v0.1 | Battle-tested, EVM-compatible, 2s finality, ~$0.001/tx — optimal for production without custom infrastructure |
| Apr 2026 | OP Stack rollup as NorthStar chain | Purpose-built chain for attestation throughput + Ethereum settlement; upgrade path is non-breaking |
| Apr 2026 | UUPS upgradeable proxy pattern | Bug fixes and feature additions without redeployment; history preserved on same address |
| Apr 2026 | Events as storage (not contract state) | Minimizes gas costs; off-chain indexers build queryable state; auditable via eth_getLogs independently |
| Apr 2026 | Batch Merkle anchoring for Audit Chain | 100x cost reduction vs. individual anchoring; Merkle proofs preserve individual record verifiability |
| Apr 2026 | 20-block confirmation window | Balances finality speed (40s on Polygon) with reorg protection; critical revocations additionally anchored to L1 |
| Apr 2026 | Quorum = ≥2 independent orgs for Tier 4 | Single auditor compromise should not grant top-tier trust; two-person integrity rule for high-value attestations |
| Apr 2026 | Pull + SSE for federation (not webhook-only) | Matches Layer 2 HITL pattern; no webhook infrastructure required for early adopters; SSE provides live feed for advanced integrators |

---

## Open Questions (Carry Into Layer 4 Design)

- [ ] Layer 4: Context Matching — privacy-preserving context disclosure mechanism design
- [ ] Layer 4: Zero-knowledge proof library selection (circom vs. snarkjs vs. noir)
- [ ] Layer 4: User-controlled context profile data model
- [ ] Layer 3 NorthStar: OP Stack rollup — when does transaction volume justify the infrastructure investment?
- [ ] Layer 3 NorthStar: Third-party auditor recruitment — which security firms to onboard first?
- [ ] Governance: At what adoption milestone does the ontology spec transition to community governance?

---

*This document is the Layer 3 architecture design. It becomes the implementation spec upon architect sign-off.*  
*Do not begin implementation until the spec is confirmed and committed to `spec/LAYER3_SPEC.md`.*  
*Branch: `git checkout -b layer3/trust-verification`*

---

**AgentLedger** | Trust & Discovery Infrastructure for the Autonomous Agent Web  
*Michael Williams | linkedin.com/in/mwill-AImission | github.com/mwill20*
