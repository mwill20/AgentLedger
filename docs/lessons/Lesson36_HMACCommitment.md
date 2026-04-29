# Lesson 36: The Safe Deposit Box — HMAC Commitment Scheme

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_disclosure.py` (lines 34–134), `api/models/context.py`  
**Prerequisites:** Lesson 34  
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

A safe deposit box works like this: the bank seals your valuables and gives you a receipt. The receipt proves the box exists and who sealed it. Later, you bring both your key and the receipt, and only then does the bank open the box and hand you the contents.

The HMAC commitment scheme in Layer 4 is that safe deposit box. During the match phase, high-sensitivity field values are **committed to** — a cryptographic hash is generated that proves the value exists and won't change — but the value itself is not disclosed. Later, during the disclose phase, the nonce (your key) is released, and the service can open the box and verify the value.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain what a commitment scheme proves and what it does not reveal
- Recite the `generate_commitment()` algorithm from memory
- Implement `verify_commitment()` from scratch given the spec
- Explain why HMAC-SHA256 with a random nonce is used instead of a plain SHA-256 hash
- Describe the commitment TTL (5 minutes) and what happens to expired commitments
- Identify what is stored in `context_commitments` and what is not

---

## What Is a Commitment Scheme?

A commitment scheme has two properties:

1. **Hiding:** Knowing the commitment tells you nothing about the value.
2. **Binding:** Once committed, you cannot change the value and still produce the same commitment.

The classic broken example: hashing the value directly. `sha256("Alice")` = a fixed hash. If a service knows the user's name might be "Alice" or "Bob", they can hash both and compare to the commitment — the scheme is not hiding.

The fix: add a **random nonce** to the hash. `hmac(nonce, value)` where `nonce` is a 256-bit random secret. Without the nonce, the service cannot verify or brute-force the commitment.

---

## The Implementation

### `generate_commitment()` (lines 34–42)

```python
import hashlib
import hmac
import secrets

