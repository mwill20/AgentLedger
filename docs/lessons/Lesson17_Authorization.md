# Lesson 17 — The Approval Desk: Human-in-the-Loop Authorization

**Layer:** 2 — Identity & Credentials  
**File:** `api/services/authorization.py` (525 lines)  
**Prerequisites:** Lesson 15 (Session Assertions — HITL requests are created in `request_session`; approval produces a session assertion)  
**Estimated time:** 75 minutes

---

## Welcome

An approval desk doesn't make decisions automatically. Every request that arrives sits in the queue until a human reviews it. The human can approve (access granted) or deny (access refused). While a request waits, it ages — and if no one responds before it expires, the access is implicitly denied.

Layer 2 HITL authorization works exactly this way. When a session request touches a sensitivity tier 3 or higher capability, `request_session()` creates an `authorization_requests` record and fires a webhook to notify the operator's approval system. `authorization.py` provides the decision endpoints: list the pending queue, approve, or deny. An approval issues a session assertion directly and notifies the agent via webhook. A denial updates the record and notifies the agent.

By the end of this lesson you will be able to:

- Explain why high-sensitivity session requests go through HITL instead of immediate issuance
- Trace the full `approve_authorization_request()` flow including the re-approval idempotency path
- Describe the `FOR UPDATE` row lock and why it's necessary
- Explain the HMAC-signed webhook pattern
- Describe `list_pending_authorizations()` and its lazy expiry side effect
- Distinguish the deny path's idempotency behavior from the approve path

---

## What This Connects To

**Lesson 15:** `request_session()` creates `authorization_requests` rows for tier ≥ 3 capabilities and calls `dispatch_authorization_webhook("authorization.pending", ...)`. That's where this lesson picks up.

**Lesson 13:** `approve_authorization_request()` calls `credentials.issue_session_assertion()` with `authorization_ref` — the session assertion in the approval case carries a link back to the human decision record.

**Layer 3 (Lessons 21–30):** Layer 3 trust tier 4 gates access to the most sensitive capabilities. An agent must obtain a HITL-approved session to access these capabilities — the approval record provides an audit trail that links back to the human decision.

---

## Architecture Position

```
Agent
  │
  │  POST /v1/sessions (sensitivity_tier >= 3)
  │
  ▼
sessions.py ─── INSERT authorization_requests (status='pending')
                └── dispatch_authorization_webhook("authorization.pending")
                                                       │
                                                       ▼
                                               Operator system
                                               (Slack, PagerDuty, etc.)
                                                       │
                                         GET /v1/authorizations        (list queue)
                                         POST /v1/authorizations/{id}/approve
                                         POST /v1/authorizations/{id}/deny
                                                       │
                                                       ▼
                                      authorization.py ─── SELECT FOR UPDATE
                                                       ├── issue_session_assertion
                                                       ├── UPDATE status='approved'/'denied'
                                                       ├── INSERT crawl_events
                                                       └── dispatch_authorization_webhook
                                                           ("authorization.approved/denied")
```

---

## Core Concepts

### The HITL State Machine

An `authorization_requests` record moves through exactly these states:

```
                  [created]
                      │
                      ▼
                   pending  ─── expires_at <= NOW() ──► expired
                    /   \
                   /     \
               approved   denied
```

There are no cycles and no transitions back to `pending`. Once a decision is made, it is final. A second call to `approve` on an already-approved record returns the existing session assertion (idempotent). A second call to `deny` on an already-denied record returns the existing denial (idempotent). Calling `approve` on a denied or expired record raises `409 Conflict`.

### The `FOR UPDATE` Row Lock

```sql
SELECT ... FROM authorization_requests ... WHERE id = :id FOR UPDATE
```

This SQL clause acquires a row-level write lock for the duration of the transaction. If two operators click "Approve" simultaneously on the same request:
- Operator A's SELECT gets the lock; Operator B's SELECT waits
- Operator A issues the session assertion, commits, releases the lock
- Operator B's SELECT then reads `status='approved'` and follows the re-approval path (returns the existing session)

Without `FOR UPDATE`, both SELECTs would read `status='pending'` simultaneously, both would insert session assertions, and the agent would receive two tokens for the same authorization. The lock ensures exactly one winner.

