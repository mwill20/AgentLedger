# Lesson 14 — The Enrollment Office: Agent Identity Registration & Revocation

**Layer:** 2 — Identity & Credentials  
**File:** `api/services/identity.py` (609 lines)  
**Prerequisites:** Lesson 13 (Credential Issuance — the JWT issued here is the output of `issue_agent_credential`)  
**Estimated time:** 90 minutes

---

## Welcome

A government enrollment office does several things before issuing an ID: it checks that your proof documents are genuine, verifies your identity hasn't already been registered, confirms you submitted a fresh application (not a replay of an old one), and then creates your official record — simultaneously issuing your ID and filing it in the registry.

`identity.py` is that enrollment office. It's the thickest service in Layer 2: 609 lines covering registration, verification, authentication, lookup, and revocation. By the end of this lesson you will be able to:

- Explain the five-step registration proof protocol that guards against spoofed registrations and replayed requests
- Describe the two-tier Redis revocation cache — why it uses both a SET and individual keys
- Trace `register_agent()` from incoming HTTP request to committed database row
- Explain why `revoke_agent()` writes to two tables simultaneously
- Distinguish `verify_agent_online()` (status check) from `authenticate_agent_credential()` (Bearer auth)
- Explain `prewarm_revocation_set()` and why it matters on service startup

---

## What This Connects To

**Previous lesson (Lesson 13):** `credentials.py` issues the JWT VC. `identity.py` calls `issue_agent_credential()` as part of the registration flow and stores the resulting token hash.

**Next lesson (Lesson 15):** `sessions.py` calls `authenticate_agent_credential()` to validate the Bearer credential before creating a session request. The `capability_scope` and `risk_tier` stored here flow into session-level authorization decisions.

**Lesson 19:** The background worker `expire_identity_records.py` queries `agent_identities.credential_expires_at` to find and deactivate expired credentials without operator intervention.

---

## Architecture Position

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Layer 2 — Identity                           │
│                                                                      │
│  credentials.py → issue_agent_credential()                          │
│  did.py         → extract_public_jwk_from_did_document()            │
│  identity.py    → register, verify, authenticate, revoke            │  ← here
│                                                                      │
│         ┌──────────────┐     ┌──────────────────────────────────┐   │
│         │  PostgreSQL  │     │             Redis                 │   │
│         │  agent_      │     │  identity:revoked_set  (SET)     │   │
│         │  identities  │     │  identity:revoked:{sha} (string) │   │
│         │  revocation_ │     │  identity:proof:{sha}  (string)  │   │
│         │  events      │     │                                  │   │
│         └──────────────┘     └──────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Core Concepts

### The Registration Proof

An agent cannot just claim a DID and ask for a credential. If it could, any party could register any DID and obtain a credential for it. The registration endpoint requires a **proof** — a cryptographic assertion that the registering party controls the private key that corresponds to the claimed DID.

The proof has three fields:
- `signature` — Ed25519 signature over the canonical registration payload
- `nonce` — a random string that prevents replay
- `created_at` — the timestamp when the proof was created, which constrains the replay window

The server performs five checks in sequence before issuing any credential:

```
1. Proof freshness (timestamp within allowed window)
2. DID document validity (extract public key from submitted document)
3. DID cross-check (re-derive the DID from the key; must match submitted DID)
4. Signature verification (verify the proof signature with the extracted key)
5. Nonce replay protection (Redis SET NX; duplicate nonce rejected)
```

Only after all five pass does the server call `issue_agent_credential()`.

### The Two-Tier Revocation Cache

Checking revocation is on the critical path of every authenticated request. A direct database query for every API call under load would be prohibitive. AgentLedger uses two Redis structures for different query shapes:

| Structure | Redis command | Purpose |
|---|---|---|
| `identity:revoked_set` (SET) | `SISMEMBER` | Fast O(1) membership check — is this DID in the revocation set at all? |
| `identity:revoked:{sha}` (string) | `GET` | Per-DID metadata (revoked_at, reason_code) for the rare case when you need detail |

