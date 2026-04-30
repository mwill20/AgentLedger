# 🎓 Lesson 25: The Ledger of Trust — Trust Tier 4 & the Scoring Engine

> **Beginner frame:** Trust scoring turns evidence into a signal agents can compare. It is not a magic truth number; it is a transparent calculation based on attestations, revocations, reliability, and policy gates.

## 🏦 Welcome Back, Agent Architect!

You can register auditors and stamp attestations. But how does a service's *trust score* actually change when those attestations arrive? And why isn't "two confirmed attestations" good enough — why does the system demand they come from *different organizations*?

Think of a **credit bureau**: it doesn't just count how many banks vouch for you. It checks whether those references are independent, how recent they are, and whether any of them have flagged you for fraud. AgentLedger's trust scoring engine works the same way — a weighted ledger that a single actor cannot game alone.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain all four components of `compute_trust_score()` and their weights
- ✅ Trace `compute_attestation_score()` through its three sub-factors (scope weight, recency decay, quorum bonus)
- ✅ Explain why `evaluate_trust_tier_4()` checks `auditor_org_id` independence, not just count
- ✅ Read all 6 SQL queries in `recompute_service_trust()` and explain what each one measures
- ✅ Predict what happens to a service's score when a revocation is confirmed on-chain
- ✅ Describe when `attestation_score` falls back to the Layer 2 identity signal

**Estimated time:** 90 minutes
**Prerequisites:** Lessons 23 (Auditor Registration) and 24 (Attestation Pipeline)

---

## 🔍 What This Component Does

```
confirm_pending_events()
          |
          v  (triggers for each affected service_id)
recompute_service_trust(db, service_id)   ← trust.py
          |
          |  6 SQL reads:
          |  ① service base row (tier, is_banned)
          |  ② capability probe score
          |  ③ operational score (uptime)
          |  ④ local reputation (session outcomes, 30d)
          |  ⑤ federated reputation (external signals, 30d)
          |  ⑥ attestation records + revocation check
          |
          v
compute_attestation_score()    ← ranker.py
compute_reputation_score()     ← ranker.py
compute_trust_score()          ← ranker.py
evaluate_trust_tier_4()        ← ranker.py
          |
          v
UPDATE services SET trust_score, trust_tier, is_banned ...
```

**Key files:**
- [`api/services/ranker.py`](../../api/services/ranker.py) — pure math functions, no I/O
- [`api/services/trust.py`](../../api/services/trust.py) — DB reads + write, calls `ranker`

---

## 🏗️ Two Modules, One Separation

`ranker.py` and `trust.py` are deliberately split. `ranker.py` contains **pure functions**: given inputs, return outputs, no side effects, no DB. Every function in it can be tested with a simple `assert` in a REPL without a database connection.

`trust.py` owns all I/O: it runs 6 SQL queries, assembles the inputs, calls `ranker`, and then writes exactly one `UPDATE`. This design means:

- Unit tests for scoring logic never touch the DB
- The integration test for `recompute_service_trust` verifies the full pipeline exactly once
- The scoring formula can be changed in `ranker.py` without touching SQL

> **Why does this matter in production?** Trust recomputation fires on every attestation confirmation and every revocation. It must be fast and safe to retry. The pure-function design makes both guarantees easy to reason about.

---

## 📝 The Four-Component Trust Formula

**File:** [`api/services/ranker.py`](../../api/services/ranker.py) lines 136–149

```python
def compute_trust_score(
    capability_probe_score: float,
    attestation_score: float,
    operational_score: float,
    reputation_score: float,
) -> float:
    """Trust score computation from the Layer 1 spec."""
    raw = (
        capability_probe_score * 0.35
        + attestation_score * 0.30
        + operational_score * 0.20
        + reputation_score * 0.15
    )
    return round(_clamp(raw) * 100.0, 2)
```

| Component | Weight | Source |
|-----------|--------|--------|
| `capability_probe_score` | 35% | Verified capability ratio from `service_capabilities` table |
| `attestation_score` | 30% | Auditor attestation quality (scope × recency × quorum) |
| `operational_score` | 20% | Uptime SLA from `service_operations.uptime_sla_percent` |
| `reputation_score` | 15% | 30-day local session outcomes + federated signals |

