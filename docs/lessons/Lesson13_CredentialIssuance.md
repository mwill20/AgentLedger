# Lesson 13 — The Notary: Credential Issuance & Verification

> **Beginner frame:** Credential issuance is a notary stamp for machine-readable claims. AgentLedger signs identity and session facts so another part of the system can verify them later without re-running the original enrollment flow.

**Layer:** 2 — Identity & Credentials  
**File:** `api/services/credentials.py` (177 lines)  
**Prerequisites:** Lesson 12 (DID Methods — you need to understand `did:key` and JWK format before the claims make sense)  
**Estimated time:** 75 minutes

---

## Welcome

A notary's job is precise: they verify an identity, witness a signature, and issue a stamped document that any third party can verify as authentic. The notary's seal is not magic — it works because the seal itself is hard to forge, and anyone who knows what a legitimate seal looks like can check it independently.

AgentLedger plays the same role. When an agent registers, the platform issues a **Verifiable Credential (VC)** — a JSON object, signed with Ed25519, formatted as a JWT. Any downstream service that holds the issuer's public key can verify that credential without calling AgentLedger. That independence is the whole point: trust that doesn't require an API call to a central authority.

By the end of this lesson you will be able to:

- Explain the W3C Verifiable Credentials data model and how it maps to JWT claims
- Trace `issue_agent_credential()` from key load to signed token
- Describe why `_cached_public_key` is module-level state (not a class) and what failure mode it prevents
- Explain why `verify_agent_credential_async` uses `run_in_executor` instead of running inline
- Distinguish a **Verifiable Credential** (365-day identity proof) from a **session assertion** (5-minute access token)
- Independently verify any AgentLedger-issued JWT using only the issuer's public key

---

## What This Connects To

**Previous lesson (Lesson 12):** `did.py` gave every agent a stable, self-describing identifier. That DID becomes the `sub` claim of the credential issued here.

**Next lesson (Lesson 14):** `identity.py` calls `issue_agent_credential()` as part of the agent registration flow, then stores the resulting token and its expiry in PostgreSQL.

**Lessons 15–17:** The session assertion issued at the bottom of `credentials.py` is the access token that `sessions.py` (Lesson 15) manages throughout the request/approval lifecycle.

---

## Architecture Position

```
┌──────────────────────────────────────────────────────────┐
│                     Layer 2 — Identity                   │
│                                                          │
│  did.py        → builds agent's DID                     │
│  crypto.py     → Ed25519 sign/verify                    │
│  credentials.py → JWT VC issuance + session assertions  │  ← you are here
│  identity.py   → registration, storage, revocation      │
│  sessions.py   → session request, approval, redemption  │
└──────────────────────────────────────────────────────────┘
         ▲                         ▲
    ISSUER_PRIVATE_JWK       PyJWT + cryptography
    (settings / env var)     (optional deps)
```

---

## Core Concepts

### The W3C Verifiable Credentials Data Model

