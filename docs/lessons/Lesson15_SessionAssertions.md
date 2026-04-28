# Lesson 15 — The Day Pass: Session Assertions

**Layer:** 2 — Identity & Credentials  
**File:** `api/services/sessions.py` (619 lines)  
**Prerequisites:** Lesson 14 (Agent Identity — the `AgentCredentialPrincipal` passed into every session function comes from `authenticate_agent_credential`)  
**Estimated time:** 90 minutes

---

## Welcome

A day pass to a secure facility is not your identity document. Your passport proves who you are (the Verifiable Credential from Lesson 13). The day pass proves you've been cleared for *this building, today, once*. It's tied to a specific purpose, expires soon, and cannot be reused after it's scanned at the gate.

Session assertions work exactly this way. An agent holds a long-lived VC (365 days). To access a specific service, it requests a short-lived session assertion (5 minutes) scoped to that service and ontology tag. The service redeems the assertion exactly once. After that, the token is burned.

By the end of this lesson you will be able to:

- Explain the two session paths: immediate issuance (low-risk) vs. pending approval (sensitive)
- Trace `request_session()` from proof verification through the sensitivity_tier branch to either a JWT or a pending record
- Describe the atomic one-use redemption pattern in `redeem_session()`
- Explain `get_session_status()` and why it queries two tables
- Describe `_scope_allows()` and how wildcard matching works
- Explain why the `crawl_events` table records session redemptions and rejections

---

## What This Connects To

**Previous lesson (Lesson 14):** `authenticate_agent_credential()` returns `AgentCredentialPrincipal`, which carries `capability_scope` and `public_key_jwk`. Both are used in `request_session()` for scope checking and proof verification.

**Lesson 17:** When `sensitivity_tier >= 3`, `request_session` creates an `authorization_requests` row and calls `authorization_service.dispatch_authorization_webhook`. The human-in-the-loop workflow in Lesson 17 then approves or denies that request, and the approval converts it into an issued session assertion.

**Layer 3 (Lessons 21–30):** Layer 3 trust scoring feeds into whether a service qualifies for Tier 4 status — which determines which capability tags appear in the `sensitivity_tier` query here.

---

## Architecture Position

```
Agent                        AgentLedger                      Service
  │                               │                               │
  │─── POST /v1/sessions ────────>│                               │
  │       (VC as Bearer,          │  _scope_allows?               │
  │        session proof)         │  sensitivity_tier?            │
  │                               │  ┌── tier < 3 ──────────────┐│
  │                               │  │  issue_session_assertion  ││
  │                               │  │  INSERT session_assertions││
  │<── 200 {assertion_jwt} ───────│  └───────────────────────────┘│
  │                               │  ┌── tier >= 3 ─────────────┐│
  │<── 202 {pending} ─────────────│  │  INSERT auth_requests     ││
  │                               │  │  dispatch_webhook (HITL)  ││
  │                               │  └───────────────────────────┘│
  │─── POST /v1/sessions/redeem ─────────────────────────────────>│
  │        (assertion_jwt)        │                               │
  │<── 200 {accepted} ────────────────────────────────────────────│
```

---

## Core Concepts

### The Two Session Paths

Session requests branch on `ontology_tags.sensitivity_tier`:

| Sensitivity tier | Path | Result |
|---|---|---|
| < 3 (low/medium risk) | Immediate | JWT issued now, stored in `session_assertions` |
| ≥ 3 (high risk) | Pending | `authorization_requests` row created, HITL webhook fired |

This design means that routine capability invocations (search, manifest lookup, low-risk data access) complete without human involvement. Sensitive operations (medical records, financial data, access to Tier 4 services) require explicit human approval before a token is ever issued.

### The Atomic Redemption Pattern

`redeem_session()` uses a single SQL UPDATE to enforce one-use behavior:

```sql
UPDATE session_assertions
SET was_used = true, used_at = NOW()
WHERE assertion_jti = :assertion_jti
  AND service_id = :service_id
  AND was_used = false
  AND expires_at > NOW()
RETURNING agent_did, ontology_tag, authorization_ref
```

If `was_used` is already `true`, or `expires_at` is in the past, the WHERE clause matches zero rows — the UPDATE returns nothing. No separate SELECT is needed to check validity first. This eliminates the classic time-of-check/time-of-use (TOCTOU) race condition: even if two requests arrive simultaneously with the same `jti`, only one UPDATE can win the `was_used = false` condition.

### Scope Matching

Agents are issued credentials with a `capability_scope` list like `["health.*", "finance.reports"]`. Before issuing any session assertion, the server checks whether the requested `ontology_tag` falls within that scope.