**Why two structures?** `SISMEMBER` on a non-existent SET returns `0` — which means "not revoked." You don't need a separate `EXISTS` check. The SET handles the common case (most DIDs are not revoked) with a single round trip. The per-DID string key carries the metadata payload for the uncommon "is revoked — what does the record say?" path.

### The `agent_identities` Table vs. `revocation_events`

When an agent is revoked:
1. `agent_identities` is **updated** in place: `is_revoked=true, is_active=false, revoked_at=NOW()`
2. `revocation_events` gets a new **insert**: immutable audit record with reason, evidence, and revoker

The update-in-place makes status lookups fast (one row read). The insert-to-events makes audit history complete (the sequence of revocations is preserved). If you only had the update, you'd have a current state but no history. If you only had the insert, every status check would require an aggregation query.

---

## Code Walkthrough

### 1. Runtime Guard (lines 29–38)

```python
def _require_identity_runtime() -> None:
    try:
        credentials.ensure_jwt_available()
        credentials.load_issuer_private_jwk()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
```

This guard wraps the two most common startup failures — missing PyJWT and unconfigured `ISSUER_PRIVATE_JWK` — into `503 Service Unavailable`. The `503` is the correct HTTP status: the server understands the request but cannot serve it due to a configuration problem, and the problem is temporary (it resolves when the config is fixed).

Every public function that needs the runtime calls this first, so the error surfaces at the API boundary rather than deep inside a service call.

### 2. The Canonical Proof Payload (lines 41–52)

```python
def _proof_payload(request: AgentRegistrationRequest) -> dict[str, Any]:
    return {
        "did": request.did,
        "did_document": request.did_document,
        "agent_name": request.agent_name,
        "issuing_platform": request.issuing_platform,
        "capability_scope": request.capability_scope,
        "risk_tier": request.risk_tier,
        "nonce": request.proof.nonce,
        "created_at": request.proof.created_at.astimezone(timezone.utc).isoformat(),
    }
```

The proof signature is over this exact dict (canonical JSON serialized by `sign_json` in `crypto.py`). Every field of the registration request is included: changing *any* field after signing would invalidate the proof. Note `.astimezone(timezone.utc).isoformat()` on line 51 — this normalizes timezone offsets so that `+05:30` and `+00:00` representations of the same instant produce the same canonical bytes.

### 3. Redis Cache Keys (lines 55–61, 64–82)

```python
_REVOCATION_SET_KEY = "identity:revoked_set"
_REVOCATION_SET_TTL = 300  # 5 minutes

def _revocation_cache_key(did_value: str) -> str:
    return "identity:revoked:" + sha256(did_value.encode("utf-8")).hexdigest()
```

The per-DID cache key hashes the DID string with SHA-256. This keeps the Redis key at a fixed 32-byte length regardless of how long the DID is, and avoids exposing raw DID strings as Redis key names (relevant for multi-tenant setups where Redis key namespaces might be visible in logs).

**Nonce replay protection (lines 64–82):**

```python
async def _store_proof_nonce(redis, did_value: str, nonce: str) -> None:
    key = "identity:proof:" + sha256(f"{did_value}:{nonce}".encode()).hexdigest()
    stored = await redis.set(key, "1", ex=settings.proof_nonce_ttl_seconds, nx=True)
    if stored is False:
        raise HTTPException(422, "proof nonce has already been used")
```

`SET ... NX` (set if not exists) is Redis's atomic compare-and-set for this pattern. If the key doesn't exist, the set succeeds and `stored` is `True`. If it already exists (duplicate nonce), the set fails and `stored` is `False` (not `None`). The combination of `NX=True` and a TTL equal to `proof_nonce_ttl_seconds` means a nonce can only be used once within the freshness window — and then the key expires automatically.

### 4. The `prewarm_revocation_set` Function (lines 85–108)