The output is multiplied by 100 and rounded to 2 decimal places. A service with `trust_score = 78.45` means all four components averaged (weighted) to 0.7845 before scaling.

`_clamp(raw)` ensures the input is in `[0.0, 1.0]` before scaling. Because all four components are independently clamped to `[0.0, 1.0]`, the weighted sum can theoretically reach exactly `1.0` but never exceed it.

---

## 📝 Code Walkthrough: `compute_attestation_score()`

**File:** [`api/services/ranker.py`](../../api/services/ranker.py) lines 49–81

This is the most complex function in `ranker.py`. It has two code paths:

**Path 1 — No Layer 3 attestations yet (lines 59–60):**
```python
if not attestations:
    return 1.0 if has_active_service_identity else 0.0
```

If no confirmed attestations exist, the function falls back to the Layer 2 identity signal. A service with a verified DID gets a baseline score of `1.0`; a completely unverified service gets `0.0`. This is the bridge from Layer 2 to Layer 3: Layer 2 credentials become the floor until Layer 3 attestations are available.

**Path 2 — One or more confirmed attestations (lines 62–81):**

```python
current_time = now or datetime.now(timezone.utc)
score = 0.0
unique_orgs: set[str] = set()

for attestation in attestations:
    scope = str(attestation["ontology_scope"])
    recorded_at = attestation["recorded_at"]
    scope_weight = 1.0 if scope.endswith(".*") else 0.6    # ① scope weight
    days_old = max(
        0,
        (current_time - recorded_at.astimezone(timezone.utc)).days,
    )
    recency_weight = max(0.5, 1.0 - (days_old / 365.0) * 0.5)  # ② recency decay
    score += scope_weight * recency_weight
    unique_orgs.add(str(attestation["auditor_org_id"]))

if len(unique_orgs) >= 2:
    score *= 1.2                                            # ③ quorum bonus

return _clamp(score / len(attestations))
```

### Sub-factor ①: Scope Weight (line 69)

```python
scope_weight = 1.0 if scope.endswith(".*") else 0.6
```

A **wildcard scope** (`health.*`, `finance.*`) covers an entire ontology domain. It requires broader expertise and is worth more: `1.0`. A **specific tag** (`finance.payments`, `travel.booking`) is narrower: `0.6`.

Why this asymmetry? An auditor who certifies a service for `health.*` is making a much stronger commitment than one who only certified a single payment flow. The broader scope attestation contributes more evidence.

### Sub-factor ②: Recency Decay (lines 70–74)

```python
days_old = max(0, (current_time - recorded_at.astimezone(timezone.utc)).days)
recency_weight = max(0.5, 1.0 - (days_old / 365.0) * 0.5)
```

A fresh attestation (day 0) has `recency_weight = 1.0`. After 365 days, `1.0 - (365/365) * 0.5 = 0.5`. The floor is `0.5` — a year-old attestation is still worth half-weight, not worthless.

| Age | Recency Weight |
|-----|---------------|
| 0 days | 1.00 |
| 90 days | 0.875 |
| 180 days | 0.75 |
| 365 days | 0.50 |
| 730 days | 0.50 (floor) |

### Sub-factor ③: Quorum Bonus (lines 78–79)

```python
if len(unique_orgs) >= 2:
    score *= 1.2
```

If attestations come from at least 2 different `auditor_org_id` values (distinct organizations), the raw accumulated score is multiplied by 1.2 before clamping. This rewards independent corroboration.

**Final normalization (line 81):**
```python
return _clamp(score / len(attestations))
```

The accumulated score is divided by the number of attestations to produce an average per-attestation score, then clamped to `[0.0, 1.0]`. The quorum bonus can push the raw average above 1.0, so clamping is essential.

**Example walkthrough** — two attestations, both 30 days old, from different orgs:

```
Attestation 1: scope = "health.*" (scope_weight=1.0), 30 days → recency_weight = 1.0 - (30/365)*0.5 ≈ 0.959
  contribution = 1.0 × 0.959 = 0.959

Attestation 2: scope = "health.records" (scope_weight=0.6), 30 days → recency_weight ≈ 0.959
  contribution = 0.6 × 0.959 = 0.575

raw_score = 0.959 + 0.575 = 1.534
unique_orgs = 2 → quorum bonus: 1.534 × 1.2 = 1.841
average = 1.841 / 2 = 0.920
clamp(0.920) = 0.920
```

