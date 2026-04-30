# Lesson 33: The Overstep Detector — Mismatch Detection & Sensitivity Tiers

> **Beginner frame:** Mismatch detection is an overreach alarm. It flags cases where a service asks for more data than its manifest, trust level, or the agent's policy supports.

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_mismatch.py`, `db/migrations/versions/005_layer4_context.py`  
**Prerequisites:** Lesson 32  
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

Imagine a pharmacist who asks you for your social security number to fill a prescription. They declared on their registration form that they need "name, date of birth, and insurance ID." An SSN is nowhere on that list. This is an **overstep** — requesting data beyond what was declared in the service manifest.

Layer 4's mismatch detection catches exactly this. When a service requests a field it never declared, `detect_mismatch()` fires before the profile or trust checks even run. The event is recorded in an append-only violation log. Critical overstepping — touching high-sensitivity fields without declaration — can escalate to a Layer 3 trust revocation.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain how `detect_mismatch()` determines over-requested fields using set difference
- Describe the two severity levels (`warning` vs `critical`) and what triggers each
- Read the `_FIELD_SENSITIVITY_TIERS` map and predict a field's sensitivity tier
- Trace a mismatch event from detection through `_record_mismatch_event()` to the database
- Describe how `resolve_mismatch()` optionally escalates to a Layer 3 revocation
- Explain why `context_mismatch_events` is append-only and never deleted

---

## The Mismatch Check: Set Difference

The detection logic is deliberately simple (`context_mismatch.py:78–99`):

```python
def detect_mismatch(
    declared_required: list[str],
    declared_optional: list[str],
    requested_fields: list[str],
) -> MismatchResult:
    declared = set(declared_required) | set(declared_optional)
    over_requested = [f for f in requested_fields if f not in declared]

    if not over_requested:
        return MismatchResult(detected=False, over_requested_fields=[], severity="")

    severity = "critical" if any(
        get_sensitivity_tier(f) >= 3 for f in over_requested
    ) else "warning"

    return MismatchResult(
        detected=True,
        over_requested_fields=over_requested,
        severity=severity,
    )
```

**The invariant:** `requested_fields - (declared_required ∪ declared_optional) = over_requested_fields`. Any field in the request that is not in the service's manifest declaration is a violation.

---

## The Sensitivity Tier Map

Sensitivity tiers control two things: the severity of a mismatch, and the trust threshold required for disclosure. They are defined statically in `context_mismatch.py:43–61`:

```python
_FIELD_SENSITIVITY_TIERS = {
    "user.ssn": 4,
    "user.full_medical_history": 4,
    "user.insurance_id": 3,
    "user.dob": 3,
    "user.government_id": 3,
}
```

For fields not in this exact map, `get_sensitivity_tier()` falls back to keyword matching:

```python
# Approximate line 70-75
if "ssn" in field_name:         return 4
if "medical" in field_name:     return 4
if "insurance" in field_name:   return 3
if "dob" in field_name:         return 3
# default:
return 1
```

**Conservative default:** Unknown fields default to tier 1 (low sensitivity). This is intentional — it is better to occasionally under-classify than to block legitimate fields. Operators can add to the map for their domain.

### Tier reference table

| Tier | Example fields | Mismatch severity if over-requested | Required trust tier |
|------|---------------|-----------------------------------|-------------------|
| 1 | `user.name`, `user.email` | `warning` | 2 |
| 2 | `user.phone`, `user.address` | `warning` | 2 |
| 3 | `user.dob`, `user.insurance_id`, `user.government_id` | **`critical`** | 3 |
| 4 | `user.ssn`, `user.full_medical_history` | **`critical`** | 4 |

---

## The Mismatch Record

When a mismatch is detected, `_record_mismatch_event()` writes to `context_mismatch_events`:

```sql
INSERT INTO context_mismatch_events (
    service_id, agent_did,
    declared_fields, requested_fields, over_requested_fields,
    severity, resolved
) VALUES (...)
```

This table is **append-only**. Rows are never updated or deleted. The `resolved` column starts as `false` and is set to `true` by `resolve_mismatch()`, but the original violation record remains permanently. This is the audit property: you can always prove that a violation occurred, even after resolution.

### What happens to the match request after a mismatch?

A mismatch does not abort the match. The matching engine records the event and **continues** evaluating the non-over-requested fields. This is the correct behaviour: a service that requests one extra field should not necessarily be blocked from receiving the fields it legitimately declared. However, the over-requested fields are automatically excluded from any classification.

---

## Resolution and Escalation

The admin endpoint `POST /v1/context/mismatches/{id}/resolve` resolves a mismatch:

```python
async def resolve_mismatch(db, mismatch_id, escalate_to_trust, resolution_note):
    if escalate_to_trust:
        auditor = await _select_active_auditor(db)
        await attestation.submit_revocation(
            db=db, service_id=mismatch.service_id, auditor_did=auditor["did"], ...
        )
    await db.execute(
        "UPDATE context_mismatch_events SET resolved=true, resolution_note=:note WHERE id=:id",
        ...
    )
