# 🎓 Lesson 21: The Notary's Seal — Layer 3 Overview & Why Blockchain

## 🛡️ Welcome Back, Agent Architect!

You've built a world-class manifest registry. Services are registered, crawled, ranked, and served through a battle-hardened FastAPI stack. But here's the million-dollar question:

> **"How do I know this service has actually been audited — and that AgentLedger isn't just making that up?"**

Today we explore **Layer 3: Trust & Verification** — the blockchain-anchored layer that makes trust claims independently verifiable. No more "trust us." Now anyone can query Polygon Amoy directly and prove the attestation exists.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain why a signed database record is not sufficient for trust claims
- ✅ Describe the two-contract architecture (AttestationLedger vs. AuditChain) and what each does
- ✅ Read the Solidity contracts and identify the roles, events, and state variables
- ✅ Explain the "events-as-storage" pattern and why it cuts costs vs. individual on-chain storage
- ✅ Explain the UUPS proxy pattern and why it matters for a live contract
- ✅ Run the Hardhat contract test suite locally
- ✅ Map the 10 Layer 3 acceptance criteria to specific code paths

**Estimated time:** 90 minutes  
**Prerequisites:** Lessons 01–10 (Layer 1 suite), basic familiarity with the concept of a blockchain transaction

---

## 🔍 What This Layer Does

Layer 1 answers "does this service exist and what can it do?"
Layer 2 answers "is this agent's identity verified?"
Layer 3 answers: **"Has this service actually earned trust — and can you prove it without asking us?"**

```
📁 Service Manifest
       |
       v
🔍 Layer 1: Registry (exists? capabilities?)
       |
       v
🔐 Layer 2: Identity (is the agent's DID verified?)
       |
       v
🏛️ Layer 3: Trust (is the service auditor-attested? on-chain proven?)
       |
       v
🔎 Layer 4: Context (what data can this service see about this agent?)
```

### The core problem: mutability

A trust rating stored in PostgreSQL can be changed silently. If AgentLedger claims a service has attestation_score=1.0, you have no way to verify that claim without trusting AgentLedger's database. This is fine for a starting point but breaks down in adversarial or regulated environments.

**The blockchain solution:** When an auditor attests a service, that event is written to a smart contract on Polygon Amoy. Anyone with a public RPC endpoint can independently query `eth_getLogs` to confirm the event exists — no AgentLedger involvement required. The database becomes a fast read cache; the chain is the ground truth.

---

## 🏗️ The Two-System Architecture

Layer 3 deploys **two smart contracts** on Polygon Amoy (`chain_id=80002`):

```
┌─────────────────────────────────────────────────────────────────┐
│                    AttestationLedger.sol                         │
│  Who audited what service, when, and under which scope           │
│  Events: AttestationRecorded | RevocationRecorded | VersionRecorded │
│  Proxy: 0x7961BC0F69Dac95309F197E176ea8CD1D3EbF23D              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           │  (separate concerns)
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                       AuditChain.sol                             │
│  Cryptographic anchor for agent transaction audit records        │
│  Events: BatchAnchorCommitted | AuditRecordAnchored              │
│  Proxy: 0x55366DA11A48e2dCFE3F67f9802aF3e032dC2244              │
└─────────────────────────────────────────────────────────────────┘
```

**Why two contracts?** Attestations (who audited which service) and audit records (what agents did with which service) have different authors, different update rates, and different consumers. Separating them keeps each contract focused and independently upgradeable.

---

## 📚 Key Concepts

### events-as-storage

Smart contract storage (state variables) costs gas per byte for every write — and for data that grows unboundedly (like attestation history), this gets expensive fast. The pattern used here:

- **Contract state** = minimal hot-path index only. `AttestationLedger.sol` stores just two mappings:
  - `latestManifestHash[serviceId]` — current manifest hash for fast lookup
  - `isGloballyRevoked[serviceId]` — bool flag for the revocation hot path
- **Events** = the real history. Every attestation, revocation, and version change is an event. Events are stored in bloom filters on each block, queryable via `eth_getLogs` — much cheaper than state writes.

The result: the full audit history of a service costs ~$0.001 in gas regardless of how many attestations it has, because events don't touch storage.

### UUPS Proxy Pattern

Contracts on a blockchain are immutable once deployed. If a bug is found, you'd normally have to redeploy and migrate. The **UUPS (Universal Upgradeable Proxy Standard)** pattern solves this:

```
┌──────────────────────────────────────────────┐
│  Proxy Contract (permanent address)          │
│  delegatecall → Implementation Contract      │
│  _authorizeUpgrade → only ADMIN_ROLE         │
└──────────────────────────────────────────────┘
```

- Users and integrators interact with the **proxy address** (which never changes)
- The **implementation** (the actual logic) can be swapped by `ADMIN_ROLE`
- This is what allows bug fixes without breaking the contract address referenced in attestations

OpenZeppelin's `UUPSUpgradeable` handles the proxy mechanics. The `_authorizeUpgrade` function in both contracts enforces that only `ADMIN_ROLE` can change the implementation.

### Role-Based Access Control

Both contracts inherit from `AccessControlUpgradeable`. Roles:

| Role | Who holds it | What it grants |
|------|-------------|----------------|
| `ADMIN_ROLE` | App signer (deployer) | Upgrade implementation, record versions |
| `AUDITOR_ROLE` | Registered auditors | Record attestations and revocations |
| `ANCHOR_ROLE` | App signer | Commit Merkle batch anchors to AuditChain |

The 20-byte Solidity role IDs are computed as `keccak256("AUDITOR_ROLE")` etc.

### Polygon Amoy vs. Ethereum

Why Polygon and not Ethereum mainnet?

| Criterion | Ethereum mainnet | Polygon PoS (Amoy) |
|-----------|-----------------|-------------------|
| Cost per tx | ~$2–50 | ~$0.001 |
| Finality | ~13s per block | ~2s per block |
| EVM compat | Native | Full |
| NorthStar | No | Migrate to OP Stack rollup |

The spec evaluation chose Polygon Amoy for v0.1. The NorthStar upgrade path is a custom OP Stack rollup for higher attestation throughput (Phase 2).

---

## 📝 Code Walkthrough: AttestationLedger.sol

**File:** [`contracts/AttestationLedger.sol`](../../contracts/AttestationLedger.sol)

```solidity
// Lines 1-7: Imports
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts-upgradeable/access/AccessControlUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
```
All three are from OpenZeppelin's upgradeable library — standard, audited, battle-tested.

```solidity
// Lines 8-11: Role constants
contract AttestationLedger is Initializable, AccessControlUpgradeable, UUPSUpgradeable {
    bytes32 public constant AUDITOR_ROLE = keccak256("AUDITOR_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
```
Roles are stored as `bytes32` keccak256 hashes. This is the standard OpenZeppelin pattern — the string label is only used to compute the hash at compile time.

```solidity
// Lines 13-14: Minimal state storage
    mapping(bytes32 => bytes32) public latestManifestHash;
    mapping(bytes32 => bool) public isGloballyRevoked;
```
These are the **only two state variables**. Everything else is in events. The `bytes32` key is `keccak256(serviceId)` — hashing the service domain string into a fixed-width EVM-compatible key.

```solidity
// Lines 16-35: Three event types
    event AttestationRecorded(
        bytes32 indexed serviceId,    // which service
        bytes32 indexed auditorRef,   // keccak256(msg.sender) - anonymized
        string ontologyScope,         // e.g. "health.*"
        string certificationRef,      // external reference (ISO, SOC2 etc)
        uint256 expiresAt,            // unix timestamp, 0 = no expiry
        bytes32 evidenceHash          // keccak256(evidence_package JSON)
    );

    event RevocationRecorded(
        bytes32 indexed serviceId,
        bytes32 indexed auditorRef,
        string reasonCode,            // e.g. "data_breach", "license_expired"
        bytes32 evidenceHash
    );

    event VersionRecorded(
        bytes32 indexed serviceId,
        bytes32 manifestHash,         // hash of the new manifest
        uint256 recordedAt            // block.timestamp
    );
```
All three events have `indexed` fields for efficient `eth_getLogs` filtering. The `indexed` keyword creates a bloom filter entry that makes queries O(1) rather than O(all blocks).

```solidity
// Lines 37-40: Constructor blocks re-initialization
    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }
```
UUPS proxies use `initialize()` instead of `constructor()`. The `_disableInitializers()` call prevents someone from calling `initialize()` on the implementation contract directly (only the proxy should call it).

