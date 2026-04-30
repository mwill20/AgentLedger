# Lesson 35: The Trust Ladder — Trust Gates & Sensitivity Enforcement

> **Beginner frame:** Trust gates are vault doors for data. The more sensitive the field, the more trust a service needs before AgentLedger allows disclosure.

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_matcher.py`, `api/services/context_mismatch.py`  
**Prerequisites:** Lessons 33, 34  
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A bank vault has multiple access levels. A teller can open the cash drawer. A manager can open the main vault. Only senior security staff can access the safe deposit room. Each level requires a higher credential — and presenting a teller's badge doesn't get you into the safe deposit room no matter how politely you ask.

Layer 4's trust gate works the same way. A service's trust tier is its credential level. A field's sensitivity tier determines which vault it belongs to. The gate checks: **does this service's credential level match the vault containing this field?** If not, the field stays locked.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Map sensitivity tiers to required trust tiers (the "ladder")
- Explain why required-field failures raise 403 while optional-field failures are silent
- Trace the trust gate check in `context_matcher.py`
- Explain how trust tier is sourced from Layer 3 at match time
- Describe the re-verification at disclose time and why it's necessary
- Identify the edge case where trust drops between match and disclose

---

## The Sensitivity-to-Trust Mapping

This is the ladder. Field sensitivity tier → minimum service trust tier required:

```
Sensitivity Tier 4 (e.g., user.ssn, user.full_medical_history)
    → Service must have trust_tier >= 4
    → Layer 3: two independent org attestations + not globally revoked

Sensitivity Tier 3 (e.g., user.dob, user.insurance_id, user.government_id)
    → Service must have trust_tier >= 3
    → Layer 3: at least one confirmed attestation

Sensitivity Tier 1 or 2 (e.g., user.name, user.email, user.phone)
    → Service must have trust_tier >= 2
    → Layer 3: minimum registered service standard
```

This mapping is defined in `context_matcher.py` (approximately lines 255–297). There is no configuration — it is hardcoded as an invariant of the data model. A tier-4 field cannot flow to a tier-2 service by any configuration path.

---

## The Gate in Code

```python
def _check_trust_threshold(field: str, service: ServiceContext, is_required: bool):
    sensitivity = service.field_sensitivity_tiers.get(field, 1)
    required_trust_tier = {4: 4, 3: 3}.get(sensitivity, 2)

    if service.trust_tier >= required_trust_tier:
        return True  # field clears the gate

    if is_required:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"service trust_tier {service.trust_tier} insufficient "
                   f"for required field {field} (requires tier {required_trust_tier})"
        )
    return False  # optional field: silent withhold
```

The `field_sensitivity_tiers` dict was precomputed in Step 3 (load service context). This is why the precomputation matters: the gate is called for every field, so computing the tier at gate time would call `get_sensitivity_tier()` in a hot loop.

---

## Required vs. Optional: The Asymmetry

The most important design decision in the trust gate:

**Required field + insufficient trust = 403 Forbidden.**  
**Optional field + insufficient trust = silent withhold.**

This maps to intent. When a service marks a field as required in its manifest, it is saying "I cannot serve this agent without this field." If the service cannot be trusted with that field, the honest answer is "you cannot serve this agent" — a 403.

When a field is optional, the service is saying "I'd like this if possible." Withholding it silently degrades the experience but does not break the contract.

This asymmetry forces service authors to think carefully about `required` vs. `optional` in their manifests. Marking everything as required is a bad practice: it causes 403s for any high-sensitivity field the service cannot be trusted with, blocking the entire interaction.

---

## Where Trust Tier Comes From

The trust tier used in the gate is read from the `services` table at match time:

```sql
SELECT s.trust_tier, s.trust_score, s.is_active, s.is_banned
FROM services s
WHERE s.id = :service_id
```

This is the **same trust_tier** computed and maintained by Layer 3's `recompute_service_trust()`. Layer 4 reads Layer 3's output — it does not recompute trust itself. The trust system is thus a single source of truth: Layer 3 maintains it, Layer 4 enforces it.

**Important:** The trust tier in the services table reflects the state at the time of the last `recompute_service_trust()` call. There is a brief window (seconds to minutes) between a revocation being confirmed on-chain and the trust recompute running. During this window, the match might proceed with a stale trust tier. This is a known tradeoff between consistency and performance.

---

## Re-verification at Disclose Time

The disclose phase repeats the trust check:

```python
# In context_disclosure.disclose_context() ~line 545:
current_service = await _load_service_trust(db, request.service_id)
for field in committed_fields:
    sensitivity = get_sensitivity_tier(field)
    required_tier = {4: 4, 3: 3}.get(sensitivity, 2)
    if current_service.trust_tier < required_tier:
        raise HTTPException(status_code=403, detail=f"trust dropped for {field}")