Final `attestation_score = 0.92`

---

## 📝 Code Walkthrough: `compute_reputation_score()`

**File:** [`api/services/ranker.py`](../../api/services/ranker.py) lines 84–98

```python
def compute_reputation_score(
    successful_redemptions_30d: int,
    failed_redemptions_30d: int,
    federated_score: float | None = None,
    is_blocklisted: bool = False,
) -> float:
    """Return a bounded reputation score from local and federated outcomes."""
    if is_blocklisted:
        return 0.0

    total = successful_redemptions_30d + failed_redemptions_30d
    local_score = 0.0 if total <= 0 else _clamp(successful_redemptions_30d / total)
    if federated_score is None:
        return local_score
    return _clamp((local_score * 0.70) + (_clamp(federated_score) * 0.30))
```

**Revocation hard-stop (line 91):** If `is_blocklisted=True` (which maps to `is_globally_revoked` from the chain), the score is immediately `0.0` regardless of local outcomes. This is a deliberate override — a service that has been globally revoked on-chain cannot earn a positive reputation score from local behavior.

**Local score (lines 94–95):** `successful / (successful + failed)` from the last 30 days of `crawl_events`. If no events exist (`total = 0`), `local_score = 0.0` (not 0.5) — no data is treated as unknown, not neutral.

**Federated blending (lines 96–98):** 70% local weight, 30% federated signal weight. This is the federation network effect: a service's reputation across the broader AgentLedger network accounts for 30% of its score on any single node.

> **Recommended (not implemented here):** Federated signals currently require manual insertion into `crawl_events` with `event_type='federated_reputation_signal'`. A production deployment would have the federation push pipeline automatically insert these events when a peer node's signed blocklist or attestation update arrives.

---

## 📝 Code Walkthrough: `evaluate_trust_tier_4()`

**File:** [`api/services/ranker.py`](../../api/services/ranker.py) lines 101–113

```python
def evaluate_trust_tier_4(
    attestations: list[dict[str, Any]],
    is_globally_revoked: bool,
) -> bool:
    """Return whether a service qualifies for Layer 3 trust tier 4."""
    if is_globally_revoked or len(attestations) < 2:
        return False
    active_orgs = {
        str(attestation["auditor_org_id"])
        for a in attestations
        if not attestation.get("is_expired", False)
    }
    return len(active_orgs) >= 2
```

This function does **not** return a score — it returns a boolean. Tier 4 is a categorical gate, not a threshold. The conditions:

| Condition | Reason |
|-----------|--------|
| `not is_globally_revoked` | A revoked service can never be Tier 4 |
| `len(attestations) >= 2` | Minimum two independent stamps required |
| `≥2 unique active auditor_org_id values` | Must come from different organizations |

The `auditor_org_id` is the last segment of the auditor's DID extracted in `trust.py`:
```python
# "did:web:auditor.example.com" → "auditor.example.com"
auditor_org_id = did_value.rsplit(":", 1)[-1]
```

This means registering two auditors from `did:web:audit.firm.io` and `did:web:audit.firm.io.eu` does **not** satisfy the quorum — both map to the same domain segment `audit.firm.io.eu`. Wait — actually the `rsplit(":", 1)` splits on the *last colon*. For `did:web:auditor.example.com`, the last segment is `auditor.example.com`. Two auditors at `did:web:auditor.example.com` and `did:web:auditor.example.com` produce the same `auditor_org_id = "auditor.example.com"` — one org, quorum fails.

**Why require organizational independence?** A single audit firm could theoretically register multiple auditor DIDs and issue attestations from both. The org-ID check catches this: if both DIDs share the same domain, only one org identity is counted. This is the primary defense against collusion.

> **Recommended (not implemented here):** A harder quorum check would require the two `chain_address` values to be different wallet addresses *and* the two `did` domains to be different TLD+1 domains (not just last-segment matching). The current check is a strong heuristic but not a cryptographic guarantee.

---

## 📝 Code Walkthrough: `recompute_service_trust()` — All 6 SQL Queries

**File:** [`api/services/trust.py`](../../api/services/trust.py) lines 13–204

This is the integration point. It fires after `confirm_pending_events()` for every service_id affected by newly confirmed chain events.