### HMAC Webhook Signing

Outbound webhooks carry an optional HMAC-SHA256 signature:

```
X-AgentLedger-Signature: sha256={digest}
X-AgentLedger-Timestamp: {unix_timestamp}
```

The signature is computed as:

```
HMAC-SHA256(
    key=AUTHORIZATION_WEBHOOK_SECRET,
    message=f"{timestamp}.{json_payload_bytes}"
)
```

Including the timestamp in the signed message prevents **replay attacks**: capturing a valid webhook payload and re-submitting it later would fail because the timestamp would no longer be "recent" (the receiving system should reject timestamps more than 5 minutes old).

The signature is optional: if `AUTHORIZATION_WEBHOOK_SECRET` is not configured, the headers are sent without `X-AgentLedger-Signature`. This allows development setups to receive webhooks without requiring a shared secret.

---

## Code Walkthrough

### 1. Webhook Infrastructure (lines 53–88)

**`_webhook_headers` (lines 53–64):**

```python
def _webhook_headers(event_type, payload_json, timestamp):
    headers = {
        "X-AgentLedger-Event": event_type,
        "X-AgentLedger-Timestamp": timestamp,
    }
    secret = settings.authorization_webhook_secret.strip()
    if secret:
        signed = f"{timestamp}.{payload_json}".encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), signed, sha256).hexdigest()
        headers["X-AgentLedger-Signature"] = f"sha256={digest}"
    return headers
```

Note `hmac.new(...)` (not `hmac.HMAC(...)`) — this is the `hmac` module's factory function. The `sha256` reference is the `hashlib.sha256` constructor imported at the top of the file.

The `payload_json` passed in is the canonical JSON string of the entire envelope: `{"event": ..., "payload": ..., "sent_at": ...}`. The HMAC covers all of this, including the event type and timestamp.

**`dispatch_authorization_webhook` (lines 67–88):**

```python
async def dispatch_authorization_webhook(event_type, payload):
    url = settings.authorization_webhook_url.strip()
    if not url:
        return   # No-op when webhook URL is not configured

    envelope = {
        "event": event_type,
        "payload": payload,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    payload_json = json.dumps(envelope, sort_keys=True, default=str)
    ...
    try:
        async with httpx.AsyncClient(timeout=...) as client:
            response = await client.post(url, json=envelope, headers=headers)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("authorization webhook dispatch failed for %s: %s", event_type, exc)
```

The `except Exception` swallows all webhook failures. This is intentional: webhook delivery is best-effort. The authorization state is committed to the database before the webhook fires; if the webhook fails, the decision is still recorded and can be retrieved by polling. Letting webhook failures propagate would make approvals non-idempotent and dependent on network reliability.

`json.dumps(..., sort_keys=True, default=str)` serializes the envelope canonically. `default=str` handles `datetime` objects that Pydantic hasn't pre-serialized — it calls `str()` on them, producing ISO format strings.

### 2. `list_pending_authorizations` (lines 90–139)

```python
async def list_pending_authorizations(db):
    # First: bulk expire stale pending requests
    await db.execute(text("""
        UPDATE authorization_requests
        SET status = 'expired',
            decided_at = COALESCE(decided_at, NOW())
        WHERE status = 'pending'
          AND expires_at <= NOW()
    """))
    await db.commit()

    # Then: fetch remaining pending
    result = await db.execute(text("""
        SELECT ar.id, ar.agent_did, s.domain AS service_domain, ...
        FROM authorization_requests ar
        JOIN services s ON s.id = ar.service_id
        WHERE ar.status = 'pending'
        ORDER BY ar.created_at ASC
    """))
    ...
```

**Lazy bulk expiry** — the first UPDATE bulk-expires all stale pending requests before returning the live queue. This is a "piggybacked maintenance" pattern: rather than running a dedicated background job to expire requests, expiry happens whenever an operator lists the queue. The result is that the queue never shows expired items to the human reviewer, even without a running background task.

`COALESCE(decided_at, NOW())` sets `decided_at` only if it wasn't already set (which it shouldn't be for pending requests, but guards against data inconsistency).

The list is ordered `ASC` by `created_at` — oldest first. Operators should process requests in FIFO order to minimize wait time for agents whose requests are nearing expiry.

