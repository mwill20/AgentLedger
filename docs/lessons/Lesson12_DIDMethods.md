# 🎓 Lesson 12: The Name Badge — DID Methods (did:key & did:web)

> **Beginner frame:** A DID is a verifiable name badge for software. AgentLedger uses DIDs to connect agents and services to public keys, so identity can be checked without relying only on a username or database row.

## 🪪 Welcome Back, Agent Architect!

In Lesson 11 you learned how to sign and verify. Now the question is: **how do you prove which public key belongs to whom?**

Think of a **name badge**: your name is public information, but the badge itself is issued by a trusted authority and contains a photo (your public key) that can't be forged. Decentralized Identifiers (DIDs) work the same way — they're globally unique, self-describing names that encode or point to a public key, with no central authority required.

AgentLedger uses two DID methods: `did:key` for agents (the key *is* the identity) and `did:web` for services (the key is published at a known HTTPS path).

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain what a DID is and why it doesn't require a central registry
- ✅ Derive a `did:key` identifier from an Ed25519 public JWK step by step
- ✅ Explain the multicodec prefix and base58 encoding in `did:key`
- ✅ Reconstruct a public JWK from a `did:key` string (the reverse operation)
- ✅ Describe the structure of a DID document and its required fields
- ✅ Explain `did:web` and how a service publishes its identity at `/.well-known/did.json`

**Estimated time:** 60 minutes
**Prerequisites:** Lesson 11 (Cryptographic Foundations)

---

## 🔍 What This Component Does

```
Ed25519 public JWK
        │
        │  did_key_from_public_jwk()
        ▼
"did:key:z6Mk..."          ← globally unique, no registry needed
        │
        │  build_did_key_document()
        ▼
DID Document {
  "id": "did:key:z6Mk...",
  "verificationMethod": [{"publicKeyJwk": {...}}],
  "authentication": [...],
  "assertionMethod": [...]
}

Service domain "example.com"
        │
        │  service_did_from_domain()   [in service_identity.py]
        ▼
"did:web:example.com"
        │
        │  Fetch https://example.com/.well-known/did.json
        ▼
DID Document (published by the service operator)
```

**Key file:** [`api/services/did.py`](../../api/services/did.py) (118 lines)

---

## 🏗️ What Is a DID?

A DID is a string like:
```
did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK
did:web:example.com
did:web:agentledger.io
```

**Anatomy:** `did : <method> : <method-specific-identifier>`

- `did` — the scheme, always literal
- `<method>` — how the identifier works (`key`, `web`, `ethr`, `ion`, etc.)
- `<method-specific-identifier>` — varies by method

**Why no central registry?** For `did:key`, the identifier *is* the public key — it's derived mathematically from the key material. No database lookup required. For `did:web`, the identifier maps to an HTTPS path — resolution is standard DNS + HTTPS, no special infrastructure.

---

## 📝 Code Walkthrough: `did:key` Derivation

**File:** [`api/services/did.py`](../../api/services/did.py) lines 43–48

```python
_ED25519_MULTICODEC_PREFIX = bytes.fromhex("ed01")

def did_key_from_public_jwk(public_jwk: dict[str, Any]) -> str:
    """Derive a did:key identifier from an Ed25519 public JWK."""
    if public_jwk.get("kty") != "OKP" or public_jwk.get("crv") != "Ed25519" or "x" not in public_jwk:
        raise ValueError("expected Ed25519 OKP public JWK")
    fingerprint_bytes = _ED25519_MULTICODEC_PREFIX + b64url_decode(str(public_jwk["x"]))
    return f"did:key:z{_base58_encode(fingerprint_bytes)}"
```

Step by step:

**Step 1:** Validate the JWK is Ed25519 (same check as `crypto.py`).

**Step 2:** Decode the public key bytes from `jwk["x"]` (base64url → 32 raw bytes).

**Step 3:** Prepend the multicodec prefix `0xed01`:
```
fingerprint_bytes = b"\xed\x01" + <32 raw key bytes>  = 34 bytes total
```

The multicodec prefix identifies the key type. `0xed01` means "Ed25519 public key" in the multicodec registry. This is what makes `did:key` self-describing — any resolver can read the prefix and know what algorithm to use.

**Step 4:** Base58-encode the 34 bytes (using the Bitcoin alphabet).

**Step 5:** Prefix with `z` (the multibase indicator for base58btc) and wrap: `did:key:z<base58>`.

**Why base58 instead of base64?** Base58 (the Bitcoin alphabet) excludes visually ambiguous characters: `0` (zero), `O` (capital O), `I` (capital I), `l` (lowercase L). This makes `did:key` identifiers safe to use in print, QR codes, and spoken communication.