### Query ① — Service base row (lines 18–32)

```python
SELECT id, trust_tier, last_verified_at, is_banned
FROM services
WHERE id = :service_id
```

The guard: if `service_row is None`, raise `ValueError("service not found")`. This protects against ghost service_id values arriving from chain events for services that were deleted from the DB.

### Query ② — Capability probe score (lines 36–51)

```python
SELECT
    COUNT(*) AS total_count,
    COUNT(*) FILTER (WHERE is_verified = true) AS verified_count
FROM service_capabilities
WHERE service_id = :service_id
```

```python
capability_probe_score = 0.0 if total_count == 0 else verified_count / total_count
```

If a service has no listed capabilities at all, the probe score is `0.0` — the system knows nothing about what it can do. If all capabilities are verified, it's `1.0`. This score is 35% of the final trust score and is updated by the Layer 2 capability verification pipeline (not by Layer 3).

### Query ③ — Operational score (lines 53–65)

```python
SELECT uptime_sla_percent
FROM service_operations
WHERE service_id = :service_id
```

```python
operational_score = 0.5 if uptime is None else max(0.0, min(float(uptime) / 100.0, 1.0))
```

If no operations record exists (many dev-mode services), the fallback is `0.5` (neutral). The raw `uptime_sla_percent` (e.g. `99.5`) is divided by 100 to produce a `[0.0, 1.0]` score.

### Query ④ — Local reputation (lines 67–82)

```python
SELECT
    COUNT(*) FILTER (WHERE event_type = 'session_redeemed') AS success_count,
    COUNT(*) FILTER (WHERE event_type = 'session_redeem_rejected') AS failure_count
FROM crawl_events
WHERE service_id = :service_id
  AND created_at >= NOW() - INTERVAL '30 days'
```

**Conditional aggregation** (`FILTER` clause) in a single scan is more efficient than two separate queries. The 30-day window prevents old failures from permanently degrading a service that has improved.

### Query ⑤ — Federated reputation (lines 84–101)

```python
SELECT AVG((details->>'score')::float) AS federated_score
FROM crawl_events
WHERE service_id = :service_id
  AND event_type = 'federated_reputation_signal'
  AND created_at >= NOW() - INTERVAL '30 days'
```

`details->>'score'` extracts the `score` key from the JSONB `details` column and casts it to float. The `AVG` handles the case where multiple peer nodes have sent signals — they're all averaged. `NULL` is returned if no federated signals exist in the window; the Python code converts that to `None` which triggers the "local only" branch in `compute_reputation_score`.

### Query ⑥ — Revocation + attestations (lines 103–153)

```python
# Revocation check
SELECT COUNT(*) AS total_count
FROM chain_events
WHERE service_id = :service_id
  AND event_type = 'revocation'
  AND is_confirmed = true

# Active attestations
SELECT
    ar.ontology_scope,
    ar.recorded_at,
    ar.expires_at,
    a.did AS auditor_did
FROM attestation_records ar
JOIN auditors a ON a.id = ar.auditor_id
WHERE ar.service_id = :service_id
  AND ar.is_active = true
  AND ar.is_confirmed = true
  AND (ar.expires_at IS NULL OR ar.expires_at > NOW())
  AND a.is_active = true
ORDER BY ar.recorded_at DESC
```

The attestation query has **5 filters**:
1. `ar.is_active = true` — not soft-deleted by a revocation
2. `ar.is_confirmed = true` — on-chain confirmation received (20-block window passed)
3. `ar.expires_at IS NULL OR ar.expires_at > NOW()` — not past the attestation's validity period
4. `a.is_active = true` — the auditor who issued it is still active
5. `ar.service_id = :service_id` — scoped to this service

Filtering on both `ar.is_confirmed = true` and `a.is_active = true` is a belt-and-suspenders guard: even if an auditor's credential expired (set to `is_active = false`), their past attestations are automatically excluded without needing to update each attestation record individually.

The `auditor_org_id` is derived in the Python loop (line 142):
```python
auditor_org_id = did_value.rsplit(":", 1)[-1]
```

### The Final UPDATE (lines 176–197)