```

If the service's trust tier dropped between match and disclose (e.g., a revocation was confirmed on-chain and recompute ran), the disclose raises 403. The commitment exists in the database but the nonce is never released.

This is the safety net for the trust-recompute latency window. The match might have cleared a field at trust tier 3. By disclose time, that tier might have dropped to 2. The disclose gate catches it.

---

## A Trust Ladder Scenario

**Setup:**
- Service trust_tier = 2
- Agent requests: `user.name` (tier 1), `user.email` (tier 1), `user.dob` (tier 3), `user.ssn` (tier 4)
- All fields are marked `required` in the manifest

**Trust gate evaluation:**
```
user.name  → sensitivity 1 → requires tier 2 → service tier 2 → PASS
user.email → sensitivity 1 → requires tier 2 → service tier 2 → PASS
user.dob   → sensitivity 3 → requires tier 3 → service tier 2 → FAIL → 403
user.ssn   → sensitivity 4 → requires tier 4 → service tier 2 → FAIL → 403 (never reached)
```

Result: The match returns 403 on `user.dob` (first required-field failure). `user.ssn` is never evaluated.

**If `user.dob` and `user.ssn` were optional:**
```
user.name  → permitted
user.email → permitted
user.dob   → withheld (trust insufficient, optional)
user.ssn   → withheld (trust insufficient, optional)
```

Match succeeds with `permitted=[user.name, user.email]`, `withheld=[user.dob, user.ssn]`.

---

## Exercise 1 — Trigger a Trust Gate 403

Create a profile that permits `user.dob` for all services. Then make a match request against a service with trust_tier=2 where `user.dob` is declared as required.

First, check a service's trust tier:
```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, name, trust_tier FROM services LIMIT 5;"
```

Then include `user.dob` in a required-field match request against a tier-2 service and observe the 403.

---

## Exercise 2 — Compare Required vs. Optional

Take the same match request. In your service manifest (or test setup), toggle `user.dob` from required to optional. Confirm the match now returns 200 with `user.dob` in `withheld_fields` instead of raising 403.

---

## Exercise 3 — Inspect Trust Tier in the Services Table

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, name, trust_tier, trust_score, attestation_score FROM services ORDER BY trust_tier DESC LIMIT 10;"
```

Identify which services have trust_tier >= 3. These are the services that can receive `user.dob` and `user.insurance_id`. Are any of the IntegrationTest services at tier 3?

---

## Best Practices

**Design manifests with honest required/optional splits.** A service that marks every field required will 403 on any high-sensitivity field it cannot be trusted with. This punishes agents trying to use the service legitimately. Mark fields optional unless the service genuinely cannot function without them.

**Recommended (not implemented here):** A service capability score that factors in how often the service hits 403 due to trust-gate failures. A service that consistently fails the gate is either declaring fields incorrectly or has a trust score that needs improving.

---

## Interview Q&A

**Q: What prevents a service from simply setting its own trust_tier to 4 in the database?**  
A: Trust tier is computed by `recompute_service_trust()` in Layer 3 — it is derived from confirmed on-chain attestations, not set directly. The `services.trust_tier` column is written only by that function. No API endpoint allows a service to set its own trust tier.

**Q: A service had trust_tier=3 at match time. A revocation is confirmed 30 seconds later, dropping it to tier 2. The agent calls disclose. What happens?**  
A: The disclose phase re-queries the service's current trust tier. It finds tier 2. For any committed field requiring tier 3 (e.g., `user.dob`), the disclose raises 403 and the nonce is not released. The commitment remains in the database but expires after 5 minutes unused.

**Q: Why is there no tier-2-requires-tier-2 explicit mapping?**  
A: Tiers 1 and 2 both require the minimum standard (trust_tier >= 2). Tier 1 fields are low enough sensitivity that the same threshold applies. The map is `{4: 4, 3: 3}` with a default of 2 — any sensitivity not explicitly mapped defaults to the minimum tier.

---

## Key Takeaways

- The ladder: sensitivity tier 4 → trust tier 4; tier 3 → trust tier 3; tier 1/2 → trust tier 2
- Required field failure = 403 Forbidden; optional field failure = silent withhold
- Trust tier is read from Layer 3's `services` table — Layer 4 enforces, Layer 3 maintains
- Disclose phase re-verifies trust — trust can drop between match and disclose
- The asymmetry (required vs. optional) forces correct manifest design

---

## Next Lesson

**Lesson 36 — The Safe Deposit Box: HMAC Commitment Scheme** dives into how `generate_commitment()` and `verify_commitment()` work, why HMAC-SHA256 was chosen over full ZKP for v0.1, and what the 5-minute commitment TTL means for the disclosure flow.