**Full derivation example:**
```
public key bytes (hex): e8763b7e4b7a6e7e...  (32 bytes)
multicodec prefix:      ed01
fingerprint bytes:      ed01 e8763b7e4b7a6e7e...  (34 bytes)
base58btc encoded:      6MkhaXgBZDvotDkL5257...
did:key:                did:key:z6MkhaXgBZDvotDkL5257...
```

---

## 📝 Code Walkthrough: Base58 Encoding

**File:** [`api/services/did.py`](../../api/services/did.py) lines 13–40

```python
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _base58_encode(data: bytes) -> str:
    zero_count = len(data) - len(data.lstrip(b"\x00"))
    num = int.from_bytes(data, "big")
    encoded = ""
    while num:
        num, remainder = divmod(num, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    return ("1" * zero_count) + (encoded or "1")
```

This is a standard base-conversion algorithm: repeatedly divide by 58 and map remainders to the alphabet. The `zero_count` prefix handles leading zero bytes (which the integer conversion would lose — `b"\x00abc"` and `b"abc"` have the same integer value).

The implementation is in pure Python rather than using a library because the base58btc alphabet is short enough that a custom implementation is self-contained and avoids an extra dependency.

---

## 📝 Code Walkthrough: Reverse — `public_jwk_from_did_key()`

**File:** [`api/services/did.py`](../../api/services/did.py) lines 51–63

```python
def public_jwk_from_did_key(did: str) -> dict[str, str]:
    """Reconstruct a public Ed25519 JWK from a did:key identifier."""
    prefix = "did:key:z"
    if not did.startswith(prefix):
        raise ValueError("expected did:key identifier")
    payload = _base58_decode(did[len(prefix):])
    if not payload.startswith(_ED25519_MULTICODEC_PREFIX):
        raise ValueError("unsupported did:key multicodec prefix")
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(payload[len(_ED25519_MULTICODEC_PREFIX):]),
    }
```

The reverse:
1. Strip `did:key:z` prefix
2. Base58-decode the remainder → 34 bytes (`\xed\x01` + 32 key bytes)
3. Verify the multicodec prefix is `\xed\x01` (Ed25519)
4. Take the remaining 32 bytes, base64url-encode → `"x"` field
5. Return the OKP JWK

This means `did:key` is **self-contained**: given only the DID string, you can reconstruct the public key. No external lookup, no network call, no database.

---

## 📝 Code Walkthrough: DID Documents

**File:** [`api/services/did.py`](../../api/services/did.py) lines 71–86

```python
def build_did_document(did: str, public_jwk: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal DID document backed by an Ed25519 public JWK."""
    method_id = _verification_method_id(did)
    return {
        "id": did,
        "verificationMethod": [
            {
                "id": method_id,
                "type": "JsonWebKey2020",
                "controller": did,
                "publicKeyJwk": public_jwk,
            }
        ],
        "authentication": [method_id],
        "assertionMethod": [method_id],
    }
```

**`_verification_method_id(did)`** (line 67):
```python
def _verification_method_id(did: str) -> str:
    return f"{did}#{did.split(':')[-1]}"
```

For `did:key:z6Mk...`, this produces `did:key:z6Mk...#z6Mk...` — the DID followed by `#` followed by the last colon-separated segment. This is the W3C DID spec convention for `did:key` verification method IDs.

**The four required DID document fields:**

| Field | Purpose |
|-------|---------|
| `id` | The DID itself — the document's canonical identifier |
| `verificationMethod` | List of key descriptors with their public key material |
| `authentication` | Which keys can authenticate (prove you control the DID) |
| `assertionMethod` | Which keys can make assertions (sign credentials on behalf of this DID) |

**Why both `authentication` and `assertionMethod`?** They serve different purposes. `authentication` proves "I am this agent" (login). `assertionMethod` proves "I assert this claim" (signing a credential or manifest). In AgentLedger, agents use `authentication` when signing session request proofs, and the issuer uses `assertionMethod` when signing JWT VCs. Having both in the same key makes the implementation simpler — one key pair per identity.

**JsonWebKey2020** is the verification method type defined in the W3C DID spec for JWK-backed keys. The verifier looks for a `verificationMethod` entry of this type and extracts `publicKeyJwk`.

---

## 🌐 `did:web` — Service Identity

For services, the DID method is `did:web`. A service at `example.com` has DID `did:web:example.com`, and its DID document is served at:

```
https://example.com/.well-known/did.json
```