```solidity
// Lines 42-47: Initialization (one-time setup, replaces constructor)
    function initialize(address defaultAdmin) public initializer {
        __AccessControl_init();
        _grantRole(DEFAULT_ADMIN_ROLE, defaultAdmin);
        _grantRole(ADMIN_ROLE, defaultAdmin);
    }
```
`initializer` modifier ensures this can only be called once. The deployer gets `ADMIN_ROLE` but NOT `AUDITOR_ROLE` — auditors must be granted separately via `grant_roles.js`.

```solidity
// Lines 49-64: recordAttestation — the primary write path
    function recordAttestation(
        bytes32 serviceId,
        string calldata ontologyScope,
        string calldata certificationRef,
        uint256 expiresAt,
        bytes32 evidenceHash
    ) external onlyRole(AUDITOR_ROLE) {
        emit AttestationRecorded(
            serviceId,
            keccak256(abi.encodePacked(msg.sender)),  // anonymize auditor address
            ontologyScope,
            certificationRef,
            expiresAt,
            evidenceHash
        );
    }
```
Note: **no storage write**. This function only emits an event. The `onlyRole(AUDITOR_ROLE)` modifier will revert the transaction if the caller hasn't been granted `AUDITOR_ROLE`. The auditor's address is anonymized by hashing it — the event records that *someone with AUDITOR_ROLE* attested, not their wallet address.

```solidity
// Lines 66-78: recordRevocation — the one mutable state write
    function recordRevocation(
        bytes32 serviceId,
        string calldata reasonCode,
        bytes32 evidenceHash
    ) external onlyRole(AUDITOR_ROLE) {
        isGloballyRevoked[serviceId] = true;  // <- the only mutable state write
        emit RevocationRecorded(...);
    }
```
This is the **only function that writes to contract state**. Why? Because the revocation hot path (is this service currently blocked?) needs an O(1) lookup via `isGloballyRevoked[serviceId]`, not a full event scan.

```solidity
// Lines 88-95: Upgrade authorization
    function _authorizeUpgrade(address newImplementation)
        internal view override
        onlyRole(ADMIN_ROLE)
    {
        require(newImplementation != address(0), "invalid implementation");
    }
```
Only `ADMIN_ROLE` can authorize an upgrade. The `view` modifier means this doesn't touch state — it's a pure gate check.

---

## 📝 Code Walkthrough: AuditChain.sol

**File:** [`contracts/AuditChain.sol`](../../contracts/AuditChain.sol)

```solidity
// Lines 8-10: Role constants
contract AuditChain is Initializable, AccessControlUpgradeable, UUPSUpgradeable {
    bytes32 public constant ANCHOR_ROLE = keccak256("ANCHOR_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
```

```solidity
// Lines 12-27: Two event types
    event BatchAnchorCommitted(
        bytes32 indexed batchId,      // UUID of the off-chain batch
        bytes32 merkleRoot,           // root of the Merkle tree of record hashes
        uint256 recordCount,          // how many records in this batch
        uint256 anchoredAt            // block.timestamp
    );

    event AuditRecordAnchored(        // not currently emitted by commitBatch
        bytes32 indexed agentDid,
        bytes32 indexed serviceId,
        string ontologyTag,
        bytes32 recordHash,
        bytes32 sessionAssertionId,
        uint256 anchoredAt
    );
```

```solidity
// Lines 40-46: commitBatch — the main anchor function
    function commitBatch(
        bytes32 batchId,
        bytes32 merkleRoot,
        uint256 recordCount
    ) external onlyRole(ANCHOR_ROLE) {
        emit BatchAnchorCommitted(batchId, merkleRoot, recordCount, block.timestamp);
    }
```
The entire function is just one event emit. There is **no state**. The entire audit chain lives in events. A single call anchors up to 100+ records' worth of hashes via one Merkle root — this is the 100x cost reduction compared to anchoring each record individually.

**Why no per-record events?** The `AuditRecordAnchored` event type exists for future individual record anchoring (Layer 4+ use case). For now, only `BatchAnchorCommitted` is used — batch Merkle anchoring is sufficient for integrity proofs.

---

## 🏗️ How the Contracts Fit Into the Stack

```
contracts/
├── AttestationLedger.sol     <- auditor attestations and revocations
├── AuditChain.sol            <- batch Merkle anchoring
├── abi/                      <- compiled ABIs loaded by chain.py
│   ├── AttestationLedger.json
│   └── AuditChain.json
├── artifacts/                <- compiled bytecode (Hardhat output)
├── scripts/
│   ├── deploy.js             <- Hardhat deploy script (both contracts)
│   ├── deploy.py             <- Python wrapper for deploy.js
│   └── grant_roles.js        <- Grant AUDITOR_ROLE + ANCHOR_ROLE post-deploy
└── test/                     <- Hardhat test suite (4 tests)
```