### 3. `approve_authorization_request` (lines 142–381)

This is the longest function in the file. Step through the key sections:

**The `FOR UPDATE` fetch (lines 148–175):**

```python
result = await db.execute(text("""
    SELECT ar.id, ar.agent_did, ar.service_id, ar.ontology_tag,
           ar.status, ar.expires_at, s.domain AS service_domain,
           s.is_active, s.is_banned, s.last_verified_at,
           ai.is_active AS agent_is_active, ai.is_revoked AS agent_is_revoked
    FROM authorization_requests ar
    JOIN services s ON s.id = ar.service_id
    JOIN agent_identities ai ON ai.did = ar.agent_did
    WHERE ar.id = :authorization_request_id
    FOR UPDATE
"""), {...})
```

The JOIN to `services` and `agent_identities` is important: approving a request re-validates that both the service and the agent are still in good standing at decision time. An agent that registered when the request was created might have been revoked before the human approves it.

**Idempotent re-approval (lines 183–210):**

```python
if row["status"] == "approved":
    session_result = await db.execute(text("""
        SELECT id, assertion_token, expires_at
        FROM session_assertions
        WHERE authorization_ref = :authorization_request_id
        ORDER BY issued_at DESC LIMIT 1
    """), {...})
    session_row = session_result.mappings().first()
    ...
    return AuthorizationDecisionResponse(
        status="approved", ...,
        assertion_jwt=session_row["assertion_token"],
        ...
    )
```

If a second `approve` call arrives for an already-approved request (retry, UI glitch, concurrent operator), the function returns the existing session assertion rather than issuing a second one. The `ORDER BY issued_at DESC LIMIT 1` gets the most recently issued session for that authorization (there should only be one, but defensive).

**State guards (lines 212–256):**

```python
if row["status"] == "denied":
    raise HTTPException(409, "authorization request has already been denied")
if row["status"] == "expired":
    raise HTTPException(409, "authorization request has already expired")
if row["expires_at"] <= datetime.now(timezone.utc):
    # Mark as expired and raise 409
    ...
if not row["agent_is_active"] or row["agent_is_revoked"]:
    raise HTTPException(403, "agent identity is inactive or revoked")
if not row["is_active"] or row["is_banned"]:
    raise HTTPException(403, "service is inactive or banned")
if row["last_verified_at"] is None:
    raise HTTPException(412, "service identity is not active")
```

The temporal expiry check (line 223) is separate from the status-based expiry check (line 217). It's possible for a request to still have `status='pending'` but `expires_at` in the past — for example, if the list endpoint was never called to trigger bulk expiry. The inline check catches this case and marks the request expired before raising.

**Session issuance and dual write (lines 258–346):**

```python
assertion_jwt, assertion_jti, expires_at = credentials.issue_session_assertion(
    subject_did=row["agent_did"],
    service_did=service_did,
    service_id=str(row["service_id"]),
    ontology_tag=row["ontology_tag"],
    authorization_ref=str(row["id"]),   # Links back to approval record
    ttl_seconds=settings.approved_session_ttl_seconds,
)

# INSERT into session_assertions (with authorization_ref populated)
# UPDATE authorization_requests SET status='approved', approver_id=...
# INSERT into crawl_events ('authorization_approved')
await db.commit()
```

Note `authorization_ref=str(row["id"])` — the session assertion JWT carries the UUID of the human approval record. When the service receives and redeems this token, it can resolve the full approval chain if needed. The `session_assertions.authorization_ref` column is the DB-side link.

`approved_session_ttl_seconds` is a separate setting from `session_assertion_ttl_seconds` — HITL-approved sessions can be given a longer validity window (e.g., 30 minutes vs. 5 minutes) since a human already made the decision.

**Webhook after commit (lines 367–380):**

```python
await dispatch_authorization_webhook("authorization.approved", {...})
return response
```

Same post-commit webhook pattern as in `request_session()`: the decision is committed to the database first, then the webhook fires. If the webhook fails, the approval is preserved and can be fetched by polling.

### 4. `deny_authorization_request` (lines 384–524)

The deny path is simpler than the approve path — it doesn't issue any token:

```python
# Same FOR UPDATE fetch
# Idempotency: already denied → return existing denial response
# Guards: already approved → 409; already expired → 409
# Expired at decision time → mark expired → 409

# Write the denial
await db.execute(text("""
    UPDATE authorization_requests
    SET status = 'denied', approver_id = :approver_id, decided_at = NOW()
    WHERE id = :authorization_request_id
"""), {...})

# INSERT crawl_events ('authorization_denied')
await db.commit()

# Fire webhook: "authorization.denied"
```

**Deny is idempotent, approve re-approval returns existing token:**

| Scenario | approve behavior | deny behavior |
|---|---|---|
| Already approved | Return existing session JWT | Raise 409 |
| Already denied | Raise 409 | Return existing denial response |
| Already expired | Raise 409 | Raise 409 |

The asymmetry is intentional: re-approving is a retry scenario where the caller wants the token. Re-denying is an idempotent acknowledgment — the caller doesn't need any new data, just confirmation.

---

## Exercises

### Exercise 1 — Observe the HITL flow end-to-end

```bash
# 1. Create a session request for a high-sensitivity capability
# (requires an active agent credential and a sensitivity_tier >= 3 service)
curl -s -X POST http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer <agent_credential_jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "service_domain": "highsec-service.example.com",
    "ontology_tag": "health.records",
    "request_context": {"purpose": "patient lookup"},
    "proof": {"signature": "<sig>", "nonce": "<nonce>", "created_at": "<now>"}
  }' | python -m json.tool

# Expected:
# {"status": "pending_approval", "authorization_request_id": "<uuid>", "expires_at": "..."}

# 2. List the pending queue
curl -s http://localhost:8000/v1/authorizations \
  -H "X-API-Key: <operator_key>" | python -m json.tool

# 3. Approve the request
curl -s -X POST "http://localhost:8000/v1/authorizations/<uuid>/approve" \
  -H "X-API-Key: <operator_key>" | python -m json.tool

# Expected: {"status": "approved", "assertion_jwt": "eyJhbGci...", ...}
```

### Exercise 2 — Test re-approval idempotency

```bash
# Approve the same request a second time
curl -s -X POST "http://localhost:8000/v1/authorizations/<uuid>/approve" \
  -H "X-API-Key: <operator_key>" | python -m json.tool

# Expected: same assertion_jwt as the first approval (not a new token)
```

### Exercise 3 — Verify the HMAC signature

```python
import hmac, hashlib, json, time

WEBHOOK_SECRET = "your-configured-secret"
timestamp = str(int(time.time()))

# Reconstruct the envelope as the server would
envelope = {
    "event": "authorization.approved",
    "payload": {"authorization_request_id": "...", "status": "approved", ...},
    "sent_at": "2026-04-27T12:00:00+00:00",
}
payload_json = json.dumps(envelope, sort_keys=True, default=str)
signed = f"{timestamp}.{payload_json}".encode("utf-8")
expected_digest = hmac.new(WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()

# Simulate receiving the webhook header
received_signature = f"sha256={expected_digest}"
print("Valid:", received_signature.startswith("sha256=") and received_signature[7:] == expected_digest)
```

Expected output:
```
Valid: True
```

### Exercise 4 (failure) — Attempt to approve an expired request

```bash
# First, expire a pending request by waiting (or directly modifying expires_at in psql)
docker compose exec db psql -U agentledger -c "
UPDATE authorization_requests
SET expires_at = NOW() - interval '1 second'
WHERE id = '<uuid>' AND status = 'pending';
"

# Then attempt approval
curl -s -X POST "http://localhost:8000/v1/authorizations/<uuid>/approve" \
  -H "X-API-Key: <operator_key>" | python -m json.tool
```

Expected output:
```json
{"detail": "authorization request has expired"}
```

HTTP status: `409 Conflict`

---

## Best Practices

### What AgentLedger does

- **`FOR UPDATE` row lock** — prevents concurrent approvals from issuing duplicate session assertions
- **Idempotent approve** — returns the existing session assertion on re-approval; callers can retry safely
- **HMAC-signed webhooks** — optional but recommended; prevents replay and spoofing attacks on receiving systems
- **Best-effort webhook dispatch** — `except Exception: logger.warning(...)` ensures authorization decisions are never blocked by webhook delivery failures
- **State re-validated at decision time** — `approve_authorization_request` re-checks both agent and service status even after the original session request passed those checks