**Resolution protocol:**
1. Parse the DID: `did:web:example.com` → domain `example.com`
2. Fetch `https://example.com/.well-known/did.json` over HTTPS
3. Parse the JSON response as a DID document
4. Extract the `verificationMethod` containing an Ed25519 public key
5. Use that key to verify the service's manifest signature

This is implemented in `api/services/service_identity.py` (covered in Lesson 16). The `did.py` module provides the building blocks:

```python
# In service_identity.py:
from api.services.did import (
    extract_public_jwk_from_did_document,
    build_did_document,
)

# Derive did:web from domain:
service_did = f"did:web:{domain}"

# Fetch and parse did.json, then extract the key:
public_jwk = extract_public_jwk_from_did_document(did_document, expected_did=service_did)
```

**`extract_public_jwk_from_did_document()`** (lines 100–118):

```python
def extract_public_jwk_from_did_document(
    did_document: dict[str, Any],
    expected_did: str | None = None,
) -> dict[str, Any]:
    if expected_did is not None and did_document.get("id") != expected_did:
        raise ValueError("DID document id does not match expected DID")

    verification_methods = did_document.get("verificationMethod")
    if not isinstance(verification_methods, list) or not verification_methods:
        raise ValueError("DID document must include verificationMethod entries")

    for method in verification_methods:
        if isinstance(method, dict) and isinstance(method.get("publicKeyJwk"), dict):
            public_jwk = method["publicKeyJwk"]
            if public_jwk.get("kty") == "OKP" and public_jwk.get("crv") == "Ed25519":
                return public_jwk

    raise ValueError("DID document does not contain an Ed25519 publicKeyJwk")
```

**Why `expected_did` validation?** A malicious service could serve a DID document with `id: did:web:evil.com` at `https://legitimate.com/.well-known/did.json`, trying to impersonate another service. Checking that `did_document["id"] == expected_did` catches this.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Derive and reconstruct a did:key

```bash
docker compose exec api python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from api.services.crypto import public_jwk_from_ed25519_public_key, b64url_encode
from api.services.did import did_key_from_public_jwk, public_jwk_from_did_key, build_did_key_document

# Generate a key pair
private_key = Ed25519PrivateKey.generate()
public_jwk = public_jwk_from_ed25519_public_key(private_key.public_key())

# Derive the DID
did = did_key_from_public_jwk(public_jwk)
print('DID:', did)
print('DID length:', len(did), 'chars')

# Reconstruct the JWK from the DID alone
reconstructed = public_jwk_from_did_key(did)
print('Original x:', public_jwk['x'][:20], '...')
print('Reconstructed x:', reconstructed['x'][:20], '...')
print('Match:', public_jwk['x'] == reconstructed['x'])

# Build the full DID document
doc = build_did_key_document(public_jwk)
import json
print(json.dumps(doc, indent=2))
"
```

**Expected output:**
```
DID: did:key:z6Mk<~46 chars>
DID length: ~56 chars
Original x: <first 20 chars> ...
Reconstructed x: <same 20 chars> ...
Match: True
{
  "id": "did:key:z6Mk...",
  "verificationMethod": [{"id": "did:key:z6Mk...#z6Mk...", "type": "JsonWebKey2020", ...}],
  "authentication": ["did:key:z6Mk...#z6Mk..."],
  "assertionMethod": ["did:key:z6Mk...#z6Mk..."]
}
```

### 🔬 Exercise 2: Verify round-trip integrity

```bash
docker compose exec api python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from api.services.crypto import public_jwk_from_ed25519_public_key, b64url_encode, sign_json, verify_json_signature
from api.services.did import did_key_from_public_jwk, public_jwk_from_did_key

# Generate key pair
private_key = Ed25519PrivateKey.generate()
public_jwk = public_jwk_from_ed25519_public_key(private_key.public_key())
private_jwk = {**public_jwk, 'd': b64url_encode(private_key.private_bytes_raw())}

# Sign a payload
payload = {'action': 'prove_ownership', 'nonce': 'xyz789'}
signature = sign_json(payload, private_jwk)

# Get DID from public key
did = did_key_from_public_jwk(public_jwk)
print('DID:', did[:40], '...')

# Reconstruct public key from DID only — no original JWK needed
recovered_jwk = public_jwk_from_did_key(did)

# Verify using only the DID-recovered key
verified = verify_json_signature(payload, signature, recovered_jwk)
print('Verified from DID-recovered key:', verified)
print()
print('This proves: given only the DID, you can verify signatures')
print('No database, no network, no central registry needed.')
"
```

### 🔬 Exercise 3 (Failure): DID document ID mismatch

