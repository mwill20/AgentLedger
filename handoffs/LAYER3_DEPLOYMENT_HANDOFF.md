# AgentLedger — Layer 3 Deployment Handoff
**Created:** April 2026  
**Status:** Waiting on Amoy testnet POL tokens  
**Resume at:** Step 3 — Contract Deployment

---

## Where We Are

Layer 3 (Trust & Verification) is fully built and code-complete. The only thing 
blocking Layer 3 completion is deploying two smart contracts to the Polygon Amoy 
testnet. Everything else is done.

### What Is Already Complete
- Layer 1: Manifest Registry — ✅ COMPLETE (all 10 acceptance criteria)
- Layer 2: Identity & Attestation — ✅ COMPLETE (all 10 acceptance criteria)
- Layer 3: Code — ✅ COMPLETE (228 Python tests passing, 4 contract tests passing)
- Layer 3: Hardhat config updated — polygonAmoy, chainId 80002 ✅
- Layer 3: deploy.js updated to print tx hashes ✅
- Layer 3: grant_roles.js created ✅
- Alchemy account created — ✅ (Michael's First App)
- MetaMask wallet created — ✅
- Polygon Amoy RPC endpoint — ✅

### What Is Blocked
- Amoy POL token balance is ~0.0067 POL
- Minimum needed for deployment: 0.05 POL
- The Alchemy faucet (which gives 0.5 POL) has a 24-hour cooldown
- Cooldown resets approximately 20 hours from when you stopped

---

## Do While Waiting For More Amoy POL

These tasks do not require gas and should be completed before retrying deployment. The goal is to make tomorrow's work a straight deploy-and-verify run.

### 1. Do not retry deployment until the wallet is funded

The last deployment attempt failed before any contract was deployed because the deployer wallet did not have enough Amoy POL for gas.

Current blocker:
- Wallet balance was about `0.0067` Amoy POL.
- The first proxy deployment alone needed about `0.0189` Amoy POL.
- Full deployment plus role grants should not be retried until the wallet has at least `0.05` Amoy POL. Prefer `0.1` Amoy POL so we have room for retries.

Do not run `npx hardhat run contracts/scripts/deploy.js --network polygonAmoy` again until the balance is above that threshold.

### 2. Verify local contract tooling is ready

Run this while waiting:

```powershell
npm run contracts:test
```

Expected result:

```text
4 passing
```

If this fails, fix the contract/tooling issue before touching the chain.

### 3. Confirm the deployer address and balance without exposing secrets

Use this command from the repo root. It prints only the public wallet address and current balance.

```powershell
node -e "require('dotenv').config(); const { ethers } = require('ethers'); (async () => { const provider = new ethers.JsonRpcProvider(process.env.AMOY_RPC_URL); const wallet = new ethers.Wallet(process.env.CHAIN_SIGNER_PRIVATE_KEY, provider); const balance = await provider.getBalance(wallet.address); console.log(JSON.stringify({ address: wallet.address, balancePOL: ethers.formatEther(balance) }, null, 2)); })().catch((err) => { console.error(err); process.exit(1); });"
```

Expected before proceeding:

```text
balancePOL >= 0.05
```

Do not paste `CHAIN_SIGNER_PRIVATE_KEY` or the full RPC URL into chat, docs committed to git, screenshots, or issue comments.

### 4. Preserve current repo state

Do not reset, clean, or switch branches while waiting. The current branch already contains the required Amoy prep work:

- `hardhat.config.js` has `polygonAmoy` with chain ID `80002`.
- `contracts/scripts/deploy.js` prints proxy deployment tx hashes and block numbers.
- `contracts/scripts/grant_roles.js` grants `ADMIN_ROLE`, `AUDITOR_ROLE`, and `ANCHOR_ROLE`.
- `.env.example` includes `AMOY_RPC_URL`.

Tomorrow should start from this same branch: `layer3/trust-verification`.

### 5. Prepare the acceptance test target service

Layer 3 attestation needs a service that already exists in the database. Before deployment, identify one candidate service ID and domain:

```powershell
docker compose exec db psql -U agentledger -d agentledger -c "SELECT id, domain, trust_tier FROM services WHERE is_active = true ORDER BY created_at DESC LIMIT 5;"
```

Save one `id` and `domain` from the output. Use that same service for tomorrow's attestation, trust-tier, revocation, and blocklist checks.

### 6. Tomorrow's first command sequence

Once the wallet balance is funded, tomorrow starts here:

```powershell
npm run contracts:test
npx hardhat run contracts/scripts/deploy.js --network polygonAmoy
```

After deployment succeeds, copy these from the JSON output:

- `attestationLedger.address`
- `attestationLedger.deployment.txHash`
- `attestationLedger.deployment.blockNumber`
- `auditChain.address`
- `auditChain.deployment.txHash`
- `auditChain.deployment.blockNumber`

Use the earlier of the two deployment block numbers as `CHAIN_START_BLOCK`.

Then run the role grant command using the deployed proxy addresses and the deployer wallet's public address:

```powershell
npx hardhat run contracts/scripts/grant_roles.js --network polygonAmoy -- <attestationLedger address> <auditChain address> <signerAddress>
```

Capture all three grant tx hashes before moving on.

---
## Credentials You Already Have

Store these safely — do NOT share publicly:

| Item | Where it lives |
|---|---|
| Alchemy RPC URL | Your .env file as AMOY_RPC_URL |
| MetaMask private key | Your .env file as CHAIN_SIGNER_PRIVATE_KEY |
| MetaMask wallet address | MetaMask extension (0x...) |
| MetaMask password | You set this during install |

Your AMOY_RPC_URL is:
```
https://polygon-amoy.g.alchemy.com/v2/uEcCPvlNmAASxFXWYFpS6
```

---

## Step-by-Step: What To Do When You Return

### Step 1 — Claim POL tokens (you do this, ~5 min)

1. Go to: https://alchemy.com/faucets/polygon-amoy
2. Log in with your Alchemy account
3. Paste your MetaMask wallet address (0x...)
4. Request tokens — you should be able to claim now that 24 hours have passed
5. Open MetaMask and wait 1-2 minutes for the balance to show
6. Confirm you see at least 0.1 POL in MetaMask before proceeding

If Alchemy faucet still shows an error, try the backup:
- Go to: https://faucet.polygon.technology
- Select Polygon Amoy + POL
- Connect GitHub (github.com/mwill20) for identity verification
- Claim

---

### Step 2 — Confirm your .env is set correctly

Open your AgentLedger repo and verify these exact values are in your .env file:

```
AMOY_RPC_URL=https://polygon-amoy.g.alchemy.com/v2/uEcCPvlNmAASxFXWYFpS6
CHAIN_SIGNER_PRIVATE_KEY=<your MetaMask private key>
CHAIN_ID=80002
CHAIN_NETWORK=polygonAmoy
CHAIN_MODE=web3
```

Do not proceed to Step 3 until all five lines are in .env.

---

### Step 3 — Deploy contracts (give this to Claude Code)

Paste this exact prompt into Claude Code:

```
My Amoy wallet is now funded. Deploy the Layer 3 contracts to Polygon Amoy.

Run:
  npx hardhat run contracts/scripts/deploy.js --network polygonAmoy

Wait for it to complete. Copy both proxy contract addresses from the output.

Then run:
  npx hardhat run contracts/scripts/grant_roles.js --network polygonAmoy \
    -- <attestationLedger address> <auditChain address> <signerAddress>

Where <signerAddress> is the MetaMask wallet address (0x...) from CHAIN_SIGNER_PRIVATE_KEY.

After both commands succeed, set these in .env:
  ATTESTATION_LEDGER_CONTRACT_ADDRESS=<attestationLedger proxy address>
  AUDIT_CHAIN_CONTRACT_ADDRESS=<auditChain proxy address>
  CHAIN_START_BLOCK=<block number from deploy output>

Show me:
1. The transaction hash for AttestationLedger deployment
2. The transaction hash for AuditChain deployment
3. The transaction hashes for all 3 role grants
4. The two contract addresses

Do not proceed past this point until I confirm the tx hashes.
```

---

### Step 4 — Run end-to-end acceptance (give this to Claude Code)

Only do this AFTER Step 3 tx hashes are confirmed. Paste this into Claude Code:

```
Layer 3 contracts are deployed. Run the full end-to-end acceptance sequence 
against Polygon Amoy. CHAIN_MODE=web3 must be active.

Run these in order and show the output of each:

1. POST /v1/attestation/auditors/register
   Register a test auditor. Show the response.

2. POST /v1/attestation/submit
   Submit an attestation for any service already in the DB.
   Show the response including the on-chain tx hash.

3. Poll GET /v1/chain/status every 10 seconds until 
   confirmation_depth >= 20. Show the final status response.
   (This takes about 40 seconds on Amoy — ~2 second block times)

4. GET /v1/services/{service_id}
   Show that trust_tier=4 and attestation_score > 0.

5. POST /v1/attestation/revoke
   Revoke the attestation from step 2.
   Show the on-chain tx hash.

6. GET /v1/federation/blocklist
   Show that the revoked service appears in the blocklist.

7. Run Locust load test against Layer 3 endpoints:
   100 concurrent users, 30 seconds
   Target: p95 < 500ms
   Show the full Locust output.

Layer 3 is not complete until all 7 steps produce clean output.
```

---

### Step 5 — Generate completion document (give this to Claude Code)

Only after all 7 acceptance steps pass:

```
All Layer 3 acceptance criteria have passed. Generate 
spec/LAYER3_COMPLETION.md in the same format as 
spec/LAYER1_COMPLETION.md and spec/LAYER2_COMPLETION.md.

Include:
- All 10 acceptance criteria checked off
- All build phases with commit references
- Test count (currently 228 Python + 4 contract)
- Load test results
- On-chain tx hashes for deployment and acceptance run
- 5 Layer 4 integration points for the next session

Then commit:
  git add spec/LAYER3_COMPLETION.md
  git commit -m "feat: Layer 3 complete — all 10 acceptance criteria verified"
  git push
```

---

## Layer 3 Acceptance Criteria (10 gates)

These must all be checked before Layer 3 is complete:

```
[ ] Attestation Events written and indexed on Amoy chain
[ ] Revocation Events propagate to federation blocklist
[ ] Version Events track manifest changes immutably
[ ] Trust score recomputes with attestation_score populated
[ ] Trust tier 4 gate activates after live attestation
[ ] Audit batch anchoring confirmed on-chain
[ ] Federation blocklist endpoint returns revoked services
[ ] Live chain deployment verified (real Amoy tx hashes)
[ ] 20-block confirmation behavior verified on real chain
[ ] Layer 3 load targets: p95 < 500ms @ 100 concurrent
```

---

## What Comes After Layer 3

Once Layer 3 is signed off, the next session begins Layer 4: Context Matching.

Layer 4 NorthStar: Agents route the right user context to the right service — 
sharing only what is necessary, protecting everything else, with full user control.

Key Layer 4 concepts to design:
- Privacy-preserving context disclosure
- User-controlled context profiles  
- Zero-knowledge proofs for high-sensitivity fields (medical, financial)
- Context mismatch detection
- GDPR/CCPA compliance enforcement by protocol

Do not begin Layer 4 design until LAYER3_COMPLETION.md is committed.

---

## If Something Goes Wrong

**Faucet still won't work:**
Try https://polygon-amoy.drpc.org as your RPC URL instead of Alchemy.
It's free, no signup, no rate limits for small deployments.

**Deployment fails with gas error again:**
Your balance is still too low. Request from both faucets before retrying.

**Claude Code asks which network to deploy to:**
Always answer: polygonAmoy (not polygonMumbai, not mainnet)

**You can't find your MetaMask private key:**
MetaMask extension → click account name → three dots → Account Details → 
Export Private Key → enter your MetaMask password

**You can't remember your Alchemy login:**
Go to alchemy.com → sign in with whatever email you used to create the account

---

*Save this document. When you return, start at Step 1 and work straight through.*
*Do not skip steps or do them out of order.*