### Recommended (not implemented here)

- **Durable outbox for webhooks** — current dispatch is in-process. If the process crashes between `db.commit()` and `dispatch_authorization_webhook()`, the webhook is lost. Writing the webhook payload to a database table and delivering via background worker would guarantee at-least-once delivery.
- **Configurable per-approval TTL** — `approved_session_ttl_seconds` is a global setting. A production system might want per-service or per-sensitivity-tier TTLs so that medical record sessions expire sooner than general-purpose approvals.
- **Approval delegation** — currently any valid operator API key can approve any request. A production HITL system would scope approvals to specific service domains or ontology tags per approver identity.

---

## Interview Q&A

**Q: Why does `approve_authorization_request` re-validate the agent and service status at decision time, even though those checks already happened when the session was requested?**

A: An agent's credential could be revoked in the window between when the session request was created and when the human approves it. Similarly, a service could be banned. Approving access for a revoked agent or banned service would be a security violation. The re-validation at decision time ensures the current state is checked, not just the state at request creation.

**Q: What attack does the HMAC timestamp protect against?**

A: Replay attacks. Without a timestamp component, an attacker who intercepts a valid `authorization.approved` webhook (e.g., via a network tap) could re-submit it later to trigger the receiving system to re-process the approval event — potentially granting access that has since been revoked. Including the timestamp in the HMAC forces the receiving system to reject requests with timestamps outside a short freshness window (typically ±5 minutes), invalidating captured payloads.

**Q: Why is `deny` idempotent when `approve` raises 409 on a re-deny?**

A: A `deny` operation has no side effect that needs to be communicated back — the caller just needs to know the request was denied. A second `deny` returns the same denial response, since the state hasn't changed in any meaningful way. A second `approve` of an already-approved request also has no side effect, but it *does* need to return something useful (the existing session JWT) so the caller can complete the approval flow. The different idempotency semantics match the different informational needs.

**Q: What happens if the lazy bulk-expiry in `list_pending_authorizations` fails?**

A: The SELECT after the failed UPDATE would still work — it just might include requests that should have been expired but weren't yet updated. Those requests would be rejected at decision time (the `expires_at <= datetime.now()` check catches them inline in both `approve` and `deny`). The lazy expiry is a UI convenience that keeps the queue clean; correctness doesn't depend on it.

**Q: How does an operator know when a new authorization request arrives if they're not continuously polling?**

A: The `dispatch_authorization_webhook("authorization.pending", ...)` call in `sessions.py` fires whenever a new HITL request is created. The operator's system (Slack, PagerDuty, a custom dashboard) receives this webhook and notifies the appropriate reviewer. AgentLedger supports one configured webhook URL — routing decisions to specific teams is the receiving system's responsibility.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 17 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ HITL state machine                                              │
│   pending → approved (session issued)                          │
│   pending → denied                                             │
│   pending → expired (TTL elapsed, no decision)                 │
│                                                                 │
│ Concurrency safety                                             │
│   SELECT ... FOR UPDATE on authorization_requests              │
│   Serializes concurrent approve/deny attempts                  │
│                                                                 │
│ Webhook signing                                                │
│   X-AgentLedger-Signature: sha256={hmac}                      │
│   Signed message: "{timestamp}.{canonical_json_payload}"      │
│   Optional: only active when secret is configured             │
│                                                                 │
│ Idempotency                                                    │
│   approve: already approved → return existing JWT              │
│   deny: already denied → return existing denial response       │
│   approve → denied/expired: 409 Conflict                      │
│                                                                 │
│ Events fired (in order)                                        │
│   sessions.py:  authorization.pending (webhook)               │
│   auth.py:      authorization.approved/denied (webhook)       │
│   crawl_events: authorization_approved/denied (DB log)        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 18 — The Forms** covers `api/models/identity.py` and `api/routers/identity.py`: the Pydantic request/response schemas for the full Layer 2 API surface (13 endpoints across 3 auth tiers), and how FastAPI dependency injection connects the identity service layer to the HTTP boundary.