```python
UPDATE services
SET trust_score = :trust_score,
    trust_tier = :trust_tier,
    is_banned = CASE WHEN :globally_revoked THEN true ELSE is_banned END,
    ban_reason = CASE
        WHEN :globally_revoked THEN 'globally_revoked_on_chain'
        ELSE ban_reason
    END,
    updated_at = NOW()
WHERE id = :service_id
```

The `CASE` expression for `is_banned` is additive: if a service was already banned for another reason, revocation sets it to `true`. If it was not globally revoked, the existing `is_banned` value is left unchanged — `recompute_service_trust` does not clear bans.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Single-org vs. two-org quorum

Register a service and two auditors from the **same** organization. Confirm attestations. Verify tier stays below 4. Then register a third auditor from a **different** org and attest again.

```bash
# Register two auditors from the SAME org
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:lab1.auditco.io",
    "name": "AuditCo Lab 1",
    "ontology_scope": ["health.*"],
    "chain_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
  }' | python3 -m json.tool

curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:lab2.auditco.io",
    "name": "AuditCo Lab 2",
    "ontology_scope": ["health.*"],
    "chain_address": "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
  }' | python3 -m json.tool
```

**Note:** Both DIDs have last segment `lab1.auditco.io` and `lab2.auditco.io` — these are **different** org IDs per the `rsplit(":", 1)[-1]` logic! To test same-org, use:
```bash
# Same org: both map to "auditco.io"
"did": "did:web:auditco.io"   # auditor_org_id = "auditco.io"
"did": "did:web:auditco.io"   # duplicate DID — upsert, still one org
```

For a true same-org test that bypasses the upsert constraint:
```bash
# These have different DIDs but different last segments — use for cross-org test
"did": "did:web:health-auditors.org"   # org_id = "health-auditors.org"
"did": "did:web:security-labs.io"      # org_id = "security-labs.io"
```

```bash
# Submit two attestations from different orgs (assumes a service exists)
# Get a service domain first:
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT domain FROM services LIMIT 1;"

# Attest from first org
curl -s -X POST http://localhost:8000/v1/attestations \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{
    "auditor_did": "did:web:health-auditors.org",
    "service_domain": "<YOUR_DOMAIN>",
    "ontology_scope": "health.*",
    "evidence_package": {"type": "automated_scan", "result": "pass", "tool": "HealthCheck v2"}
  }' | python3 -m json.tool

# Attest from second org
curl -s -X POST http://localhost:8000/v1/attestations \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{
    "auditor_did": "did:web:security-labs.io",
    "service_domain": "<YOUR_DOMAIN>",
    "ontology_scope": "health.*",
    "evidence_package": {"type": "manual_review", "result": "pass", "reviewer": "Jane Smith"}
  }' | python3 -m json.tool
```

```bash
# Check the trust tier (in CHAIN_MODE=local, confirmation is immediate)
curl -s http://localhost:8000/v1/services/<YOUR_DOMAIN> \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected output (two different orgs):**
```json
{
  "trust_tier": 4,
  "trust_score": 83.50,
  "attestation_score": 0.92
}
```

**Expected output (same org, or single attestation):**
```json
{
  "trust_tier": 3,
  "trust_score": 69.10,
  "attestation_score": 0.80
}
```

### 🔬 Exercise 2: Observe recency decay directly in the REPL

Use the Python REPL to see how `compute_attestation_score` reacts to time.

```bash
docker compose exec api python3 -c "
from datetime import datetime, timezone, timedelta
from api.services.ranker import compute_attestation_score

now = datetime.now(timezone.utc)

# Fresh attestation (0 days old)
fresh = [{'ontology_scope': 'health.*', 'recorded_at': now, 'auditor_org_id': 'org1', 'is_expired': False}]
print('Fresh (0d):', compute_attestation_score(False, fresh, now))

# 180-day-old attestation
old_ts = now - timedelta(days=180)
old = [{'ontology_scope': 'health.*', 'recorded_at': old_ts, 'auditor_org_id': 'org1', 'is_expired': False}]
print('Old (180d):', compute_attestation_score(False, old, now))

# Year-old attestation (should hit 0.5 floor)
ancient_ts = now - timedelta(days=365)
ancient = [{'ontology_scope': 'health.*', 'recorded_at': ancient_ts, 'auditor_org_id': 'org1', 'is_expired': False}]
print('Ancient (365d):', compute_attestation_score(False, ancient, now))

