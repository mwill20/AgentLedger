# 🎓 Lesson 29: The Inspector General — Live Amoy Acceptance Run

## 🕵️ Welcome Back, Agent Architect!

You've studied every Layer 3 component in `CHAIN_MODE=local`. Now it's time to verify the real thing — an actual deployment to Polygon Amoy testnet, live transaction hashes, real block confirmations, and independent on-chain verification.

Think of an **inspector general**: they don't just review internal reports. They show up unannounced, read the original records directly, and compare them to what the organization claims. This lesson is your independent verification run — you'll verify attestation events exist on Amoy without trusting AgentLedger's API at all.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Fund an Amoy testnet wallet and configure AgentLedger for `CHAIN_MODE=web3`
- ✅ Deploy `AttestationLedger` and `AuditChain` contracts to Polygon Amoy
- ✅ Run the full 10-criterion acceptance sequence against the live chain
- ✅ Independently verify an attestation event using a direct `eth_getLogs` call
- ✅ Interpret the chain status polling output and understand what `confirmation_depth >= 20` means
- ✅ Record your own transaction hashes in the acceptance format

**Estimated time:** 90–120 minutes (including live chain wait times)
**Prerequisites:** All of Lessons 21–28; active Polygon Amoy wallet with POL tokens

> ⚠️ **Wallet required:** This lesson requires a MetaMask wallet funded with at least **0.1 Amoy POL**. The Alchemy faucet at [faucet.polygon.technology](https://faucet.polygon.technology) gives 0.5 POL per day; the Polygon faucet at [alchemy.com/faucets/polygon-amoy](https://alchemy.com/faucets/polygon-amoy) gives a backup claim. If your wallet balance is below 0.05 POL, do not attempt deployment — the transaction will fail mid-sequence.

---

## 🔍 What This Lesson Covers

```
Local machine (.env configured)
         │
         │  CHAIN_MODE=web3
         │  AMOY_RPC_URL=https://polygon-amoy.g.alchemy.com/v2/...
         │  ATTESTATION_LEDGER_CONTRACT_ADDRESS=0x...
         │
         ▼
Polygon Amoy Testnet (chain_id=80002)
         │
         ├── AttestationLedger.sol (UUPS proxy)
         │    └── records: AttestationRecorded, RevocationRecorded, ServiceVersionUpdated
         │
         └── AuditChain.sol (UUPS proxy)
              └── records: BatchAnchorCommitted
```

**Key files:**
- [`contracts/scripts/deploy.js`](../../contracts/scripts/deploy.js) — deployment script
- [`contracts/scripts/grant_roles.js`](../../contracts/scripts/grant_roles.js) — role grant script
- [`spec/LAYER3_COMPLETION.md`](../../spec/LAYER3_COMPLETION.md) — reference tx hashes from the completed run
- [`handoffs/LAYER3_DEPLOYMENT_HANDOFF.md`](../../handoffs/LAYER3_DEPLOYMENT_HANDOFF.md) — step-by-step deployment checklist

---

## 🔧 Prerequisites Checklist

Before beginning the acceptance run, verify all of the following:

```bash
# 1. Confirm Amoy wallet balance (must be >= 0.05 POL)
node -e "
require('dotenv').config();
const { ethers } = require('ethers');
(async () => {
  const provider = new ethers.JsonRpcProvider(process.env.AMOY_RPC_URL);
  const wallet = new ethers.Wallet(process.env.CHAIN_SIGNER_PRIVATE_KEY, provider);
  const balance = await provider.getBalance(wallet.address);
  console.log({ address: wallet.address, balancePOL: ethers.formatEther(balance) });
})().catch(err => { console.error(err); process.exit(1); });
"
```

**Minimum balance:** 0.05 POL. **Recommended:** 0.1 POL (room for retries).

```bash
# 2. Confirm contract tests pass locally (before touching the chain)
npm run contracts:test
```

**Expected:** `4 passing`

```bash
# 3. Confirm .env has all required Layer 3 chain variables
grep -E 'CHAIN_MODE|AMOY_RPC_URL|CHAIN_ID|CHAIN_SIGNER_PRIVATE_KEY|CHAIN_CONFIRMATION_BLOCKS' .env
```

**Required values:**
```
CHAIN_MODE=web3
AMOY_RPC_URL=https://polygon-amoy.g.alchemy.com/v2/<your-key>
CHAIN_ID=80002
CHAIN_SIGNER_PRIVATE_KEY=<your-private-key>
CHAIN_CONFIRMATION_BLOCKS=20
```

> ⚠️ **Never commit `.env` to git.** The `.gitignore` already excludes it, but double-check before any push.

```bash
# 4. Confirm Docker stack is running
docker compose ps
```

**Expected:** `api`, `db`, `redis`, `worker` all in `running` state.

---

## 🚀 Step 1: Deploy Contracts to Polygon Amoy

```bash
# Deploy both UUPS proxies
npx hardhat run contracts/scripts/deploy.js --network polygonAmoy
```

**Expected output (save every address and hash):**
```json
{
  "attestationLedger": {
    "address": "0x...",
    "deployment": {
      "txHash": "0x...",
      "blockNumber": 37400939
    }
  },
  "auditChain": {
    "address": "0x...",
    "deployment": {
      "txHash": "0x...",
      "blockNumber": 37400945
    }
  }
}
```

**If the deployment fails with a gas error:** Your wallet balance is too low. Request from the faucet at `https://faucet.polygon.technology` and try again after 2 minutes.

After a successful deployment, update your `.env`:
```
ATTESTATION_LEDGER_CONTRACT_ADDRESS=<attestationLedger.address>
AUDIT_CHAIN_CONTRACT_ADDRESS=<auditChain.address>
CHAIN_START_BLOCK=<the earlier blockNumber>
```

Then verify on [Polygon Amoy PolygonScan](https://amoy.polygonscan.com/). Search for your `attestationLedger.address` — you should see the deployment transaction.

---

## 🔑 Step 2: Grant Roles

```bash
# Grant AUDITOR_ROLE, ANCHOR_ROLE, and ADMIN_ROLE
npx hardhat run contracts/scripts/grant_roles.js --network polygonAmoy \
  -- <attestationLedger.address> <auditChain.address> <your-signer-wallet-address>
```

**Expected output (save all 3 tx hashes):**
```
Granted AUDITOR_ROLE tx: 0x...  block: 37400994
Granted ANCHOR_ROLE tx: 0x...   block: 37400996
ADMIN_ROLE was granted during proxy initialization — no separate tx needed.
```

**What each role does:**

| Role | Contract | Who needs it |
|------|----------|--------------|
| `AUDITOR_ROLE` | `AttestationLedger` | The app signer wallet that calls `recordAttestation` and `recordRevocation` |
| `ANCHOR_ROLE` | `AuditChain` | The app signer wallet that calls `commitBatch` |
| `ADMIN_ROLE` | Both | The deployer wallet (already granted during initialization) |

Without `AUDITOR_ROLE`, all calls to `recordAttestation` will revert with `AccessControl: account 0x... is missing role`. Without `ANCHOR_ROLE`, all `commitBatch` calls revert.

---

## 🔄 Step 3: Restart the Worker

```bash
# Restart the Celery worker so it picks up the new .env values
docker compose restart worker
```

Verify the worker sees the new configuration:
```bash
docker compose logs worker --tail=20
```

**Expected:** Log lines showing `CHAIN_MODE=web3` and the Alchemy RPC URL being used.

---

## 🧪 The 10-Criterion Acceptance Sequence

### Criterion 1: Register Two Auditors from Different Organizations

```bash
# Auditor 1 — org: health-auditors.org
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:health-auditors.org",
    "name": "Health Security Labs",
    "ontology_scope": ["health.*"],
    "chain_address": "<your-signer-wallet-address>"
  }' | python3 -m json.tool

# Auditor 2 — org: security-labs.io (different domain = different org_id)
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:security-labs.io",
    "name": "Security Labs Inc",
    "ontology_scope": ["health.*"],
    "chain_address": "<your-signer-wallet-address>"
  }' | python3 -m json.tool
```

### Criterion 2: Find Your Target Service

```bash
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT id, domain, trust_tier FROM services WHERE is_active = true ORDER BY created_at DESC LIMIT 5;"
```

**Pick one service.** Use its `id` and `domain` for all subsequent steps. Save them — you'll need both throughout the acceptance run.

### Criterion 3: Submit Two Attestations (one per auditor)

```bash
SERVICE_DOMAIN="<your-chosen-domain>"

# Attestation from Auditor 1
curl -s -X POST http://localhost:8000/v1/attestations \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"auditor_did\": \"did:web:health-auditors.org\",
    \"service_domain\": \"${SERVICE_DOMAIN}\",
    \"ontology_scope\": \"health.*\",
    \"evidence_package\": {\"type\": \"automated_scan\", \"result\": \"pass\", \"tool\": \"HealthCheck v2\"}
  }" | python3 -m json.tool
```

**Save the `tx_hash` from the response.** It will look like `0xff6f0a...`.

```bash
# Attestation from Auditor 2
curl -s -X POST http://localhost:8000/v1/attestations \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"auditor_did\": \"did:web:security-labs.io\",
    \"service_domain\": \"${SERVICE_DOMAIN}\",
    \"ontology_scope\": \"health.*\",
    \"evidence_package\": {\"type\": \"manual_review\", \"result\": \"pass\", \"reviewer\": \"Jane Smith\"}
  }" | python3 -m json.tool
```

**Save the second `tx_hash`.**

### Criterion 4: Wait for 20-Block Confirmation

```bash
# Poll every 10 seconds until confirmation_depth >= 20 for both txs
TX1="<attestation-1-tx-hash>"
TX2="<attestation-2-tx-hash>"

# Check status for tx1
curl -s "http://localhost:8000/v1/chain/status?tx_hash=${TX1}" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected interim response (still pending):**
```json
{
  "chain_mode": "web3",
  "latest_block": 37402210,
  "tx_status": {
    "tx_hash": "0xff6f0a...",
    "block_number": 37402202,
    "confirmation_depth": 8,
    "is_confirmed": false
  }
}
```

**Expected final response (confirmed):**
```json
{
  "chain_mode": "web3",
  "latest_block": 37402225,
  "tx_status": {
    "tx_hash": "0xff6f0a...",
    "block_number": 37402202,
    "confirmation_depth": 23,
    "is_confirmed": true
  }
}
```

**What you're watching:** `confirmation_depth = latest_block - tx_block_number`. Once it reaches 20, `confirm_chain_events` will promote this event to `is_confirmed=true` on the next 5-second beat. On Polygon Amoy (~2s blocks), this takes approximately **40–50 seconds** from submission.

> **If you're impatient:** `confirmation_depth` growing from 0 to 20 is ~40 seconds of real waiting. This is intentional — use this time to observe what the Celery worker is doing:
> ```bash
> docker compose logs worker --tail=50 --follow
> ```
> You'll see `index_chain_events` and `confirm_chain_events` beating every 5 seconds, with log lines showing the indexed event count and confirmation state.

### Criterion 5: Verify Trust Tier 4

```bash
SERVICE_ID="<your-service-id>"

curl -s "http://localhost:8000/v1/services/${SERVICE_ID}" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected:**
```json
{
  "id": "<service-id>",
  "domain": "<domain>",
  "trust_tier": 4,
  "trust_score": 49.9,
  "attestation_score": 1.0
}
```

**If `trust_tier` is not 4:** Check that both attestations are confirmed (`is_confirmed=true` in `attestation_records`):
```bash
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT id, auditor_id, is_confirmed, created_at FROM attestation_records WHERE service_id = '<service-id>';"
```

### Criterion 6: Submit Revocation

```bash
curl -s -X POST http://localhost:8000/v1/attestations/revoke \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d "{
    \"auditor_did\": \"did:web:health-auditors.org\",
    \"service_domain\": \"${SERVICE_DOMAIN}\",
    \"reason\": \"security_incident\"
  }" | python3 -m json.tool
```

**Save the revocation `tx_hash`.** Wait for `confirmation_depth >= 20` as before.

### Criterion 7: Verify Federation Blocklist

```bash
curl -s http://localhost:8000/v1/federation/blocklist \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected:** Your service's domain appears in `revocations[]` with the revocation tx hash.

### Criterion 8: Create Audit Records and Anchor a Batch

```bash
# Create 3 audit records
for i in 1 2 3; do
  curl -s -X POST http://localhost:8000/v1/audit/records \
    -H "X-API-Key: dev-local-only" \
    -H "Content-Type: application/json" \
    -d "{
      \"agent_did\": \"did:key:test-agent-${i}\",
      \"action_type\": \"context_disclosure\",
      \"action_context\": {\"context_type\": \"medical_record\", \"disclosed_fields\": [\"allergy_list\"]},
      \"outcome\": \"success\"
    }" | python3 -m json.tool
done

# Wait for the 60-second anchor beat to fire, or manually trigger it:
docker compose exec api python3 -c "
import asyncio
from crawler.tasks._async_db import run_with_fresh_session
from api.services import audit

async def run():
    result = await run_with_fresh_session(audit.anchor_pending_records)
    print(result)

asyncio.run(run())
"
```

**Save the `batch_id` and `tx_hash` from the anchor output.**

### Criterion 9: Verify Audit Batch Integrity

```bash
RECORD_ID="<one-of-the-created-record-ids>"

curl -s "http://localhost:8000/v1/audit/records/${RECORD_ID}/verify" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected:**
```json
{
  "record_id": "<record-id>",
  "integrity_valid": true,
  "merkle_proof_valid": true,
  "on_chain_root_matches": true,
  "batch_status": "confirmed"
}
```

### Criterion 10: Load Test (Optional for live run; required for acceptance)

```bash
# Run Locust against Layer 3 endpoints (100 users, 30 seconds)
docker compose exec api locust -f tests/locustfile.py \
  --headless \
  --users 100 \
  --spawn-rate 10 \
  --run-time 30s \
  --host http://localhost:8000 \
  --tags layer3 \
  --csv /tmp/layer3_load
```

**Target:** `p95 < 500ms`, `failures = 0`

**Reference result** (from `spec/LAYER3_COMPLETION.md`):
```
Total requests: 6681 | Failures: 0 | Median: 8ms | p95: 92ms | p99: 130ms
```

---

## 🔍 Independent On-Chain Verification

This is the inspector general's signature move: verify the attestation event **without using AgentLedger's API** — by calling `eth_getLogs` directly on the Alchemy RPC.

```bash
# Verify the AttestationRecorded event exists on Amoy
# without trusting AgentLedger
docker compose exec api python3 -c "
from web3 import Web3
import os

# Connect directly to Amoy
rpc_url = os.getenv('AMOY_RPC_URL')
w3 = Web3(Web3.HTTPProvider(rpc_url))

contract_address = os.getenv('ATTESTATION_LEDGER_CONTRACT_ADDRESS')

# The AttestationRecorded event topic (keccak256 of the event signature)
# Matches: AttestationRecorded(bytes32,bytes32,bytes32,string,uint256)
ATTESTATION_RECORDED_TOPIC = w3.keccak(
    text='AttestationRecorded(bytes32,bytes32,bytes32,string,uint256)'
).hex()

print('Topic:', ATTESTATION_RECORDED_TOPIC)

# Query eth_getLogs for this event from the target transaction
tx_hash = '<YOUR-ATTESTATION-TX-HASH>'
receipt = w3.eth.get_transaction_receipt(tx_hash)

if receipt is None:
    print('Transaction not found on chain!')
else:
    print('Transaction found in block:', receipt.blockNumber)
    print('Status:', 'success' if receipt.status == 1 else 'FAILED')
    for log in receipt.logs:
        if log.address.lower() == contract_address.lower():
            print('Event emitted by AttestationLedger:')
            print('  Topics:', [t.hex() for t in log.topics])
"
```

**Expected output:**
```
Topic: 0x<keccak256-of-event-sig>
Transaction found in block: 37402202
Status: success
Event emitted by AttestationLedger:
  Topics: ['0x<AttestationRecorded-topic>', '0x<service-id-hash>', '0x<auditor-hash>']
```

This proves the event exists on-chain independently of AgentLedger's database. The `Topics[0]` is the keccak256 of the event signature — a permanent on-chain record that no database administrator can modify.

---

## 📝 Recording Your Acceptance Run

Mirroring the format from `spec/LAYER3_COMPLETION.md`, record your own transaction table:

```markdown
## My Acceptance Run — [Date]

### Contract Deployment

| Contract | Address | Deployment Tx | Block |
|----------|---------|---------------|-------|
| AttestationLedger proxy | 0x... | 0x... | ... |
| AuditChain proxy | 0x... | 0x... | ... |

### Role Grants

| Role | Tx hash | Block |
|------|---------|-------|
| AUDITOR_ROLE | 0x... | ... |
| ANCHOR_ROLE | 0x... | ... |

### Acceptance Transactions

| Flow | Tx hash | Block |
|------|---------|-------|
| Attestation 1 | 0x... | ... |
| Attestation 2 | 0x... | ... |
| Revocation | 0x... | ... |
| Audit batch anchor | 0x... | ... |
```

---

## 🔧 Troubleshooting

**"Deployment fails with 'insufficient gas'"**
Your wallet has < 0.05 POL. Request from `faucet.polygon.technology` (Polygon faucet, not Ethereum) and wait 2 minutes.

**"Faucet shows an error"**
The Alchemy faucet has a 24-hour cooldown per wallet address. If you claimed within the last 24 hours, use the backup faucet at `https://faucet.polygon.technology` → select Polygon Amoy + POL → connect GitHub for identity verification.

**"trust_tier is still 3 after two attestations"**
Both attestations must be `is_confirmed=true`. Check `attestation_records`:
```bash
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT id, is_confirmed, auditor_id FROM attestation_records WHERE service_id='<id>';"
```
If `is_confirmed=false`, wait for more blocks and check `docker compose logs worker` for the confirm task firing.

**"Transaction not found on chain"**
Your Alchemy RPC URL may have expired or be rate-limited. Test with:
```bash
curl -s -X POST "$AMOY_RPC_URL" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}'
```
Expected: `{"result":"0x..."}` (a hex block number).

**"CHAIN_MODE=local after restarting the stack"**
The `.env` file has `CHAIN_MODE=local`. Change to `web3` and restart the stack: `docker compose up -d`.

---

## 📊 Summary Reference Card

| Item | Value from completed run |
|------|--------------------------|
| Network | Polygon Amoy (`chain_id=80002`) |
| AttestationLedger proxy | `0x7961BC0F69Dac95309F197E176ea8CD1D3EbF23D` |
| AuditChain proxy | `0x55366DA11A48e2dCFE3F67f9802aF3e032dC2244` |
| Attestation 1 tx | `0xff6f0a456c36452a85a437b68240678b27a8b042185c9c403429d2e9825a7b55` |
| Attestation 2 tx | `0x867cee7807f664396d1d215fb1c61f14b52539e381ec674ff3dd034675e08e8b` |
| Revocation tx | `0xfcd55ffde0653ca09a267f049b82402f5fc06a2de812995b59beb713c8f20f3f` |
| Audit batch tx | `0xe066d788317d44e0241e2a71b21f5cb76462ce1e0302f23f84e93fc765be1b9b` |
| 20-block wait time | ~40 seconds (2s/block × 20 blocks) |
| Trust tier after 2 orgs | `4` |
| Attestation score | `1.0` |

---

## 📚 Interview Preparation

**Q: How would you explain "blockchain-anchored trust" to a non-technical stakeholder in 60 seconds?**

**A:** "Imagine a government notary who stamps official documents — but instead of paper stamps, every approval gets written into a shared ledger that thousands of computers worldwide hold identical copies of. Once it's written, no single person — not even us — can change or delete it. So when our system says 'this AI service was approved by two independent security firms on April 27, 2026 at 2:34pm,' that's a permanent fact that anyone can verify independently, and that no administrator can quietly revise later. That's what blockchain-anchored trust means: the approval is tamper-proof and publicly auditable."

**Q: What's the minimum wallet balance needed for a full Layer 3 deployment?**

**A:** Based on the acceptance run, you need approximately 0.05 Amoy POL. The two UUPS proxy deployments consume roughly 0.025–0.035 POL each (gas is variable). Role grants consume ~0.001 POL each. Attestation, revocation, and audit batch anchor transactions each consume <0.005 POL. Total: ~0.05–0.08 POL. The recommended funding is 0.1 POL to allow for retry headroom.

**Q: How do you prove a record exists on-chain without trusting AgentLedger?**

**A:** Call `eth_getTransactionReceipt(tx_hash)` directly on the Polygon Amoy RPC endpoint using any web3 client (`web3.py`, `ethers.js`, `cast`). The receipt contains the event logs emitted by the contract. Verify that: (1) `receipt.status == 1` (transaction succeeded), (2) the log's `address` matches the `AttestationLedger` contract address, and (3) `log.topics[0]` matches the keccak256 of `AttestationRecorded(bytes32,bytes32,bytes32,string,uint256)`. If all three match, the event is on-chain and was emitted by the correct contract — regardless of what AgentLedger's database says.

---

## ✅ Key Takeaways

- Live Amoy deployment requires: funded wallet (≥0.05 POL), Alchemy RPC URL, Hardhat config for `polygonAmoy`, role grants after deployment
- The 20-block confirmation window takes ~40 seconds on Amoy (2s/block) — plan for this delay in any live testing
- Independent verification requires only a wallet address and the tx hash — no AgentLedger dependency, no API key
- Record your own transaction hashes in the acceptance format — they're your proof that the system works end-to-end on a real public chain
- `CHAIN_MODE=local` gets you 95% of the way there for development; `CHAIN_MODE=web3` is the final 5% that proves the integration is real

---

## 🎓 Layer 3 Complete — What Comes Next?

You've now covered the entire Layer 3 curriculum: contracts, chain abstraction, auditor registration, attestation pipeline, trust scoring, Merkle audit batching, federation, background workers, hardening, and a live acceptance run.

Layer 4 (Context Matching) builds on these surfaces:
- `attestation_records` + `trust_tier` gate context disclosure decisions
- `audit_records` provide the tamper-evident trail for context disclosures
- The federated blocklist blocks revoked services from receiving any context
- `AuditChain.sol` is UUPS upgradeable to add a `ContextDisclosureAnchored` event

The foundation is solid. The watchmen are running. The ledger is tamper-proof.

*Remember: An inspector who can't verify independently isn't inspecting — they're just reading the report. Always call `eth_getLogs` yourself.* 🕵️