```bash
docker compose exec api python3 -c "
from api.services.did import extract_public_jwk_from_did_document

# A DID document claiming to be from evil.com
malicious_doc = {
    'id': 'did:web:evil.com',
    'verificationMethod': [{
        'id': 'did:web:evil.com#key-1',
        'type': 'JsonWebKey2020',
        'controller': 'did:web:evil.com',
        'publicKeyJwk': {'kty': 'OKP', 'crv': 'Ed25519', 'x': 'abc123xyz'}
    }],
    'authentication': ['did:web:evil.com#key-1'],
}

# But served from legitimate.com — the expected_did check catches this
try:
    extract_public_jwk_from_did_document(malicious_doc, expected_did='did:web:legitimate.com')
except ValueError as e:
    print('Caught impersonation attempt:', e)
"
```

**Expected:**
```
Caught impersonation attempt: DID document id does not match expected DID
```

---

## 📊 Summary Reference Card

| Function | Location | Purpose |
|----------|----------|---------|
| `did_key_from_public_jwk(jwk)` | `did.py:43` | Ed25519 JWK → `did:key:z<base58>` |
| `public_jwk_from_did_key(did)` | `did.py:51` | `did:key:z<base58>` → Ed25519 JWK |
| `build_did_document(did, jwk)` | `did.py:71` | Build minimal W3C DID document |
| `build_did_key_document(jwk)` | `did.py:89` | Build DID document for did:key |
| `build_issuer_did_document(did, jwk)` | `did.py:95` | Build AgentLedger issuer DID document |
| `extract_public_jwk_from_did_document(doc, expected)` | `did.py:100` | Extract Ed25519 JWK from DID document, validate id |
| `_verification_method_id(did)` | `did.py:66` | `did:key:z6Mk...` → `did:key:z6Mk...#z6Mk...` |

| Concept | Value |
|---------|-------|
| `did:key` prefix | `did:key:z` |
| Multicodec prefix for Ed25519 | `0xed01` (2 bytes) |
| Base58 alphabet | Bitcoin alphabet (58 chars, no 0/O/I/l) |
| Fingerprint size | 34 bytes (2 prefix + 32 key) |
| `did:web` resolution path | `https://<domain>/.well-known/did.json` |
| DID document type for JWK keys | `JsonWebKey2020` |

---

## 📚 Interview Preparation

**Q: Why does AgentLedger use `did:key` for agents but `did:web` for services?**

**A:** Agents are ephemeral — they might be a software process, a short-lived script, or a mobile app. They don't necessarily have a publicly routable hostname or the ability to serve HTTPS. `did:key` requires nothing external: the DID *is* the public key, derivable offline, with no infrastructure required. Services, by contrast, are persistent web endpoints with DNS names and HTTPS certificates. `did:web` lets a service prove ownership of its domain (its natural identifier) by publishing a DID document at a known path — anyone can fetch it without trusting AgentLedger.

**Q: What does the `z` prefix in `did:key:z6Mk...` mean?**

**A:** The `z` is the multibase prefix for base58btc encoding. Multibase is a self-describing encoding scheme where the first character identifies how the rest of the string is encoded: `z` = base58btc, `u` = base64url, `f` = base16. This means a resolver encountering `did:key:z...` knows to base58-decode the identifier without any other context.

**Q: Could two different agents end up with the same DID?**

**A:** No, assuming the Ed25519 key generation is cryptographically random. The 32-byte private key is sampled from a 2²⁵⁶ space — the probability of two independently generated keys being identical is astronomically small (approximately 1 in 10⁷⁷). The DID is a deterministic function of the public key, so unique keys produce unique DIDs.

---

## ✅ Key Takeaways

- A DID is a globally unique identifier that encodes or points to a public key — no central registry required
- `did:key` embeds the public key directly in the identifier: `did:key:z<base58(multicodec_prefix + key_bytes)>`
- The multicodec prefix `0xed01` identifies Ed25519, making `did:key` self-describing
- `public_jwk_from_did_key()` reverses the derivation — given only the DID string, reconstruct the full public JWK
- A DID document wraps the public key with `verificationMethod`, `authentication`, and `assertionMethod` fields
- `did:web` maps a domain to a DID document served at `https://<domain>/.well-known/did.json`

---

## 🚀 Ready for Lesson 13?

Next up: **The Notary — Credential Issuance & Verification**. We'll see how AgentLedger uses the issuer DID and Ed25519 to sign JSON Web Tokens (JWTs) into Verifiable Credentials — and how any third party can verify them without calling AgentLedger at all.

*Remember: A name badge is only trustworthy if it's issued by someone you trust and encodes something only the holder could have.* 🪪