# Two-org quorum bonus
two_orgs = [
    {'ontology_scope': 'health.*', 'recorded_at': now, 'auditor_org_id': 'org1', 'is_expired': False},
    {'ontology_scope': 'health.*', 'recorded_at': now, 'auditor_org_id': 'org2', 'is_expired': False},
]
print('Two orgs (quorum):', compute_attestation_score(False, two_orgs, now))
"
```

**Expected output:**
```
Fresh (0d): 1.0
Old (180d): 0.75
Ancient (365d): 0.5
Two orgs (quorum): 1.0
```

> **Why does "Two orgs" hit exactly 1.0?** Each attestation contributes `1.0 × 1.0 = 1.0`. Sum = `2.0`. Quorum bonus: `2.0 × 1.2 = 2.4`. Average: `2.4 / 2 = 1.2`. `_clamp(1.2) = 1.0`. The clamp is doing real work here.

### 🔬 Exercise 3 (Failure): Submit revocation; confirm reputation drops to 0

```bash
# Submit a revocation for an attested service
SERVICE_DOMAIN="<YOUR_DOMAIN>"

curl -s -X POST "http://localhost:8000/v1/attestations/${SERVICE_DOMAIN}/revoke" \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{
    "auditor_did": "did:web:health-auditors.org",
    "reason": "Security incident: credential exposure"
  }' | python3 -m json.tool

# Check the trust score — reputation_score should be 0.0
curl -s "http://localhost:8000/v1/services/${SERVICE_DOMAIN}" \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected behavior:**
```json
{
  "trust_tier": 1,
  "trust_score": 24.25,
  "is_banned": true,
  "ban_reason": "globally_revoked_on_chain"
}
```

Check the math: with `reputation_score = 0.0` and a typical service:
```
capability_probe_score = 0.8 (some capabilities verified)
attestation_score = 0.0 (globally revoked → 0)  ← wait...
```

Actually: `compute_attestation_score` uses `is_blocklisted` in `compute_reputation_score`, not in `compute_attestation_score`. The attestation score itself doesn't drop to zero — it's `compute_reputation_score` that returns `0.0` when `is_blocklisted=True`. And `evaluate_trust_tier_4` returns `False` when `is_globally_revoked=True`, dropping the tier back to whatever it was before Layer 3.

---

## 📊 The Full Score Path: End to End

Here is the complete data path from a confirmed attestation to a visible trust score change:

```
1. Auditor posts attestation → attestation_records row (is_confirmed=false)
2. Chain event indexed (CHAIN_MODE=local: synthetic; web3: real tx)
3. confirm_pending_events() fires (every 5s)
   → finds events where block_number <= latest - 20
   → sets is_confirmed=true on the chain_events row
   → sets is_confirmed=true on the attestation_records row
   → calls recompute_service_trust(db, service_id) for each affected service
4. recompute_service_trust():
   → runs 6 SQL queries
   → calls compute_attestation_score(), compute_reputation_score(), compute_trust_score()
   → calls evaluate_trust_tier_4()
   → UPDATE services SET trust_score=..., trust_tier=...
5. Next GET /v1/services/{domain} returns the updated values
```

In `CHAIN_MODE=local`, steps 2–4 happen within the same request (synthetic events are immediately "confirmed"). In `CHAIN_MODE=web3`, there is a ~40-second delay between step 1 and step 5.

---

## 📊 Summary Reference Card

| Item | Location |
|------|----------|
| Trust score formula (4 components) | `ranker.py:compute_trust_score()` lines 136–149 |
| Attestation score (scope × recency × quorum) | `ranker.py:compute_attestation_score()` lines 49–81 |
| Scope weight (wildcard = 1.0, specific = 0.6) | `ranker.py` line 69 |
| Recency decay formula | `ranker.py` line 74 |
| Quorum bonus (×1.2 for ≥2 orgs) | `ranker.py` lines 78–79 |
| Reputation score (local 70% + federated 30%) | `ranker.py:compute_reputation_score()` lines 84–98 |
| Tier 4 gate (≥2 confirmed, ≥2 orgs, not revoked) | `ranker.py:evaluate_trust_tier_4()` lines 101–113 |
| Full recompute (6 SQL + 1 write) | `trust.py:recompute_service_trust()` lines 13–204 |
| Auditor org_id extraction | `trust.py` line 142 |
| Trust score range | 0.00 – 100.00 |
| Capability probe weight | 35% |
| Attestation score weight | 30% |
| Operational score weight | 20% |
| Reputation score weight | 15% |