```

When `escalate_to_trust=true`, the resolution calls directly into Layer 3's `submit_revocation()`. This writes a revocation event to the blockchain and drops the service's `trust_tier`. The mismatch detection system is therefore a **trust accountability loop**: repeated over-requesting causes a service to lose its Layer 3 attestation.

---

## Code Walkthrough: `context_mismatch.py`

| Function | Lines | Purpose |
|----------|-------|---------|
| `get_sensitivity_tier(field)` | ~64–75 | Maps field name → tier 1–4 |
| `detect_mismatch(declared, requested)` | ~78–99 | Returns MismatchResult with over-requested set and severity |
| `_record_mismatch_event(db, ...)` | ~153–206 | INSERT to context_mismatch_events |
| `list_mismatches(db, ...)` | ~259–310 | Admin: paginated list with filters |
| `resolve_mismatch(db, ...)` | ~372–448 | Mark resolved; optionally escalate |

---

## Exercise 1 — Observe a Mismatch

To trigger a mismatch detection, send a match request where `requested_fields` includes a field not in the service manifest. You'll need a registered service and agent DID.

First, check what context fields your service declares:

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT field_name, is_required FROM service_context_requirements WHERE service_id = '<your-service-id>';"
```

Then in your match request, add a field that is NOT in that list (e.g., `user.ssn`). After the match, query the mismatch log:

```bash
curl -s "http://localhost:8000/v1/context/mismatches" \
  -H "X-API-Key: dev-local-admin" | python -m json.tool
```

**Expected:** A new entry with `severity='critical'` (if you used `user.ssn`) or `severity='warning'` for a tier-1/2 field.

---

## Exercise 2 — Test `get_sensitivity_tier`

The sensitivity tier function can be tested directly in a Python REPL:

```bash
cd C:\Projects\AgentLedger
python -c "
from api.services.context_mismatch import get_sensitivity_tier
fields = ['user.name', 'user.email', 'user.dob', 'user.ssn',
          'user.insurance_id', 'user.custom_medical_note', 'user.unknown_field']
for f in fields:
    print(f'{f:40s} -> tier {get_sensitivity_tier(f)}')
"
```

**Expected output:**
```
user.name                                -> tier 1
user.email                               -> tier 1
user.dob                                 -> tier 3
user.ssn                                 -> tier 4
user.insurance_id                        -> tier 3
user.custom_medical_note                 -> tier 4  (keyword: 'medical')
user.unknown_field                       -> tier 1  (default)
```

---

## Exercise 3 — Failure Case: Escalation Path

Trace what happens when `escalate_to_trust=true` in `resolve_mismatch()`:

1. Read `context_mismatch.py` lines 372–448
2. Find where `attestation.submit_revocation()` is called
3. Identify what auditor is selected (hint: `_select_active_auditor()`)
4. Answer: What happens if there are no active auditors registered? Trace the failure path.

**Expected:** If no active auditor exists, `_select_active_auditor()` raises 422. This means escalation requires at least one registered auditor — a Layer 3 precondition on a Layer 4 operation.

---

## Best Practices

**Log mismatches even for fields the profile would deny.** The mismatch event is about the service's manifest declaration, not about the agent's profile. A service requesting an undeclared field is suspicious regardless of what the profile would have done with it. Record everything.

**Recommended (not implemented here):** An automatic escalation threshold — if a service accumulates N critical mismatches in 24 hours, trigger escalation automatically rather than requiring an admin to manually resolve each one.

---

## Interview Q&A

**Q: Does a mismatch prevent the match from completing?**  
A: No. The over-requested fields are excluded, the event is recorded, and the match continues for the legitimately declared fields. Blocking the entire match would break legitimate workflows for one bad field.

**Q: Why is the mismatch table append-only?**  
A: Violations are forensic evidence. If a service is later disputed, the full history of over-requesting must be provable. Allowing updates would let an operator hide a violation after-the-fact.

**Q: How does severity='critical' differ from severity='warning' in practice?**  
A: In the current implementation, both are recorded identically. The severity field enables the admin to filter `GET /v1/context/mismatches?severity=critical` to prioritise review. The escalation decision is still manual. A future version could auto-escalate on critical mismatches.

---

## Key Takeaways

- Mismatch = `requested_fields - (declared_required ∪ declared_optional)`
- `severity='critical'` when any over-requested field has `sensitivity_tier >= 3`
- `get_sensitivity_tier()` uses an exact map first, then keyword fallback, default tier 1
- Mismatch events are append-only — `resolved=true` means resolved but not erased
- Escalation to Layer 3 (`submit_revocation`) is one function call away from a mismatch
- The mismatch check runs before profile evaluation — manifest compliance is checked first

---

## Next Lesson

**Lesson 34 — The Gatekeeper: The Matching Engine** traces the full 8-step `match_context_request()` flow — from rate limit check through session assertion verification, mismatch detection, trust gating, profile evaluation, and commitment generation.