def generate_commitment(field_value: str) -> tuple[str, str]:
    nonce = secrets.token_hex(32)        # 256-bit cryptographically random nonce
    commitment = hmac.new(
        key=nonce.encode(),
        msg=field_value.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return commitment, nonce
```

**Why `secrets.token_hex(32)` and not `random.randbytes(32)`?**  
`secrets` uses the OS cryptographic random source (`/dev/urandom` on Linux). `random` uses a deterministic PRNG seeded from system time — predictable if the attacker knows the seed. For a nonce that protects user data, the OS source is mandatory.

**Why `hmac.new()` instead of `hashlib.sha256(nonce + value)`?**  
Length-extension attacks. SHA-256 is vulnerable: given `sha256(secret || message)`, an attacker can compute `sha256(secret || message || extension)` without knowing `secret`. HMAC explicitly prevents this by using a two-pass construction internally. In practice, the nonce is not secret once released — but building the commitment correctly from the start is the right habit.

### `verify_commitment()` (lines 45–56)

```python
def verify_commitment(commitment_hash: str, nonce: str, field_value: str) -> bool:
    expected = hmac.new(
        key=nonce.encode(),
        msg=field_value.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(commitment_hash, expected)
```

**Why `hmac.compare_digest()` instead of `==`?**  
Timing attacks. A naive `==` on strings short-circuits at the first differing character — an attacker can measure response time differences to determine how many leading characters match and reconstruct the hash byte-by-byte. `hmac.compare_digest()` always takes the same time regardless of how many characters match. This is a standard security precaution even when the commitment is not a secret.

---

## Creating Commitments: `create_commitments()` (lines 59–134)

For each field with verdict `'commit'` from the matching engine:

```python
for field_name in committed_fields:
    field_value = field_values[field_name]      # from the match request
    commitment_hash, nonce = generate_commitment(field_value)

    await db.execute(
        """
        INSERT INTO context_commitments (
            match_id, agent_did, service_id, session_assertion_id,
            field_name, commitment_hash, nonce,
            nonce_released, expires_at,
            fields_requested, fields_permitted, fields_withheld, fields_committed
        ) VALUES (...)
        """,
        {
            "match_id": match_id,
            "commitment_hash": commitment_hash,
            "nonce": nonce,           # stored server-side until disclose
            "nonce_released": False,
            "expires_at": now + timedelta(seconds=300),  # 5-minute TTL
            ...
        }
    )
```

**The nonce is stored server-side.** The match response returns only the `commitment_hash` and a `commitment_id` (the row UUID). The nonce is released later during the disclose phase by setting `nonce_released=true` and returning the nonce value to the agent.

**Why store the nonce in the database?**  
The server is the trusted party holding the nonce on behalf of the agent. When the agent calls `/disclose`, the server looks up the nonce and releases it. This works because AgentLedger is the trust infrastructure — the nonce must be re-verifiable if the agent disputes what was committed.

---

## The Commitment Row

```
context_commitments
├── id              — the commitment_id returned to the caller
├── match_id        — groups all commitments from one match request
├── agent_did       — who the commitment belongs to
├── service_id      — which service requested the field
├── field_name      — e.g., "user.dob"
├── commitment_hash — HMAC-SHA256(nonce, field_value)
├── nonce           — stored server-side, released on disclose
├── nonce_released  — false until POST /context/disclose runs
├── nonce_released_at — timestamp of release (or null)
├── expires_at      — NOW() + 5 minutes
└── fields_*        — snapshot of the full match classification
```

The `fields_*` columns (`fields_requested`, `fields_permitted`, `fields_withheld`, `fields_committed`) store the full classification snapshot from the match. This means the disclose phase can reconstruct the match result from the commitment row without relying on the Redis cache still being alive.

---

## TTL and Expiry

Commitments expire 5 minutes after creation. After expiry:
- The commitment row remains in the database (never deleted — audit trail)
- The disclose phase checks `expires_at > NOW()` before releasing nonces
- An expired commitment raises 410 Gone

**Why 5 minutes?**  
Long enough for a human or automated agent to review the match result and call disclose. Short enough that a compromised nonce does not remain usable indefinitely. The 5-minute window is a design choice — it can be extended for workflows that require more review time, but shorter is safer.

---

## What the Service Does With the Commitment

The match response includes `commitment_ids` — UUIDs referencing the commitment rows. The service does not receive the nonce or the field value at match time. After the agent calls `/disclose` and receives the nonce, the service can call `verify_commitment(commitment_hash, nonce, actual_field_value)` to confirm the value matches what was committed.

This is the binding property in action: the field value cannot be changed between commit and disclose. If the agent tries to pass a different value at disclose time, `verify_commitment()` will return `False`.

---

## Exercise 1 — Implement and Test `verify_commitment` in a REPL

```python
import hashlib, hmac, secrets

def generate_commitment(field_value):
    nonce = secrets.token_hex(32)
    commitment = hmac.new(
        key=nonce.encode(), msg=field_value.encode(), digestmod=hashlib.sha256
    ).hexdigest()
    return commitment, nonce

def verify_commitment(commitment_hash, nonce, field_value):
    expected = hmac.new(
        key=nonce.encode(), msg=field_value.encode(), digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(commitment_hash, expected)

# Commit a value
commitment, nonce = generate_commitment("1990-03-15")
print("Commitment:", commitment)
print("Verify correct value:", verify_commitment(commitment, nonce, "1990-03-15"))
print("Verify wrong value:  ", verify_commitment(commitment, nonce, "1990-03-16"))
print("Verify wrong nonce:  ", verify_commitment(commitment, secrets.token_hex(32), "1990-03-15"))
```

**Expected:** True, False, False.

---

## Exercise 2 — Read the Commitment Row

After a match that produces committed fields, inspect the `context_commitments` table:

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT id, field_name, commitment_hash, nonce_released, expires_at FROM context_commitments ORDER BY created_at DESC LIMIT 5;"
```

Confirm: `nonce_released=false`, `expires_at` is ~5 minutes in the future.

---

## Exercise 3 — Observe Expiry Behaviour

Create a commitment. Wait 5 minutes (or temporarily set the TTL very short for testing). Then call `/context/disclose` with the expired `commitment_id`.

**Expected:** 410 Gone with a detail message indicating the commitment has expired.

---

## Recommended (Not Implemented Here)

The current scheme is **HMAC-SHA256 v0.1**. The spec defers full **zero-knowledge proofs** (circom/snark.js) to v0.2. A ZKP would allow the agent to prove properties of the field value — e.g., "my birth year is before 2000" — without revealing the value at all. The HMAC scheme requires the field value to be revealed to the server during the match (to compute the commitment). A ZKP commitment would never require revealing the value to the server.

---

## Interview Q&A

**Q: Why is the nonce stored server-side rather than given to the agent at match time?**  
A: If the nonce were given to the agent at match time, the agent could release it to the service immediately — bypassing the disclose phase entirely and eliminating the final trust re-verification step. Storing the nonce server-side enforces the two-phase flow.

**Q: What does `hmac.compare_digest()` protect against?**  
A: Timing attacks. A variable-time comparison leaks information about how many characters match. Constant-time comparison removes the timing signal.

**Q: What happens if the same field value is committed twice in the same match?**  
A: Two separate nonces are generated, producing two different commitment hashes. The binding and hiding properties still hold for each independently. This is intentional — commitment uniqueness is per `(match_id, field_name)`, enforced by the `UNIQUE(match_id, field_name)` index.

---

## Key Takeaways

- `generate_commitment(value)` = HMAC-SHA256(random 256-bit nonce, value)
- Nonce is stored server-side; commitment hash is returned to the caller
- `verify_commitment(hash, nonce, value)` uses `hmac.compare_digest()` — constant-time
- 5-minute TTL; expired commitments cannot be disclosed
- The `fields_*` snapshot in each row allows disclose to work even if the Redis cache expires
- v0.1 uses HMAC; ZKP deferred to v0.2

---

## Next Lesson

**Lesson 37 — The Key Handoff: Selective Disclosure & Nonce Release** traces the full `disclose_context()` flow — loading the match snapshot, re-checking trust, releasing nonces, and writing the append-only audit record.