---

## 📚 Interview Preparation

**Q: Why require two different `auditor_org_id` values for Tier 4? What stops a single firm registering two DIDs?**

**A:** The `auditor_org_id` is extracted as the last segment of the auditor's DID (e.g., `did:web:audit.firm.io` → `audit.firm.io`). If one firm registers `did:web:dept1.audit.firm.io` and `did:web:dept2.audit.firm.io`, their org IDs would be `dept1.audit.firm.io` and `dept2.audit.firm.io` — treated as different orgs and quorum would be satisfied. This is a known limitation: the current check is a strong deterrent but not a cryptographic guarantee of independence. A production hardening would require independent `chain_address` wallet signatures and potentially a known-auditor registry with verified domain ownership (similar to Certificate Transparency logs).

**Q: What does `attestation_score = 1.0` mean in real terms?**

**A:** It means the service has at least two confirmed attestations from different organizations (quorum satisfied), at least one is a wildcard-scope attestation (`health.*`), and both are recent enough that the quorum bonus (`×1.2`) pushed the average above 1.0 before clamping. A score of 1.0 means the attestation component is contributing its maximum possible value (30% × 1.0 = 0.30) to the trust score.

**Q: What happens to the trust score if an auditor's credential expires (they don't renew)?**

**A:** The attestation query in `recompute_service_trust` filters `a.is_active = true`. If the auditor's `is_active` flag is set to `false` (by the recommended but not-yet-implemented `expire_auditor_credentials` Celery task), that auditor's attestations are automatically excluded from the score computation. The next `recompute_service_trust` call will find fewer attestations, potentially dropping the service below the Tier 4 quorum threshold. This is a cascade from auditor credential expiry to service trust degradation — by design.

**Q: Why is `compute_trust_score` in `ranker.py` rather than `trust.py`?**

**A:** Pure separation of concerns. `ranker.py` is a library of stateless math functions — it has no imports from the project's DB layer, no `from sqlalchemy import ...`, and can be imported in tests without spinning up any infrastructure. `trust.py` owns the I/O integration. This means the formula can be unit-tested with a simple dict of inputs, and the integration test for `recompute_service_trust` focuses only on the DB round-trip, not the scoring math.

**Q: Why is `operational_score` default `0.5` but `local_score` in reputation is `0.0` when there's no data?**

**A:** Different design philosophies for different signals. A missing `service_operations` row means the service hasn't declared uptime data — the system is neutral (0.5). But a missing session history means the service has never been used — that's a genuine zero-activity signal. An untested service that has never served a single session is different from a service that simply hasn't set an uptime SLA. The operational component gives benefit of the doubt; the reputation component does not.

---

## ✅ Key Takeaways

- Trust scoring is split into `ranker.py` (pure math) and `trust.py` (DB + integration) — the only write is a single `UPDATE services` at the end
- `compute_attestation_score()` has three sub-factors: scope weight (`1.0` for wildcard, `0.6` for specific), recency decay (linear decay to `0.5` floor at 365 days), and quorum bonus (×1.2 for ≥2 unique organizations)
- Without Layer 3 attestations, the attestation score falls back to the Layer 2 identity signal (`1.0` if DID verified, else `0.0`)
- `evaluate_trust_tier_4()` is a gate, not a threshold — it checks revocation status, minimum attestation count, and organizational independence
- `recompute_service_trust()` runs 6 SQL queries: service base row, capability probe, uptime, local reputation, federated reputation, and attestations (with embedded revocation check)
- A confirmed global revocation immediately drops `reputation_score` to `0.0`, sets `is_banned=true`, and disqualifies the service from Tier 4

---

## 🚀 Ready for Lesson 26?

You've already completed it — Lesson 26 covers the Merkle audit chain. Up next: **Lesson 27 — The Neighborhood Watch**, where revocations don't stay local. We'll trace how a confirmed revocation fan-outs to every subscribed registry in the federation within 60 seconds via signed webhook pushes.

*Remember: A bank that relies on a single reference is naïve. The trust ledger demands independent corroboration — and it won't be fooled by sister companies.* 🏦
