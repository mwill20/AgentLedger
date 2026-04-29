# Lesson 38: The Paper Trail — Disclosure Audit History & GDPR Erasure

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_disclosure.py` (lines 592–704), `api/routers/context.py`  
**Prerequisites:** Lesson 37  
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

Every bank transaction generates a permanent ledger entry. Even if a transaction is reversed, the original entry and the reversal both remain. You can always prove what happened. But the *details* of certain entries — say, what you purchased — can be redacted for privacy while the *fact* of the transaction is preserved.

Layer 4's audit trail works exactly this way. Every context disclosure writes an immutable record to `context_disclosures`. GDPR erasure (`revoke_disclosure()`) nulls the field-name arrays — redacting the details — but the row itself remains. The fact of the disclosure is preserved. The specifics are erased.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Query the disclosure audit trail with filters: agent_did, service_id, date range
- Explain the pagination implementation using a window function
- Describe what `revoke_disclosure()` does and does not delete
- Explain the GDPR tension: right to erasure vs. audit integrity requirement
- Trace how `erased=true` rows appear in the compliance PDF
- Identify what a regulator can prove from an erased disclosure record

---

## `list_disclosures()` — The Audit Query

```python
async def list_disclosures(
    db, agent_did, service_id=None, from_date=None, to_date=None,
    limit=50, offset=0
):
```

The query is paginated using a window function so both the current page and total count are returned in one round-trip:

```sql
SELECT
    id, agent_did, service_id, ontology_tag,
    fields_requested, fields_disclosed, fields_withheld, fields_committed,
    disclosure_method, trust_score_at_disclosure, trust_tier_at_disclosure,
    erased, erased_at, created_at,
    COUNT(*) OVER () AS total_count
FROM context_disclosures
WHERE agent_did = :agent_did
  [AND service_id = :service_id]
  [AND created_at >= :from_date]
  [AND created_at <= :to_date]
ORDER BY created_at DESC
LIMIT :limit OFFSET :offset
```

`COUNT(*) OVER ()` is a window function that computes the total count across all matching rows without a second query. This is the same pattern used in Layer 1's service list endpoint.

**What the response includes:**
- `fields_disclosed`: list of field names that were permitted/plaintext
- `fields_committed`: list of field names that were nonce-released
- `fields_withheld`: list of field names that were blocked
- `trust_tier_at_disclosure`: the service's trust tier at the moment nonces were released

**What the response never includes:**
- Field values — these are never stored anywhere in the Layer 4 tables
- The nonce — released once, never stored after `nonce_released_at`

---

## `revoke_disclosure()` — GDPR Right to Erasure

```python
async def revoke_disclosure(db, disclosure_id, agent_did):
    # Load the row — confirm it belongs to this agent
    row = await db.execute(
        "SELECT id, agent_did FROM context_disclosures WHERE id = :id",
        {"id": disclosure_id}
    )
    if row["agent_did"] != agent_did:
        raise HTTPException(403)

    # Erase the field-name arrays (not the row itself)
    await db.execute(
        """
        UPDATE context_disclosures
        SET erased = true,
            erased_at = NOW(),
            fields_requested = '{}',
            fields_disclosed = '{}',
            fields_withheld = '{}',
            fields_committed = '{}'
        WHERE id = :id
        """,
        {"id": disclosure_id}
    )
    await db.commit()
```

**What erasure does:**
- Sets `erased=true` and `erased_at=NOW()`
- Nulls all four field-name arrays to empty arrays `{}`
- The row itself remains

**What erasure does not do:**
- Does not delete the row
- Does not touch `agent_did`, `service_id`, `ontology_tag`, `disclosure_method`, `trust_tier_at_disclosure`, `created_at`

---

## The GDPR Tension

GDPR Article 17 (right to erasure) says: delete personal data on request.  
GDPR Article 5(1)(e) says: keep data no longer than necessary.  
GDPR Recital 65 says: erasure obligations apply unless there are legal grounds for processing (e.g., compliance obligations).

Layer 4 resolves this tension with **partial erasure**:

| Column | After erasure |
|--------|--------------|
| `fields_requested` | `{}` (erased) |
| `fields_disclosed` | `{}` (erased) |
| `fields_withheld` | `{}` (erased) |
| `fields_committed` | `{}` (erased) |
| `erased` | `true` |
| `erased_at` | timestamp |
| `agent_did` | **retained** |
| `service_id` | **retained** |
| `ontology_tag` | **retained** |
| `trust_tier_at_disclosure` | **retained** |
| `created_at` | **retained** |

**What a regulator can still prove after erasure:**
- Agent X interacted with service Y at time T
- The interaction involved a service in ontology domain Z at trust tier N
- The disclosure was erased at time T+N by the agent

**What a regulator cannot prove:**
- Which specific fields were shared

This satisfies the privacy requirement (specific fields erased) while preserving audit integrity (the interaction is provable). The compliance PDF renders erased rows with `[ERASED]` in place of the field names.

---

## The Route Layer

These endpoints are in `api/routers/context.py`:

```python
@router.get("/disclosures/{agent_did}", response_model=DisclosureListResponse)
async def list_context_disclosures(
    agent_did: str,
    service_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):