`_scope_allows()` supports three matching modes:
1. **Wildcard all**: `"*"` — allows any tag
2. **Prefix wildcard**: `"health.*"` or `"health"` — allows `health.records`, `health.labs`, etc.
3. **Exact match**: `"finance.reports"` — allows only that exact tag

This gives credential issuers fine-grained control: a healthcare data agent can be scoped to `health.*` without granting access to finance or identity endpoints.

---

## Code Walkthrough

### 1. Helper Functions (lines 28–81)

**`_service_did_from_domain` (line 28):**

```python
def _service_did_from_domain(domain: str) -> str:
    return f"did:web:{domain}"
```

A one-liner that converts a service domain to its `did:web` identifier. Kept as a named function (rather than an inline f-string) so every callsite uses the same format and the format can change in one place.

**`_session_proof_payload` (lines 33–45):**

```python
def _session_proof_payload(principal, request):
    return {
        "agent_did": principal.did,
        "service_domain": request.service_domain,
        "ontology_tag": request.ontology_tag,
        "request_context": request.request_context,
        "nonce": request.proof.nonce,
        "created_at": request.proof.created_at.astimezone(timezone.utc).isoformat(),
    }
```

This is the payload the agent signed when building the session request. The server reconstructs this exact dict, canonicalizes it, and verifies the signature — identical to the registration proof pattern in Lesson 14. Including `service_domain` and `ontology_tag` in the signed payload means the agent cannot later claim it signed a request for a different service or capability.

**`_scope_allows` (lines 69–81):**

```python
def _scope_allows(capability_scope: list[str], ontology_tag: str) -> bool:
    if not capability_scope:
        return False
    for scope in capability_scope:
        if scope == "*":
            return True
        normalized_scope = scope[:-2] if scope.endswith(".*") else scope
        if normalized_scope == ontology_tag:
            return True
        if ontology_tag.startswith(normalized_scope + "."):
            return True
    return False
```

The `scope[:-2]` strips the trailing `.*` so that `"health.*"` becomes `"health"` for comparison. The two checks then cover:
- `normalized_scope == ontology_tag`: exact match for when the scope is `"health"` and the tag is `"health"`
- `ontology_tag.startswith(normalized_scope + ".")`: prefix match for when the tag is `"health.records"` and the scope is `"health"`

Note the `+ "."` in the prefix check: it prevents `"healthcheck.status"` from matching `"health"` scope.

### 2. The Session Request Flow (lines 84–305)

**Proof verification (lines 91–110):**

```python
# Timestamp freshness check
age_seconds = abs((...).total_seconds())
if age_seconds > settings.proof_nonce_ttl_seconds:
    raise HTTPException(422, "proof timestamp is outside the allowed replay window")

# Signature verification
if not verify_json_signature(
    payload=_session_proof_payload(principal, request),
    signature=request.proof.signature,
    public_jwk=principal.public_key_jwk,
):
    raise HTTPException(422, "invalid session proof signature")
```

The session proof uses the same pattern as the registration proof but verifies against the agent's own public key (from `principal.public_key_jwk`), not the issuer's key. This proves the session request was initiated by the agent who holds the credential — not by a third party who intercepted the credential.

**Scope check (lines 112–116):**

```python
if not _scope_allows(principal.capability_scope, request.ontology_tag):
    raise HTTPException(403, "requested ontology_tag is outside the agent credential scope")
```

This is an application-level enforcement of the credential boundary. Even if an agent authenticates successfully, it cannot request session assertions for capabilities it wasn't credentialed for.

**Service lookup (lines 118–149):**

```python
SELECT
    s.id AS service_id,
    s.domain,
    s.last_verified_at,
    t.sensitivity_tier
FROM services s
JOIN service_capabilities c ON c.service_id = s.id
JOIN ontology_tags t ON t.tag = c.ontology_tag
WHERE s.domain = :service_domain
  AND c.ontology_tag = :ontology_tag
  AND s.is_active = true
  AND s.is_banned = false
LIMIT 1
```

This query validates three things at once: the service exists, it offers the requested capability (`JOIN service_capabilities`), and the ontology tag has a known sensitivity tier (`JOIN ontology_tags`). `last_verified_at IS NULL` is a proxy for "service identity not yet confirmed" (line 150).

**The sensitivity branch (lines 156–305):**

When `sensitivity_tier >= 3`:

```python
# Create authorization_requests row
auth_result = await db.execute(text("""
    INSERT INTO authorization_requests (agent_did, service_id, ontology_tag,
        sensitivity_tier, request_context, status, expires_at, created_at)
    VALUES (:agent_did, :service_id, :ontology_tag, :sensitivity_tier,
            CAST(:request_context AS JSONB), 'pending', :expires_at, NOW())
    RETURNING id
"""), {...})

# Log the event
await db.execute(text("""
    INSERT INTO crawl_events (service_id, event_type, ...)
    VALUES (:service_id, 'authorization_requested', ...)
"""), {...})

await db.commit()

# Fire the webhook (after commit, so the record is visible to the webhook handler)
await authorization_service.dispatch_authorization_webhook("authorization.pending", {...})

return SessionStatusResponse(status="pending_approval", ...)
```

The webhook dispatch happens **after** `db.commit()` — the same pattern as Redis writes in Lesson 14. If the webhook fails, the authorization record is already committed and can be retried.

When `sensitivity_tier < 3`:

```python
service_did = _service_did_from_domain(service_row["domain"])
assertion_jwt, assertion_jti, expires_at = credentials.issue_session_assertion(
    subject_did=principal.did,
    service_did=service_did,
    service_id=str(service_row["service_id"]),
    ontology_tag=request.ontology_tag,
)

# Store the assertion
await db.execute(text("""
    INSERT INTO session_assertions (
        assertion_jti, agent_did, service_id, ontology_tag,
        assertion_token, expires_at, was_used, issued_at
    ) VALUES (..., false, NOW())
"""), {...})

return SessionStatusResponse(status="issued", assertion_jwt=assertion_jwt, ...)
```

The `assertion_jti` (UUID from `issue_session_assertion`) is stored as a column, not derived from the row ID. This is the lookup key for redemption — the service sends `assertion_jti` in the redemption request, not an internal session ID.

### 3. Session Status Polling (lines 308–450)

`get_session_status()` is the polling endpoint agents use while waiting for HITL approval. It queries two tables and returns a unified `SessionStatusResponse`:

```
┌─────────────────────────────────────────┐
│ Does a session_assertions row exist?    │
│ (for this session_id AND agent_did)     │
└─────────────┬───────────────────────────┘
              │ Yes → check expiry → return "issued" or "expired"
              │ No
              ▼
┌─────────────────────────────────────────┐
│ Does an authorization_requests row      │
│ exist? (for this id AND agent_did)      │
└─────────────┬───────────────────────────┘
              │ No → 404
              │ Yes
              ▼
         status == "pending" AND expired → UPDATE to "expired" → return "expired"
         status == "approved" → look up linked session → return "issued"
         status == "denied"   → return "denied"
         status == "expired"  → return "expired"
         status == "pending"  → return "pending_approval"
```

The `status == "approved"` case (line 400–430) does a second query to find the `session_assertions` row that was created when the human approved the request. This linked lookup uses `session_assertions.authorization_ref = authorization_requests.id`.

The lazy expiry update (lines 375–398) is worth noting: the `authorization_requests` row isn't updated to `"expired"` status in a background job; it's updated the first time someone polls its status. This is acceptable here — the record is tiny and the query is indexed.

### 4. The Redemption Flow (lines 453–619)

This is the most security-critical function in the file:

```python
async def redeem_session(db, request):
    # 1. Verify the JWT signature and expiry
    claims = credentials.verify_session_assertion(request.assertion_jwt)

    # 2. Validate audience binding
    expected_service_did = _service_did_from_domain(request.service_domain)
    if claims.get("aud") != expected_service_did:
        raise HTTPException(403, "session assertion audience does not match the service")

    # 3. Validate service binding
    service_row = ...  # SELECT from services WHERE domain = ...
    if str(service_row["id"]) != str(claims.get("service_id")):
        raise HTTPException(403, "session assertion service binding is invalid")

    # 4. Atomic one-use UPDATE
    redeem_result = await db.execute(text("""
        UPDATE session_assertions
        SET was_used = true, used_at = NOW()
        WHERE assertion_jti = :assertion_jti
          AND service_id = :service_id
          AND was_used = false
          AND expires_at > NOW()
        RETURNING agent_did, ontology_tag, authorization_ref
    """), {...})
```

The four security checks in order:

1. **JWT validity** — PyJWT verifies signature, expiry, issuer
2. **Audience check** — `aud` must match the domain the service is claiming to be
3. **Service ID binding** — the service UUID in the JWT `service_id` claim must match the actual service looked up by domain; prevents a service from redeeming tokens intended for a different service
4. **Atomic UPDATE** — `was_used = false AND expires_at > NOW()` as the WHERE clause