```python
async def prewarm_revocation_set(db: AsyncSession, redis) -> int:
    result = await db.execute(
        text("SELECT did FROM agent_identities WHERE is_revoked = true")
    )
    rows = result.scalars().all()
    if not rows:
        return 0
    pipe = redis.pipeline()
    pipe.delete(_REVOCATION_SET_KEY)
    for did_val in rows:
        pipe.sadd(_REVOCATION_SET_KEY, did_val)
    pipe.expire(_REVOCATION_SET_KEY, _REVOCATION_SET_TTL)
    await pipe.execute()
    return len(rows)
```

This function is called at startup to seed the Redis SET from the database. Without it, the first requests after a Redis restart would miss the SET entirely (it doesn't exist yet) and fall through to individual cache checks or database queries.

The **pipeline** (`redis.pipeline()`) batches all the `SADD` commands and executes them in a single network round trip, regardless of how many revoked DIDs exist. `pipe.delete()` first clears any stale state, ensuring the SET reflects the current database snapshot.

### 5. The Full Registration Flow (lines 202–334)

This is the longest function in the file. Walk through it in sequence:

**Step 1 — Proof freshness (lines 210–217):**

```python
age_seconds = abs(
    (datetime.now(timezone.utc) - request.proof.created_at.astimezone(timezone.utc)).total_seconds()
)
if age_seconds > settings.proof_nonce_ttl_seconds:
    raise HTTPException(422, "proof timestamp is outside the allowed replay window")
```

The `abs()` catches both stale proofs (past) and proofs with a future timestamp (clock skew or deliberate manipulation). `proof_nonce_ttl_seconds` (typically 300 seconds) defines the allowed window.

**Step 2 — DID document extraction (lines 219–229):**

```python
public_jwk = did.extract_public_jwk_from_did_document(
    request.did_document,
    expected_did=request.did,
)
derived_did = did.did_key_from_public_jwk(public_jwk)
```

`extract_public_jwk_from_did_document` validates that the submitted DID document's `id` field matches the claimed DID, and finds the Ed25519 verification method inside it. This prevents a party from submitting a valid DID document for a different DID.

**Step 3 — DID cross-check (lines 231–235):**

```python
if derived_did != request.did:
    raise HTTPException(422, "submitted DID does not match the DID document public key")
```

The server re-derives the DID from the extracted public key using `did_key_from_public_jwk` (which performs the base58btc + multicodec encoding from Lesson 12). If the result doesn't match the submitted DID, the DID document's public key isn't consistent with the DID — the submission is malformed or fraudulent.

**Step 4 — Signature verification (lines 237–245):**

```python
if not verify_json_signature(
    payload=_proof_payload(request),
    signature=request.proof.signature,
    public_jwk=public_jwk,
):
    raise HTTPException(422, "invalid proof signature")
```

`verify_json_signature` (from `crypto.py`) reconstructs the canonical JSON of the proof payload and verifies the Ed25519 signature against the extracted public key. This proves the registrant controls the private key for the DID.

**Step 5 — Nonce replay (line 247):**

```python
await _store_proof_nonce(redis, request.did, request.proof.nonce)
```

This is the last check before any writes. If it raises (duplicate nonce), no database state has been modified.

**Issuance and DB write (lines 249–316):**

```python
credential_jwt, expires_at = credentials.issue_agent_credential(...)
credential_hash = sha256(credential_jwt.encode("utf-8")).hexdigest()

await db.execute(text("""
    INSERT INTO agent_identities (
        did, agent_name, issuing_platform, public_key_jwk,
        capability_scope, risk_tier, credential_hash, credential_expires_at,
        registered_at, is_active, is_revoked
    ) VALUES (...)
"""), {...})
await db.commit()
```

Two details worth noting:

- **`credential_hash` (line 273):** SHA-256 of the JWT string, stored in the database. This lets an operator verify, without storing the full JWT, that a credential they have in hand matches what was issued. The JWT itself is never stored — only the hash and expiry.

- **`CAST(:public_key_jwk AS JSONB)` (line 295):** The public key JWK is stored as a JSONB column, enabling JSON operators in future queries without re-parsing the string.

### 6. Online Verification vs. Authentication (lines 337–463)

These two functions look similar but serve different purposes:

| Function | Caller | Returns | On revocation |
|---|---|---|---|
| `verify_agent_online()` | `POST /identity/verify` endpoint | `CredentialVerificationResponse` (boolean result) | Returns `valid=False` |
| `authenticate_agent_credential()` | FastAPI dependency injection | `AgentCredentialPrincipal` | Raises `403 Forbidden` |

**`verify_agent_online` — three-tier revocation check (lines 350–384):**

```python
# Tier 1: Redis SET (fastest — O(1) SISMEMBER)
if await _check_revocation_set(redis, did_value):
    return CredentialVerificationResponse(valid=False, did=did_value)

# Tier 2: Per-DID cache key (detail metadata if SET is cold)
cached_revocation = await _get_cached_revocation(redis, did_value)
if cached_revocation is not None:
    return CredentialVerificationResponse(valid=False, did=did_value)

# Tier 3: Database (authoritative — always correct)
result = await db.execute(...)
```

The first `SISMEMBER` check resolves in sub-millisecond time for the common case. Only if Redis is unavailable or the SET TTL has expired does the query fall through to the database.

**`authenticate_agent_credential` — Bearer auth flow (lines 387–463):**

After verifying the JWT and checking revocation, it calls:

```python
await db.execute(
    text("UPDATE agent_identities SET last_seen_at = NOW() WHERE did = :did"),
    {"did": did_value},
)
```

The `last_seen_at` update happens on every successful authentication. This is used by the background worker in Lesson 19 to distinguish genuinely inactive agents from recently active ones during cleanup sweeps.

### 7. Revocation (lines 512–608)

```python
async def revoke_agent(db, did_value, request, revoked_by, redis=None):
    # 1. Fetch existing state
    # 2. If not already revoked: UPDATE agent_identities + INSERT revocation_events
    # 3. Refresh revoked_at from DB
    # 4. Update Redis cache (SET + per-DID key)
```

The `if not row["is_revoked"]` guard (line 540) makes revocation idempotent — a second revocation request for the same DID still returns a valid response, just without writing to the database again. This is important for operator tooling: a retry should not fail.

The dual DB write (lines 541–581) is wrapped in one transaction block. If the `revocation_events` insert fails, the `agent_identities` update rolls back — there's no half-revoked state.

The Redis update (lines 595–602) happens **after** `db.commit()`:

```python
await _cache_revocation(redis, did_value=did_value, revoked_at=revoked_at, ...)
await _add_to_revocation_set(redis, did_value)
```

This order is correct: the source of truth is committed to the database first. If the Redis write fails, the next verification request will fall through to the database (which is already correct), and the cache will be populated on that miss. There's no window where the database says "not revoked" but Redis says "revoked."

---

## Exercises

### Exercise 1 — Full registration via API

```bash
# Generate a DID:key and registration proof in the Python REPL
docker compose run --rm api python

# In the REPL:
from api.services.crypto import generate_ed25519_keypair, sign_json
from api.services.did import did_key_from_public_jwk, build_did_document
import json, datetime

private_jwk, public_jwk = generate_ed25519_keypair()
agent_did = did_key_from_public_jwk(public_jwk)
did_doc = build_did_document(did=agent_did, public_jwk=public_jwk)

nonce = "unique-nonce-123"
created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

proof_payload = {
    "did": agent_did,
    "did_document": did_doc,
    "agent_name": "test-enrollment-agent",
    "issuing_platform": "local-dev",
    "capability_scope": ["search.manifests"],
    "risk_tier": "low",
    "nonce": nonce,
    "created_at": created_at,
}

signature = sign_json(proof_payload, private_jwk)
print("DID:", agent_did)
print("Signature:", signature[:40], "...")
```

Now call the registration endpoint:

```bash
# Replace <DID>, <DID_DOCUMENT>, <SIGNATURE>, <NONCE>, <CREATED_AT>
# with the values from the REPL above
curl -s -X POST http://localhost:8000/v1/identity/register \
  -H "Content-Type: application/json" \
  -d '{
    "did": "<DID>",
    "did_document": <DID_DOCUMENT>,
    "agent_name": "test-enrollment-agent",
    "issuing_platform": "local-dev",
    "capability_scope": ["search.manifests"],
    "risk_tier": "low",
    "proof": {
      "signature": "<SIGNATURE>",
      "nonce": "<NONCE>",
      "created_at": "<CREATED_AT>"
    }
  }' | python -m json.tool
```

Expected output (abbreviated):
```json
{
  "did": "did:key:z6Mk...",
  "credential_jwt": "eyJhbGci...",
  "credential_expires_at": "2027-04-27T...",
  "issuer_did": "did:web:agentledger.example.com"
}
```

### Exercise 2 — Verify and then revoke

```bash
# Verify the credential (use the JWT from Exercise 1)
curl -s -X POST http://localhost:8000/v1/identity/verify \
  -H "Content-Type: application/json" \
  -d '{"credential_jwt": "<JWT>"}' | python -m json.tool

# Expected: {"valid": true, "did": "did:key:..."}

# Now revoke (requires API key auth — replace <KEY> and <DID>)
curl -s -X DELETE "http://localhost:8000/v1/identity/agents/<DID>/revoke" \
  -H "X-API-Key: <KEY>" \
  -H "Content-Type: application/json" \
  -d '{"reason_code": "key_compromise", "evidence": {"source": "operator"}}' \
  | python -m json.tool

# Verify again — expect valid=false
curl -s -X POST http://localhost:8000/v1/identity/verify \
  -H "Content-Type: application/json" \
  -d '{"credential_jwt": "<JWT>"}' | python -m json.tool
```

Expected output after revocation:
```json
{"valid": false, "did": "did:key:..."}
```

### Exercise 3 — Inspect the revocation cache in Redis

```bash
docker compose exec redis redis-cli

# Check SET membership
SISMEMBER identity:revoked_set "did:key:z6Mk..."

# Check per-DID key (replace <sha256_of_did> with actual hash)
# Compute sha256 in Python: import hashlib; hashlib.sha256(did.encode()).hexdigest()
GET "identity:revoked:<sha256>"
```

Expected output:
```
(integer) 1
{"did": "did:key:z6Mk...", "revoked_at": "2026-04-27T12:00:00+00:00", "reason_code": "key_compromise"}
```

### Exercise 4 (failure) — Replay a proof

```bash
# Repeat the same registration request from Exercise 1, verbatim
curl -s -X POST http://localhost:8000/v1/identity/register \
  -H "Content-Type: application/json" \
  -d '{ ... same body as Exercise 1 ... }' | python -m json.tool
```

Expected output (first attempt after initial registration — different DID but same nonce reused too quickly):
```json
{"detail": "proof nonce has already been used"}
```

Or if the nonce was never used but the DID was already registered:
```json
{"detail": "agent DID is already registered"}
```

---

## Best Practices

### What AgentLedger does

- **5-step registration proof** — timestamp freshness, DID doc extraction, DID cross-check, signature verify, nonce replay — blocks every trivial registration attack
- **Two-tier Redis revocation cache** — SET for fast membership check, per-DID string for metadata; fallback to DB on miss
- **Dual DB write on revocation** — `agent_identities` (status) + `revocation_events` (audit trail)
- **`credential_hash` not credential_jwt** — stores the hash of the issued JWT, not the JWT itself; the credential is delivered to the agent and not retained server-side
- **Redis writes after `db.commit()`** — the database is the source of truth; cache writes are best-effort

### Recommended (not implemented here)

- **Revocation propagation to Layer 3** — currently the Layer 2 agent revocation only marks the identity as inactive. A full implementation would also propagate to the federation blocklist for cross-registry enforcement.
- **Nonce replay without Redis** — if Redis is unavailable, `_store_proof_nonce` silently passes. A degraded-mode fallback to a short-lived in-memory bloom filter would maintain nonce protection without Redis.
- **`prewarm_revocation_set` on every Redis reconnect** — currently called at startup. Redis eviction or crash between startup and the next prewarm interval could leave the SET stale. An `on_reconnect` hook would close this gap.

---

## Interview Q&A

**Q: Why does registration require a proof of key control rather than just a DID string?**

A: Without key control proof, any party could register any DID and receive a credential for it — including DIDs belonging to legitimate agents. The Ed25519 proof signature proves the registrant controls the private key corresponding to the submitted DID. Even if someone intercepts the DID string, they cannot register it without the private key.

**Q: What does `credential_hash` protect against?**

A: `credential_hash` is SHA-256 of the issued JWT. An operator can later take any credential they have in hand, hash it, and compare against the database — confirming that AgentLedger issued it as-is and that it hasn't been modified. The JWT itself is not stored server-side, which limits the exposure if the database is breached: an attacker gets public metadata but not bearer tokens.

**Q: Why are there two Redis structures for revocation rather than just one?**

A: The SET (`SISMEMBER`) is optimized for the binary "is this DID revoked?" question — it returns 0 or 1 in a single round trip for any SET size. The per-DID string key is optimized for "what does the revocation record say?" — it carries metadata without loading the full SET. Combining them handles both the common hot path (membership check) and the rarer detail path without overfitting either data structure.

**Q: What happens if the Redis write at the end of `revoke_agent` fails?**

A: The revocation is already committed to the database. The next request that hits the verification path will fall through the empty cache to the database (which correctly returns `is_revoked=true`), and the cache will be populated on that miss. There's no inconsistency window: the database is always authoritative, and the cache is an acceleration layer that can be rebuilt from the database at any time.

**Q: Why does `authenticate_agent_credential` update `last_seen_at` on every successful auth?**

A: The background worker in Lesson 19 uses `last_seen_at` to identify genuinely inactive credentials during cleanup. If it only had `registered_at`, it couldn't distinguish an active agent that registered long ago from one that hasn't been seen in months. `last_seen_at` keeps the distinction current without requiring a separate activity table.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 14 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ Registration proof (5 steps in order)                          │
│   1. Timestamp freshness  →  age_seconds <= proof_nonce_ttl    │
│   2. DID doc extraction   →  extract_public_jwk_from_did_doc   │
│   3. DID cross-check      →  derived_did == request.did        │
│   4. Signature verify     →  verify_json_signature(proof)      │
│   5. Nonce replay         →  SET NX in Redis                   │
│                                                                 │
│ Redis revocation cache                                         │
│   identity:revoked_set     →  SISMEMBER for fast O(1) check   │
│   identity:revoked:{sha}   →  GET for metadata detail         │
│   identity:proof:{sha}     →  SET NX for nonce replay         │
│                                                                 │
│ Revocation dual write                                          │
│   agent_identities: UPDATE (is_revoked, is_active, revoked_at) │
│   revocation_events: INSERT (immutable audit record)           │
│   Redis: SADD + SET (cache update — after db.commit())         │
│                                                                 │
│ Verification vs. Authentication                                │
│   verify_agent_online()      →  boolean CredentialVerif.      │
│   authenticate_agent_cred()  →  AgentCredentialPrincipal      │
│                               (raises on revoked/inactive)    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 15 — The Day Pass** covers `api/services/sessions.py`: how agents request access to a specific service, how the session assertion JWT from `credentials.py` is managed through a request/approval lifecycle, and how `jti` is used for one-use enforcement.