The ABIs are consumed by `api/services/chain.py` at runtime:
```python
# api/services/chain.py line 29
_CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "abi"

# line 79-81
@lru_cache
def _load_contract_abi(name: str) -> list[dict[str, Any]]:
    path = _CONTRACTS_ROOT / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))
```

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Run the Hardhat Contract Test Suite

```bash
# Make sure Node dependencies are installed
npm install

# Run all contract tests
npx hardhat test
```

**Expected output:**
```
  AttestationLedger
    ✓ deploy and initialize
    ✓ emit AttestationRecorded event
    ✓ emit RevocationRecorded event

  AuditChain
    ✓ deploy, initialize, and commitBatch

  4 passing (1.2s)
```

### 🔬 Exercise 2: Deploy to Hardhat local node and query an event

```bash
# Terminal 1: Start a local Hardhat node
npx hardhat node

# Terminal 2: Deploy both contracts
npx hardhat run contracts/scripts/deploy.js --network localhost
```

**Expected output from deploy:**
```
Deploying contracts with account: 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
AttestationLedger proxy deployed to: 0x...
AuditChain proxy deployed to: 0x...
```

Now interact with the contract in a Hardhat console:
```bash
npx hardhat console --network localhost
```

```javascript
// In the Hardhat console:
const { ethers } = require("hardhat");
const attestation = await ethers.getContractAt(
  "AttestationLedger",
  "<PROXY_ADDRESS_FROM_DEPLOY>"
);

// Grant yourself AUDITOR_ROLE (on the local node you own the deployer)
const AUDITOR_ROLE = await attestation.AUDITOR_ROLE();
const [owner] = await ethers.getSigners();
await attestation.grantRole(AUDITOR_ROLE, owner.address);

// Emit one attestation event
const serviceId = ethers.keccak256(ethers.toUtf8Bytes("healthservice.example.com"));
const evidenceHash = ethers.keccak256(ethers.toUtf8Bytes("{}"));
await attestation.recordAttestation(
  serviceId,
  "health.*",
  "ISO-27001",
  Math.floor(Date.now() / 1000) + 365 * 86400,
  evidenceHash
);
console.log("AttestationRecorded event emitted!");
```

**Expected output:**
```
AttestationRecorded event emitted!
```

### 🔬 Exercise 3: Intentional Failure — Call Without AUDITOR_ROLE

```javascript
// In the Hardhat console:
const [_, nonAuditor] = await ethers.getSigners();
const serviceId = ethers.keccak256(ethers.toUtf8Bytes("healthservice.example.com"));
const evidenceHash = ethers.keccak256(ethers.toUtf8Bytes("{}"));

// This should revert
try {
  await attestation.connect(nonAuditor).recordAttestation(
    serviceId, "health.*", "ISO-27001", 0, evidenceHash
  );
  console.log("ERROR: should have reverted!");
} catch (e) {
  console.log("Correctly reverted:", e.message.includes("AccessControl"));
}
```

**Expected output:**
```
Correctly reverted: true
```

⚠️ **Why this matters:** Even if someone bypasses the Python API layer and calls the contract directly, the `onlyRole(AUDITOR_ROLE)` modifier ensures the transaction reverts. This is the second layer of the scope enforcement (the first being `_scope_allows` in `attestation.py`).

---

## 📊 Summary Reference Card

| Item | Value |
|------|-------|
| AttestationLedger proxy | `0x7961BC0F69Dac95309F197E176ea8CD1D3EbF23D` |
| AuditChain proxy | `0x55366DA11A48e2dCFE3F67f9802aF3e032dC2244` |
| Chain ID (Amoy testnet) | `80002` |
| Chain ID (Polygon PoS mainnet) | `137` |
| AttestationLedger events | `AttestationRecorded`, `RevocationRecorded`, `VersionRecorded` |
| AuditChain events | `BatchAnchorCommitted`, `AuditRecordAnchored` |
| Roles: AttestationLedger | `ADMIN_ROLE`, `AUDITOR_ROLE` |
| Roles: AuditChain | `ADMIN_ROLE`, `ANCHOR_ROLE` |
| Contract source | `contracts/*.sol` |
| ABIs (loaded by Python) | `contracts/abi/*.json` |
| Hardhat config | `hardhat.config.js` |
| Deployment scripts | `contracts/scripts/deploy.js`, `grant_roles.js` |
| Spec reference | `spec/LAYER3_SPEC.md` |
| Acceptance evidence | `spec/LAYER3_COMPLETION.md` |