**The error disambiguation (lines 548–610):**

When the UPDATE matches zero rows (token already used or expired), the code does a second SELECT to determine *why* and emit the right error:

```python
existing_row = await db.execute(text("""
    SELECT was_used, expires_at
    FROM session_assertions
    WHERE assertion_jti = :assertion_jti AND service_id = :service_id
"""), {...})

if existing_row and existing_row["was_used"]:
    # Log session_redeem_rejected (reason: already_redeemed)
    raise HTTPException(409, "session assertion has already been redeemed")
if existing_row:
    # Log session_redeem_rejected (reason: expired_or_invalid)

raise HTTPException(403, "invalid or expired session assertion")
```

`409 Conflict` for already-redeemed vs. `403 Forbidden` for expired/invalid — different HTTP status codes for different operational meanings, which matters for retry logic in the consuming service.

**The `crawl_events` trail:**

Every redemption outcome is logged:
- `session_redeemed` — successful first use
- `session_redeem_rejected` — second use or expired token attempt

This feeds Layer 3 trust scoring (specifically the local reputation component) without requiring a dedicated audit table — `crawl_events` already exists for Layer 1 crawl events and doubles as an activity log.

---

## Exercises

### Exercise 1 — Request a low-risk session assertion

```bash
# First register an agent (or use one from Lesson 14)
# Then use its credential JWT as Bearer

curl -s -X POST http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer <credential_jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "service_domain": "example-service.com",
    "ontology_tag": "search.manifests",
    "request_context": {"purpose": "capability discovery"},
    "proof": {
      "signature": "<signed_proof>",
      "nonce": "session-nonce-1",
      "created_at": "<now_iso>"
    }
  }' | python -m json.tool
```

Expected output (low-risk service):
```json
{
  "status": "issued",
  "session_id": "f4a2b1c3-...",
  "assertion_jwt": "eyJhbGci...",
  "service_did": "did:web:example-service.com",
  "expires_at": "2026-04-27T12:05:00+00:00"
}
```

### Exercise 2 — Redeem the session assertion

```bash
# The service calls this endpoint to accept the assertion
curl -s -X POST http://localhost:8000/v1/sessions/redeem \
  -H "Content-Type: application/json" \
  -d '{
    "assertion_jwt": "<assertion_jwt from Exercise 1>",
    "service_domain": "example-service.com"
  }' | python -m json.tool
```

Expected output:
```json
{
  "status": "accepted",
  "agent_did": "did:key:z6Mk...",
  "ontology_tag": "search.manifests",
  "authorization_ref": null
}
```

### Exercise 3 (failure) — Replay a redeemed token

```bash
# Submit the same assertion a second time
curl -s -X POST http://localhost:8000/v1/sessions/redeem \
  -H "Content-Type: application/json" \
  -d '{
    "assertion_jwt": "<same assertion_jwt>",
    "service_domain": "example-service.com"
  }' | python -m json.tool
```

Expected output:
```json
{"detail": "session assertion has already been redeemed"}
```

HTTP status: `409 Conflict`

### Exercise 4 — Test scope enforcement

```python
# In the Python REPL: verify _scope_allows behavior
from api.services.sessions import _scope_allows

# Full wildcard
assert _scope_allows(["*"], "health.records") == True

# Prefix wildcard
assert _scope_allows(["health.*"], "health.records") == True
assert _scope_allows(["health.*"], "health") == True

# Exact match only
assert _scope_allows(["health.records"], "health.labs") == False

# Prevent false prefix match
assert _scope_allows(["health"], "healthcheck.status") == False

print("All scope tests passed")
```

Expected output:
```
All scope tests passed
```

### Exercise 5 — Inspect the crawl_events trail

```bash
# After completing Exercises 1-3, inspect the event log
docker compose exec db psql -U agentledger -c "
SELECT event_type, details->>'reason' AS reason, created_at
FROM crawl_events
WHERE event_type IN ('session_redeemed', 'session_redeem_rejected')
ORDER BY created_at DESC
LIMIT 5;
"
```

Expected output:
```
      event_type       |     reason     |          created_at
-----------------------+----------------+------------------------------
 session_redeem_rejected | already_redeemed | 2026-04-27 12:01:15+00:00
 session_redeemed      |                | 2026-04-27 12:01:10+00:00
```

---

## Best Practices

### What AgentLedger does

