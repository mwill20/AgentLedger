# Lesson 37: The Key Handoff — Selective Disclosure & Nonce Release

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_disclosure.py` (lines 519–704)  
**Prerequisites:** Lessons 35, 36  
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

You've seen the match phase build a verdict: which fields are permitted (plaintext), withheld (blocked), or committed (HMAC-locked). Now you're at the second half of the interaction.

The disclose phase is the **key handoff**: the agent confirms they want to proceed, the server re-checks that the service still deserves the data, releases the cryptographic nonces for committed fields, and writes an immutable audit record proving exactly what was shared, when, and with whom.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Trace the 6-step `disclose_context()` flow
- Explain why trust is re-checked at disclose time (not just at match time)
- Describe what is written to `context_disclosures` and what is deliberately excluded
- Distinguish the three `disclosure_method` values
- Explain how an agent would use `DisclosurePackage` to give a service field values
- Describe what happens to `context_commitments` rows after nonces are released

---

## The 6-Step Disclose Flow

```
disclose_context(db, redis, request)
│
├─ 1. Load match snapshot    — Redis first, fall back to commitment rows
├─ 2. Load commitment rows   — verified by match_id + agent_did + service_id
├─ 3. Reconstruct snapshot   — rebuild match classification if Redis expired
├─ 4. Re-check service trust — trust could have dropped since match
├─ 5. Release nonces         — UPDATE nonce_released=true for each commitment
└─ 6. Write audit record     — INSERT into context_disclosures (field names only)
```

---

## Step 1 — Load Match Snapshot

```python
cached = await redis.get(f"context:match:{request.match_id}")
snapshot = ContextMatchResponse.model_validate_json(cached) if cached else None
```

The match result was cached in Redis (5-minute TTL) at the end of `match_context_request()`. If it's still there, the disclose phase reads the full classification without any DB query.

**What if the cache expired?** Step 3 reconstructs the snapshot from the `context_commitments` rows. Each commitment row stores `fields_requested`, `fields_permitted`, `fields_withheld`, `fields_committed` — a full snapshot of the match classification. This is why those columns exist: they make the disclose phase Redis-independent.

---

## Step 2 — Load Commitment Rows

```python
rows = await db.execute(
    """
    SELECT id, field_name, commitment_hash, nonce, expires_at, nonce_released
    FROM context_commitments
    WHERE match_id = :match_id
      AND agent_did = :agent_did
      AND service_id = :service_id
      AND nonce_released = false
      AND expires_at > NOW()
    """,
    {"match_id": request.match_id, "agent_did": request.agent_did, "service_id": request.service_id}
)
```

The query enforces:
- `nonce_released = false` — prevents re-disclosure of the same commitment
- `expires_at > NOW()` — prevents disclosure after the 5-minute TTL
- Scoped by `agent_did` + `service_id` — prevents cross-agent or cross-service disclosure

If any `commitment_id` from the request is not found in this result (expired, already released, or wrong scope), the disclose raises 404.

---

## Step 4 — Re-check Trust

```python
current_service = await _load_service_trust_state(db, request.service_id)
for field in committed_fields:
    sensitivity = get_sensitivity_tier(field)
    required_tier = {4: 4, 3: 3}.get(sensitivity, 2)
    if current_service.trust_tier < required_tier:
        raise HTTPException(status_code=403,
            detail=f"service trust insufficient for {field} at disclose time")
```

This is the safety net described in Lesson 35. Trust can drop between match and disclose. A service that was tier 3 at match time might be tier 2 by disclose time if a revocation was confirmed on-chain in the interim. The re-check catches it.

For `permitted_fields` (non-committed), the trust was already enforced at match time. The re-check only applies to `committed_fields` — the higher-sensitivity ones where extra caution is warranted.

---

## Step 5 — Release Nonces

```python
for row in commitment_rows:
    await db.execute(
        """
        UPDATE context_commitments
        SET nonce_released = true,
            nonce_released_at = NOW()
        WHERE id = :id
        """,
        {"id": row["id"]}
    )