---

## 📚 Interview Preparation

**Q: Why use blockchain events instead of storing data in contract state?**

**A:** Contract state writes cost gas proportional to the data stored, and the cost compounds as history grows. Events are stored in block receipt logs and queryable via `eth_getLogs`, not in the EVM's state trie — this is dramatically cheaper. For a trust registry that could accumulate thousands of attestations, the cost difference is 10–100x. The tradeoff is that events are write-only (you can't update them) and you need an off-chain indexer to query them efficiently — which is exactly what `api/services/chain.py` provides.

**Q: Why Polygon Amoy instead of Ethereum mainnet?**

**A:** Cost and finality. A single attestation transaction costs roughly $0.001 on Polygon vs. $2–50 on Ethereum mainnet depending on gas prices. Polygon also has ~2-second block times vs. ~13 seconds, which matters for the 20-block confirmation window — 40 seconds on Polygon vs. ~3 minutes on Ethereum. The NorthStar upgrade path is a custom OP Stack rollup for even higher throughput, but Polygon PoS is the right v0.1 choice.

**Q: What happens if there's a bug in the contract? How do you fix it without invalidating existing attestations?**

**A:** The UUPS proxy pattern. Integrators interact with the proxy address (`0x7961...`), which uses `delegatecall` to forward all calls to the implementation contract. When `_authorizeUpgrade` is called by `ADMIN_ROLE`, the proxy's implementation pointer is updated to a new contract address. All historical events remain on the old implementation — they're immutable. Future transactions use the new logic. The proxy address (and therefore all external references to it) never changes.

**Q: What are the 10 Layer 3 acceptance criteria?**

**A:** (From `spec/LAYER3_COMPLETION.md`):
1. Attestation events written and indexed on Amoy
2. Revocation events propagate to federation blocklist
3. Version events track manifest changes immutably
4. Trust score recomputes with `attestation_score` populated
5. Trust tier 4 gate activates after live attestation
6. Audit batch anchoring confirmed on-chain
7. Federation blocklist endpoint returns revoked services
8. Live chain deployment verified (real Amoy tx hashes)
9. 20-block confirmation behavior verified on real chain
10. Load target: p95 < 500ms @ 100 concurrent users (actual: 92ms)

---

## ✅ Key Takeaways

- Layer 3 adds **blockchain-anchored trust** to the registry via two Polygon Amoy smart contracts
- **AttestationLedger** records who audited what service; **AuditChain** anchors batches of agent transaction records
- Both contracts use **events-as-storage** — the chain events are the ground truth; the database is a fast read cache
- The **UUPS proxy pattern** allows contract upgrades without changing the address referenced in attestations
- **Role-based access control** enforces that only registered auditors can attest/revoke, and only the app signer can anchor batches or upgrade contracts
- The contracts are minimal by design: `AttestationLedger` has **two state variables** and three events; `AuditChain` has **zero state variables** and two events

---

## ✅ Layer 3 Best Practices

| Practice | This project | General guidance |
|----------|-------------|-----------------|
| Use events for audit history | ✅ All attestation/revocation/version history in events | Always prefer events over storage for append-only data |
| Proxy pattern for upgradeability | ✅ UUPS via OpenZeppelin | Consider transparent proxy if you need simpler upgrade auth |
| Role-based access control | ✅ AUDITOR_ROLE enforced on-chain | Recommended (not implemented here): time-limited role grants |
| Chain-DB dual write | ✅ On-chain event + DB row | Recommended (not implemented here): event-sourcing reconciliation job |
| Confirmation window | ✅ 20 blocks before trust decisions | Recommended: adjust window based on chain's reorg history |

---

## 🚀 Ready for Lesson 22?

Next up: **The Switchboard — Chain Abstraction Layer**. We'll dive into `api/services/chain.py` — the Python layer that routes every chain operation between `local` mode (no gas needed) and `web3` mode (live Polygon), keeping everything downstream mode-agnostic.

*Remember: The blockchain is a global, tamper-evident notary. Anyone can verify the attestation — they don't need to trust AgentLedger to do it.* 🛡️