- **Atomic UPDATE redemption** — `WHERE was_used = false AND expires_at > NOW()` eliminates TOCTOU race conditions on token use
- **Audience binding** — `aud` claim + service domain comparison prevents cross-service token reuse
- **Service ID binding** — `service_id` UUID claim validates that the domain matches the expected service record
- **Webhook after commit** — HITL webhook fires after `db.commit()` so the authorization record is always committed before external notification
- **`crawl_events` logging** — both successful and rejected redemptions are logged for trust scoring and audit without a separate table

### Recommended (not implemented here)

- **Redis-backed `jti` burn list** — currently, one-use enforcement requires a database query (the UPDATE + optional SELECT). Under heavy load, a Redis SET of consumed `jti` values would allow sub-millisecond replay detection before hitting the database.
- **Session assertion delivery without storing the token** — the full JWT is stored in `session_assertions.assertion_token`. A more privacy-preserving design would store only `assertion_jti` and `expires_at`, returning the JWT only at issuance and re-issuing on demand when the agent polls status.
- **Webhook delivery guarantees** — `dispatch_authorization_webhook` is best-effort. A durable outbox pattern (write webhook payload to a table, deliver via background worker) would guarantee delivery even if the webhook endpoint is temporarily unavailable.

---

## Interview Q&A

**Q: How does the atomic UPDATE in `redeem_session` prevent double-redemption under concurrent requests?**

A: PostgreSQL row-level locking ensures that when two concurrent UPDATE statements target the same row, only one can acquire the lock at a time. The first UPDATE finds `was_used = false`, succeeds, and sets `was_used = true`. The second UPDATE's WHERE clause then fails because `was_used` is no longer `false` — it returns zero rows. No application-level locking or SELECT-before-UPDATE is needed. The database serializes concurrent redemptions correctly by design.

**Q: Why does `_scope_allows` append `"."` in the prefix check?**

A: Without it, `"health"` scope would match `"healthcheck.status"` because `"healthcheck.status".startswith("health")` is `True`. The `+ "."` makes the check `"healthcheck.status".startswith("health.")` which is `False`. The dot acts as a namespace separator, ensuring scope matching respects capability hierarchy boundaries.

**Q: What happens to a `pending_approval` session request if the HITL approver never responds?**

A: The `authorization_requests` row has an `expires_at` column set to `NOW() + authorization_request_ttl_seconds`. On the next poll of `get_session_status()`, if `status == "pending"` and `expires_at <= NOW()`, the code updates the row to `"expired"` in place and returns `"expired"`. The agent must then re-request the session if it still needs access.

**Q: Why does `request_session` log to `crawl_events` rather than a dedicated session events table?**

A: `crawl_events` is the general-purpose activity log for Layer 1 service monitoring. Reusing it keeps the schema lean and allows Layer 3 trust scoring (which already queries `crawl_events` for local reputation signals) to incorporate session activity without a new join. The `event_type` column discriminates between crawl events and session events.

**Q: What is `authorization_ref` in the session assertion, and when is it populated?**

A: `authorization_ref` is a foreign key to the `authorization_requests` row that authorized this session. It's `NULL` for immediate (low-risk) sessions and populated for sessions that went through the HITL approval flow. Downstream services can use it to link an active session back to the approval record — for audit, dispute resolution, or access revocation targeting a specific approval.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 15 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ Session request paths                                           │
│   sensitivity_tier < 3  → issue_session_assertion() → "issued" │
│   sensitivity_tier >= 3 → INSERT auth_requests → "pending"     │
│                                                                 │
│ Scope matching (_scope_allows)                                  │
│   "*"          → allows any tag                                │
│   "health.*"   → allows health.* prefix (with dot guard)       │
│   "health.lab" → exact match only                              │
│                                                                 │
│ Atomic one-use redemption                                       │
│   UPDATE ... WHERE was_used = false AND expires_at > NOW()     │
│   Zero rows returned → already used or expired                 │
│   409 Conflict (used) vs 403 Forbidden (expired/invalid)       │
│                                                                 │
│ Security checks in redeem_session (in order)                   │
│   1. JWT signature + expiry (PyJWT)                            │
│   2. aud == expected_service_did                               │
│   3. service_id claim == DB service UUID                       │
│   4. Atomic UPDATE (was_used=false AND expires_at>NOW())       │
│                                                                 │
│ Tables written by sessions.py                                  │
│   session_assertions   (issued JWTs, one-use tracking)         │
│   authorization_requests (HITL pending/approved/denied/expired)│
│   crawl_events          (session_redeemed / _rejected events)  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 16 — The Business Card** covers `api/services/service_identity.py`: how registered services activate a `did:web` identity, how AgentLedger signs and publishes a service manifest, and how `did:web` resolution works so external consumers can verify service identity without calling AgentLedger directly.