@router.post("/revoke/{disclosure_id}", response_model=DisclosureRevokeResponse)
async def revoke_context_disclosure(
    disclosure_id: UUID,
    payload: DisclosureRevokeRequest,
    api_key: str = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
```

Both require `api_key` — the agent's own API key. There is no admin override: only the agent can view or erase their own disclosure records.

---

## Exercise 1 — Query Your Disclosure History

After performing one or more disclosures, query the audit trail:

```bash
curl -s "http://localhost:8000/v1/context/disclosures/did:key:z6MkTestContextAgent" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

**Expected:** Paginated list with `total`, `limit`, `offset`, and `disclosures` array. Each record shows field names disclosed but never field values.

Filter to a specific service:
```bash
curl -s "http://localhost:8000/v1/context/disclosures/did:key:z6MkTestContextAgent?service_id=<uuid>" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

---

## Exercise 2 — Perform a GDPR Erasure

```bash
# Get a disclosure_id from the list
DISCLOSURE_ID="<uuid-from-list>"

curl -s -X POST "http://localhost:8000/v1/context/revoke/$DISCLOSURE_ID" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{"agent_did": "did:key:z6MkTestContextAgent"}' | python -m json.tool
```

Then query the database directly to confirm partial erasure:

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, agent_did, fields_disclosed, erased, erased_at FROM context_disclosures WHERE id = '$DISCLOSURE_ID';"
```

**Expected:** `fields_disclosed={}`, `erased=true`, `erased_at` timestamp set. Row still present with `agent_did` intact.

---

## Exercise 3 — Probe the Window Function

Request two different pages and verify the total count is consistent:

```bash
curl -s "http://localhost:8000/v1/context/disclosures/did:key:z6MkTestContextAgent?limit=2&offset=0" \
  -H "X-API-Key: dev-local-only" | python -m json.tool | grep '"total"'

curl -s "http://localhost:8000/v1/context/disclosures/did:key:z6MkTestContextAgent?limit=2&offset=2" \
  -H "X-API-Key: dev-local-only" | python -m json.tool | grep '"total"'
```

**Expected:** Same `total` on both pages — the window function computes the count across all rows, not just the current page.

---

## Best Practices

**Never delete `context_disclosures` rows.** Deletion makes it impossible to prove an erasure happened. An adversary could claim the disclosure never occurred. The erased row with `erased=true` and `erased_at` is the compliance proof that erasure was performed in response to a request.

**Recommended (not implemented here):** An erasure reason field on the revoke endpoint — capturing whether the erasure was agent-requested, admin-initiated for legal hold, or system-automated after a data retention policy. This makes compliance reporting richer without changing the core model.

---

## Interview Q&A

**Q: Under GDPR, can an agent demand complete deletion of a context_disclosures row?**  
A: AgentLedger's position is that retaining the row with erased field-name arrays satisfies Article 17 while complying with the legal basis for processing under Article 6(1)(c) (legal obligation). The specific fields disclosed — the personal data — are erased. The fact of the interaction is retained for audit and dispute resolution. Whether this fully satisfies GDPR is a legal determination, not a technical one.

**Q: Does erasing a disclosure also erase the associated commitment rows?**  
A: No. `context_commitments` rows are separate and are not touched by `revoke_disclosure()`. They expire naturally (5-minute TTL from creation) and remain for audit. The disclosure erasure only affects the `context_disclosures` table.

**Q: Why is `agent_did` retained after erasure but not the field names?**  
A: `agent_did` is a pseudonymous identifier — it proves an agent interacted with a service, but it does not reveal what data was shared. The field names (`user.dob`, `user.ssn`) are the sensitive part. Erasing those while retaining the DID satisfies the privacy requirement while preserving the accountability record.

---

## Key Takeaways

- `list_disclosures()` uses a window function for total count in one query
- Records show field names — never field values
- `revoke_disclosure()` nulls field arrays but keeps the row with `erased=true`
- Retained after erasure: `agent_did`, `service_id`, `ontology_tag`, `trust_tier_at_disclosure`, `created_at`
- Erased rows render as `[ERASED]` in the compliance PDF
- The audit trail proves what happened; erasure removes the specifics

---

## Next Lesson

**Lesson 39 — The Compliance Dossier: PDF Export** walks through `context_compliance.py` — how a full GDPR/CCPA compliance package is generated from the four Layer 4 tables using ReportLab, and what each section of the PDF proves to a regulator.