The [W3C VC specification](https://www.w3.org/TR/vc-data-model/) defines a portable format for digital credentials. The key idea: separate the **who** (DID), the **what** (credential claims), and the **how** (cryptographic proof). AgentLedger implements a compact VC subset using JWT encoding, which every modern HTTP library already knows how to handle.

A VC has three layers:

```
JWT envelope (signed)
├── Standard JWT claims: iss, sub, jti, iat, nbf, exp
└── vc claim (W3C VC body)
    └── credentialSubject
        ├── id           (same as sub — the agent's DID)
        ├── agent_name
        ├── capability_scope
        └── risk_tier
```

The JWT envelope handles identity, timing, and uniqueness. The `vc` block carries the semantic payload. Splitting them this way lets you verify the signature with any standard JWT library, then inspect the VC payload for business logic.

### The Session Assertion vs. the Verifiable Credential

| Property | Verifiable Credential | Session Assertion |
|---|---|---|
| Lifetime | 365 days (configurable) | 5 minutes (default) |
| Audience (`aud`) | absent — issuer-scoped | specific `service_did` |
| Purpose | proves who you are | proves you're allowed to do this now |
| Used by | any downstream verifier | one specific service |
| One-use? | No — reusable identity proof | Yes — `jti` burned on redemption |

Think of the VC as a passport: issued once, valid for years. The session assertion is a day pass to a specific building: issued on demand, expires in minutes, one entry only.

### Why Optional Imports?

```python
# credentials.py, lines 9-12
try:
    import jwt
except ImportError:
    jwt = None
```

The Layer 1 app ships without PyJWT. This guard lets the entire `api` package import successfully even when the optional Layer 2 dependency is missing. The function `ensure_jwt_available()` (line 19) turns a confusing `AttributeError: 'NoneType' object...` into a clear `RuntimeError: Layer 2 JWT dependency is unavailable`. This pattern appears in `crypto.py` too — it is a deliberate Layer 1/2 isolation strategy, not an oversight.

---

## Code Walkthrough

### 1. Dependency Guard (lines 19–22)

```python
def ensure_jwt_available() -> None:
    if jwt is None:
        raise RuntimeError("Layer 2 JWT dependency is unavailable; install PyJWT")
```

Every public function in this module calls this first. The alternative — checking `if jwt is None` at each call site — would scatter the same guard across six functions. A single call is cleaner and the error message is explicit.

### 2. Key Loading (lines 25–40)

```python
def load_issuer_private_jwk() -> dict[str, str]:
    if not settings.issuer_private_jwk.strip():
        raise RuntimeError("ISSUER_PRIVATE_JWK is not configured")
    value = json.loads(settings.issuer_private_jwk)
    ...
    return value

def load_issuer_public_jwk() -> dict[str, str]:
    return public_jwk_from_private_jwk(load_issuer_private_jwk())
```

`ISSUER_PRIVATE_JWK` is the platform's root signing key stored as a JSON string in environment config. Loading it from the environment on every call seems expensive — but Python's `json.loads` on a ~200-character string takes under a microsecond. The only case where this matters is the hot verification path, which uses a different mechanism (the module-level cache described below).

`load_issuer_public_jwk()` delegates entirely to `public_jwk_from_private_jwk` in `crypto.py`, which strips the `d` field from the JWK dict. This means the public key is always derived from the authoritative private key — there is no separate stored public key that could drift out of sync.

### 3. Issuing a Verifiable Credential (lines 48–81)

This is the platform's main issuance function. Read it top to bottom:

```python
def issue_agent_credential(
    subject_did: str,
    agent_name: str,
    issuing_platform: str | None,
    capability_scope: list[str],
    risk_tier: str,
) -> tuple[str, datetime]:
```

**Returns a tuple:** `(jwt_string, expires_at_datetime)`. The caller (in `identity.py`) stores both — the token goes to the agent, the expiry goes to the database so background workers can find expired credentials.

**The claims block (lines 62–79):**

```python
claims = {
    "iss": settings.issuer_did,       # AgentLedger's DID — the notary's identity
    "sub": subject_did,                # agent's DID — who this credential belongs to
    "jti": str(uuid4()),               # unique credential ID — enables revocation by ID
    "iat": int(now.timestamp()),       # issued-at (Unix seconds)
    "nbf": int(now.timestamp()),       # not-before == issued-at (valid immediately)
    "exp": int(expires_at.timestamp()),# expiry (Unix seconds)
    "vc": {
        "type": ["VerifiableCredential", "AgentIdentityCredential"],
        "credentialSubject": {
            "id": subject_did,         # must match "sub" — verified at decode time
            "agent_name": agent_name,
            "issuing_platform": issuing_platform,
            "capability_scope": capability_scope,
            "risk_tier": risk_tier,
        },
    },
}
```

Why is `"id": subject_did` repeated inside `credentialSubject` when `sub` already holds it? The W3C VC specification requires `credentialSubject.id` to identify the subject of the credential claims. `sub` is the JWT specification's way of saying the same thing. Both must be present for the token to be valid in both ecosystems — and the verifier checks they match (line 108).

**The signing line (line 80):**

```python
token = jwt.encode(claims, private_key, algorithm="EdDSA")
```

`private_key` here is a `cryptography` library `Ed25519PrivateKey` object, not the raw bytes. PyJWT accepts it directly. `algorithm="EdDSA"` tells PyJWT to use the Edwards-curve Digital Signature Algorithm with SHA-512 (RFC 8037). The resulting token is three base64url-encoded segments joined by dots: `header.claims.signature`.

### 4. The Module-Level Public Key Cache (lines 84–94)

```python
_cached_public_key = None

def _get_public_key():
    global _cached_public_key
    if _cached_public_key is None:
        _cached_public_key = load_private_key_from_jwk(
            load_issuer_private_jwk()
        ).public_key()
    return _cached_public_key
```

**Why module-level, not a class or functools.lru_cache?**

Module-level state lives for the lifetime of the Python process. In production, uvicorn runs multiple workers (each a separate process). Each worker caches one `Ed25519PublicKey` object after its first verification call. There is no synchronization overhead, no class instantiation, and no cache key to construct — just a `None` check.

`functools.lru_cache` would work too, but it adds a layer of indirection and requires the function signature to be hashable. A plain `global` is simpler and equally correct here.

**What does this prevent?** The key-loading path touches disk (environment variable parsing → `json.loads` → `cryptography` key derivation). Under 1000 req/s of credential verification, repeating that on every request would add measurable latency. After the first call, verification uses an in-memory key object — the hot path becomes pure elliptic curve math.

### 5. Synchronous Verification (lines 97–110)

```python
def _verify_agent_credential_sync(token: str) -> dict:
    ensure_jwt_available()
    claims = jwt.decode(
        token,
        key=_get_public_key(),
        algorithms=["EdDSA"],
        issuer=settings.issuer_did,
        options={"require": ["exp", "iat", "nbf", "iss", "sub"]},
    )
    subject = claims.get("vc", {}).get("credentialSubject", {}).get("id")
    if subject != claims.get("sub"):
        raise ValueError("credential subject id does not match sub")
    return claims
```

`jwt.decode()` does four things in one call:
1. Verifies the Ed25519 signature
2. Checks `exp` (rejects expired tokens)
3. Checks `nbf` (rejects tokens used before their valid-from time)
4. Checks `iss` matches `settings.issuer_did` (rejects tokens from other issuers)

The `"require"` option means PyJWT raises `MissingRequiredClaimError` if any of those five claims are absent — even if the signature is valid. A structurally malformed token fails fast before any business logic runs.

The final check (lines 107–109) is an **application-layer validation** that PyJWT has no opinion about: `credentialSubject.id` must equal `sub`. Without this check, an attacker who somehow obtained a valid token could modify the `vc` payload (which is not directly signed by the JWT spec in all implementations — the signature covers the encoded header + encoded payload as a whole, so this check is redundant from a cryptographic standpoint, but it guards against future serialization edge cases and makes the intent explicit in code).

### 6. Async Verification (lines 118–128)

```python
async def verify_agent_credential_async(token: str) -> dict:
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _verify_agent_credential_sync, token)
```

**Why not just `await _verify_agent_credential_sync(token)`?**

`_verify_agent_credential_sync` is not a coroutine — it's regular synchronous Python. Ed25519 signature verification involves elliptic curve point multiplication: it's CPU-bound, not I/O-bound. An `async def` that calls CPU-intensive code directly still **blocks the event loop** for the duration of that computation. Under 100 concurrent requests, that serializes all verification work through one thread.

`run_in_executor(None, ...)` submits the function to the default `ThreadPoolExecutor` and `await`s a `Future`. The event loop remains unblocked — it can service other requests while the thread pool handles the cryptography.

`None` as the first argument means "use the loop's default executor," which is a `ThreadPoolExecutor` sized to `min(32, os.cpu_count() + 4)` workers in Python 3.11. You don't need to configure it; the default is appropriate for most deployments.

**The `import asyncio` is inside the function body** — this is deliberate. The module imports cleanly in non-async contexts (tests, scripts, Layer 1 workers). The `asyncio` import only triggers when the async code path is actually reached.

### 7. Session Assertions (lines 131–161)

```python
def issue_session_assertion(
    subject_did: str,
    service_did: str,
    service_id: str,
    ontology_tag: str,
    authorization_ref: str | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, str, datetime]:
```

**Returns three values:** `(token, jti, expires_at)`. The caller stores `jti` in Redis or the database so it can be burned on first use.

**The `aud` claim (line 151):**

```python
"aud": service_did,
```

This is the crucial difference from the VC. The `aud` (audience) claim says: "this token is intended for exactly one recipient." When the service verifies this token, it should check that `aud` matches its own DID. This prevents token replay: a session assertion obtained for Service A cannot be used at Service B.

**The `jti` claim (line 152):**

```python
"jti": jti,  # jti = str(uuid4())
```

`jti` (JWT ID) is a unique identifier for this specific token. `sessions.py` stores it and marks it as "redeemed" on first use. A second request with the same `jti` is rejected — the token is one-use by design.

**The `ontology_tag` and `authorization_ref` claims (lines 157–158):**

These are non-standard claims (JWT allows them). `ontology_tag` specifies what capability the agent is invoking. `authorization_ref` links back to a human-in-the-loop approval record if one was required (Lesson 17). Downstream services can read these claims to make access decisions without a database round-trip.

### 8. Session Assertion Verification (lines 164–177)

```python
def verify_session_assertion(token: str) -> dict:
    ensure_jwt_available()
    private_key = load_private_key_from_jwk(load_issuer_private_jwk())
    return jwt.decode(
        token,
        key=private_key.public_key(),
        algorithms=["EdDSA"],
        issuer=settings.issuer_did,
        options={
            "require": ["exp", "iat", "nbf", "iss", "sub", "aud", "jti"],
            "verify_aud": False,
        },
    )
```

Note `"verify_aud": False`. The verifier here is AgentLedger itself — it issued the token and doesn't need to check that `aud` matches its own DID (it always will). The `aud` claim is present for downstream services to validate, not for the issuer.

Compare this to the VC verifier: session assertions require `aud` and `jti` in the `require` list; VCs don't need them. Each credential type enforces exactly the claims it needs.

---

## Independent Verification

The canonical test for a trustworthy credential system: can a third party verify the token without calling home?

AgentLedger provides the issuer's public key at:

```
GET /v1/identity/.well-known/did.json
```

Which returns the full DID document including the `verificationMethod` JWK. Given that, anyone can verify a credential with only the standard PyJWT library:

```python
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import base64, json

# Fetch the DID document from GET /v1/identity/.well-known/did.json
# Extract the public JWK from verificationMethod[0].publicKeyJwk
public_jwk = {
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "<x-value-from-did-doc>"
}

# Reconstruct the key
raw_bytes = base64.urlsafe_b64decode(public_jwk["x"] + "==")
public_key = Ed25519PublicKey.from_public_bytes(raw_bytes)

# Verify and decode (replace with your actual token)
claims = jwt.decode(
    token,
    key=public_key,
    algorithms=["EdDSA"],
    issuer="did:web:agentledger.example.com",  # issuer DID from config
    options={"require": ["exp", "iat", "nbf", "iss", "sub"]},
)
print(claims["vc"]["credentialSubject"])
```

This works without any knowledge of AgentLedger's internals. The only requirement is the issuer's public key — which is published in a standards-compliant DID document.

---

## Exercises

### Exercise 1 — Issue and decode a credential locally

```bash
# Start with a Python shell inside the API container
docker compose run --rm api python

# In the REPL:
import json
from api.config import settings
from api.services.credentials import issue_agent_credential

token, expires_at = issue_agent_credential(
    subject_did="did:key:z6Mk...",   # any DID:key value
    agent_name="test-agent",
    issuing_platform="local-test",
    capability_scope=["search.manifests"],
    risk_tier="low",
)
print("Token:", token[:80], "...")
print("Expires:", expires_at)

# Decode the payload (without verification, to see structure)
import base64, json
parts = token.split(".")
payload_bytes = base64.urlsafe_b64decode(parts[1] + "==")
print(json.dumps(json.loads(payload_bytes), indent=2))
```

Expected output (abbreviated):
```
Token: eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJkaWQ6d2ViOm...
Expires: 2027-04-27 12:00:00+00:00
{
  "iss": "did:web:agentledger.example.com",
  "sub": "did:key:z6Mk...",
  "jti": "3f7a1b2c-...",
  "iat": 1745748000,
  "nbf": 1745748000,
  "exp": 1777284000,
  "vc": {
    "type": ["VerifiableCredential", "AgentIdentityCredential"],
    "credentialSubject": {
      "id": "did:key:z6Mk...",
      "agent_name": "test-agent",
      ...
    }
  }
}
```

### Exercise 2 — Verify through the async path

```python
import asyncio
from api.services.credentials import issue_agent_credential, verify_agent_credential_async

async def run():
    token, _ = issue_agent_credential(
        subject_did="did:key:z6Mk...",
        agent_name="test-agent",
        issuing_platform=None,
        capability_scope=["search.manifests"],
        risk_tier="low",
    )
    claims = await verify_agent_credential_async(token)
    print("Verified. Agent name:", claims["vc"]["credentialSubject"]["agent_name"])

asyncio.run(run())
```

Expected output:
```
Verified. Agent name: test-agent
```

### Exercise 3 — Issue and verify a session assertion

```python
from api.services.credentials import issue_session_assertion, verify_session_assertion

token, jti, expires_at = issue_session_assertion(
    subject_did="did:key:z6MkAgent...",
    service_did="did:web:payment-service.example.com",
    service_id="00000000-0000-0000-0000-000000000001",
    ontology_tag="finance.payments",
    authorization_ref=None,
    ttl_seconds=300,   # 5 minutes
)

claims = verify_session_assertion(token)
print("JTI:", claims["jti"])
print("Audience:", claims["aud"])
print("Ontology:", claims["ontology_tag"])
print("Expires:", expires_at)
```

Expected output:
```
JTI: 7e2a0b4d-...
Audience: did:web:payment-service.example.com
Ontology: finance.payments
Expires: 2026-04-27 12:05:00+00:00
```

### Exercise 4 (failure) — Tamper with the token

```python
import base64, json

# From Exercise 1: take a valid token
token = "eyJhbGci..."  # paste your token

# Unpack the claims section and modify it
header_b64, claims_b64, sig_b64 = token.split(".")
claims_json = base64.urlsafe_b64decode(claims_b64 + "==").decode()
claims_dict = json.loads(claims_json)
claims_dict["vc"]["credentialSubject"]["risk_tier"] = "critical"  # tamper!

tampered_claims_b64 = base64.urlsafe_b64encode(
    json.dumps(claims_dict).encode()
).rstrip(b"=").decode()

tampered_token = f"{header_b64}.{tampered_claims_b64}.{sig_b64}"

# Try to verify it
from api.services.credentials import verify_agent_credential
verify_agent_credential(tampered_token)
```

Expected output:
```
jwt.exceptions.InvalidSignatureError: Signature verification failed
```

The signature covers the original base64-encoded header + claims, byte-for-byte. Any change to the payload — even adding a single space — produces a completely different byte sequence, and the Ed25519 verification fails.

---

## Best Practices

### What AgentLedger does

- **Module-level public key cache** — eliminates key-parsing overhead on the hot verification path
- **`run_in_executor`** — keeps the async event loop unblocked during CPU-bound crypto
- **Dual `sub`/`credentialSubject.id` validation** — guards against cross-credential substitution
- **`"require"` in jwt.decode** — rejects structurally invalid tokens before business logic
- **`ensure_jwt_available()`** — surfaces missing dependencies clearly rather than crashing with `AttributeError`

### Recommended (not implemented here)

- **Key rotation** — currently the platform has one issuer key. A production system would maintain a key history and include `kid` (key ID) in the JWT header so verifiers can fetch the correct key for tokens signed under any rotation.
- **Revocation list** — the VC lifetime is 365 days. If an agent's identity is compromised, there's no mechanism here to invalidate existing tokens before expiry. A revocation registry (e.g., a status list credential) would fill this gap.
- **`_cached_public_key` invalidation** — the cache has no TTL. If the platform's private key changes between deployments, any long-lived worker process will hold the stale public key until it is restarted. A restart-triggered cache clear (or a short TTL) would make key rotation safer.

---

## Interview Q&A

**Q: Why does AgentLedger use JWT for Verifiable Credentials instead of the full W3C JSON-LD format?**

A: JSON-LD VCs require linked-data canonicalization (RDF normalization), which is complex to implement correctly and rarely supported out-of-the-box by standard libraries. JWT encoding achieves the same trust model — signed, tamper-evident, independently verifiable — with library support in every modern HTTP stack. The W3C VC spec explicitly endorses JWT encoding as a valid packaging format.

**Q: What does `run_in_executor` actually do, and why is it needed for Ed25519 verification?**

A: `run_in_executor(None, fn, *args)` submits `fn(*args)` to the event loop's default thread pool and returns an `asyncio.Future`. `await` on that Future suspends the coroutine without blocking the event loop — other coroutines can run while the thread is doing elliptic curve math. It's needed because Ed25519 signature verification is CPU-bound: it cannot be made async by wrapping it in `async def`. Without `run_in_executor`, 100 concurrent verify requests would execute serially through one Python thread, degrading throughput under load.

**Q: What is the `jti` claim, and what attack does it prevent?**

A: `jti` (JWT ID) is a unique identifier for a specific token issuance. Combined with one-use tracking in `sessions.py`, it prevents **replay attacks**: a valid session assertion that was intercepted in transit cannot be submitted a second time, because the server checks that `jti` has not been seen before and marks it as consumed on first use. Without `jti`, any token that leaked mid-flight could be replayed indefinitely until it expired.

**Q: If a downstream service wants to verify an AgentLedger credential, what does it need?**

A: Only the issuer's public key from the DID document at `GET /v1/identity/.well-known/did.json`. The service extracts the JWK, reconstructs the `Ed25519PublicKey`, and calls `jwt.decode()` with `algorithms=["EdDSA"]`. No database, no AgentLedger API call, no shared secret. This is the defining property of a verifiable credential: verification is independent of the issuer.

**Q: Why does `verify_session_assertion` use `verify_aud=False` even though `aud` is in the `require` list?**

A: The `require` list ensures the `aud` claim is **present** in the token. `verify_aud=False` skips checking that `aud` matches the caller's own identity. AgentLedger is the issuer, not the intended audience — so checking that `aud` equals the issuer's DID would always fail. The `aud` claim is present for the downstream service to validate when it receives the assertion. AgentLedger only needs to confirm the token is structurally valid and unexpired.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 13 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ Verifiable Credential                                           │
│   Lifetime: 365 days    Algorithm: EdDSA (Ed25519)             │
│   Key claims: iss, sub, jti, iat, nbf, exp, vc{}               │
│   Issue: issue_agent_credential() → (token, expires_at)        │
│   Verify: verify_agent_credential_async() → claims dict        │
│                                                                 │
│ Session Assertion                                               │
│   Lifetime: 5 min (default)    Audience: service_did           │
│   Key claims: + aud, jti, service_id, ontology_tag             │
│   Issue: issue_session_assertion() → (token, jti, expires_at)  │
│   Verify: verify_session_assertion() → claims dict             │
│                                                                 │
│ Performance                                                     │
│   _cached_public_key: one key object per process lifetime      │
│   run_in_executor: offloads Ed25519 math to thread pool        │
│                                                                 │
│ Security invariants                                             │
│   sub == credentialSubject.id  (application layer check)       │
│   require=["exp","iat","nbf","iss","sub"]  (structural check)  │
│   Any payload tamper → InvalidSignatureError                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 14 — The Enrollment Office** covers `api/services/identity.py`: how agents register, how `issue_agent_credential()` is called within the full registration flow, how the resulting tokens are stored in PostgreSQL, and how revocation works. The credential you just learned to issue becomes the centerpiece of the identity lifecycle.