released_nonces[row["field_name"]] = row["nonce"]
```

The nonces are now available in `released_nonces`. They will be returned to the caller in the `DisclosurePackage`. Once `nonce_released=true`, the same commitment cannot be disclosed again — step 2's `nonce_released=false` filter will exclude it.

---

## Step 6 — Write the Audit Record

```python
await db.execute(
    """
    INSERT INTO context_disclosures (
        agent_did, service_id, session_assertion_id, ontology_tag,
        fields_requested, fields_disclosed, fields_withheld, fields_committed,
        disclosure_method, trust_score_at_disclosure, trust_tier_at_disclosure,
        profile_id, erased
    ) VALUES (...)
    """,
    {
        "fields_disclosed": permitted_fields,    # list of field NAMES
        "fields_committed": committed_fields,     # list of field NAMES
        # ... NOT the values
        "disclosure_method": _disclosure_method(permitted_fields, committed_fields),
        "trust_score_at_disclosure": current_service.trust_score,
        "trust_tier_at_disclosure": current_service.trust_tier,
        "erased": False,
    }
)
```

**Critical: field values are never written here.** The audit record contains field *names* only. This is the privacy-preserving property that makes Layer 4 GDPR-compatible: you can prove what categories of data were shared without storing the data itself.

### `disclosure_method` values

| Method | Meaning |
|--------|---------|
| `'direct'` | Only plaintext-permitted fields; no committed fields |
| `'committed'` | Only committed fields (nonce-released); no direct |
| `'direct+committed'` | Both plaintext and committed fields in same disclosure |

---

## The DisclosurePackage Response

```json
{
  "disclosure_id": "uuid",
  "permitted_fields": {
    "user.name": "Alice",
    "user.email": "alice@example.com"
  },
  "committed_field_nonces": {
    "user.dob": "a3f9c2...d81e"
  }
}
```

**`permitted_fields`**: field names mapped to plaintext values. The agent passes these directly to the service.

**`committed_field_nonces`**: field names mapped to the released nonces. The agent passes the nonce to the service alongside the field value. The service runs `verify_commitment(commitment_hash, nonce, claimed_value)` to confirm the value matches what was committed.

This is the full handoff: the match response gave the service a `commitment_hash` (the locked box). The disclose response gives the agent the `nonce` (the key). The agent hands the key and the claimed value to the service, and the service can verify the lock.

---

## What Happens to the Service Flow

From the service's perspective:

1. Service sends match request → receives `commitment_id` + `commitment_hash`
2. Service waits for agent to call `/disclose`
3. Agent calls `/disclose` → receives nonce for each committed field
4. Agent passes `(nonce, field_value)` to the service out-of-band
5. Service calls `verify_commitment(commitment_hash, nonce, field_value)` locally
6. If verification passes: the value is authentic and matches what was committed

AgentLedger never transmits field values to services. The service only receives a commitment hash. The field value travels from agent to service through their own channel, with the commitment as the integrity proof.

---

## Exercise 1 — Full Two-Phase Flow

Perform a complete match → disclose sequence using the API:

```bash
# Phase 1: Match
MATCH=$(curl -s -X POST http://localhost:8000/v1/context/match \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d '{
    "agent_did": "did:key:z6MkTestContextAgent",
    "service_id": "<service-uuid>",
    "session_assertion": "<jwt>",
    "requested_fields": ["user.name", "user.email"],
    "field_values": {"user.name": "Alice", "user.email": "alice@example.com"}
  }')
echo $MATCH | python -m json.tool

MATCH_ID=$(echo $MATCH | python -c "import sys,json; print(json.load(sys.stdin)['match_id'])")

# Phase 2: Disclose
curl -s -X POST http://localhost:8000/v1/context/disclose \
  -H "X-API-Key: dev-local-only" -H "Content-Type: application/json" \
  -d "{
    \"match_id\": \"$MATCH_ID\",
    \"agent_did\": \"did:key:z6MkTestContextAgent\",
    \"service_id\": \"<service-uuid>\",
    \"commitment_ids\": []
  }" | python -m json.tool
```

**Expected Phase 1:** `permitted_fields=[user.name, user.email]`, empty `commitment_ids`.  
**Expected Phase 2:** `DisclosurePackage` with `permitted_fields` containing name and email values.

---

## Exercise 2 — Verify Idempotency Rejection

Call `/disclose` twice with the same `match_id`. The second call should fail because `nonce_released=true` after the first call.

**Expected:** 404 on the second disclose — the commitment rows no longer match the query filter `nonce_released=false`.

---

## Exercise 3 — Confirm the Audit Record

After a successful disclose, query the audit table:

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, fields_disclosed, fields_committed, disclosure_method, trust_tier_at_disclosure, erased FROM context_disclosures ORDER BY created_at DESC LIMIT 3;"
```

Confirm: field names are present, no field values, `erased=false`.

---

## Best Practices

**Disclose is a one-way door.** Once nonces are released, they cannot be un-released. If an agent accidentally calls disclose before reviewing the match result, the commitment is spent. Design agent platforms to show users a confirmation screen between match and disclose for high-sensitivity fields.

**Recommended (not implemented here):** A disclose confirmation endpoint — a dry-run that validates the match snapshot and trust state without actually releasing nonces. This gives agent platforms a "preflight check" before the irreversible disclose.

---

## Interview Q&A

**Q: Why does the audit record write trust_score and trust_tier at disclosure time, not at match time?**  
A: Because the re-check at disclose time is what matters for compliance. If trust dropped between match and disclose and the disclose still succeeded (for permitted fields), you want to record the trust state that was actually in effect at the moment of release.

**Q: Can the service call `/disclose` on behalf of the agent?**  
A: No. The disclose request requires `agent_did` in the request body and the API key belongs to the agent's platform. A service cannot impersonate an agent's DID. The disclose is explicitly an agent-initiated action.

**Q: What prevents a service from storing the commitment hash and later brute-forcing the nonce?**  
A: The nonce is 256-bit random — `2^256` possibilities. With HMAC-SHA256, there is no shortcut. The commitment is computationally binding.

---

## Key Takeaways

- 6-step flow: load snapshot → load commitments → reconstruct → re-check trust → release nonces → audit
- Redis snapshot is preferred; commitment-row snapshot is the fallback
- `nonce_released=true` after disclose — the same commitment cannot be disclosed twice
- Audit record stores field names only — never values
- `DisclosurePackage` gives the agent nonces to pass to the service for out-of-band verification
- `disclosure_method` = `'direct'` | `'committed'` | `'direct+committed'`

---

## Next Lesson

**Lesson 38 — The Paper Trail: Disclosure Audit History & GDPR Erasure** covers `list_disclosures()`, `revoke_disclosure()`, and what "GDPR right to erasure" means when the audit trail must also be preserved for compliance.
