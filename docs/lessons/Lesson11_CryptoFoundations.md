# 🎓 Lesson 11: The Lock and Key — Cryptographic Foundations

> **Beginner frame:** Cryptography is how AgentLedger turns "trust me" into "verify this." Signatures, hashes, and canonical JSON let other systems prove who signed a claim and whether the evidence changed.

## 🔐 Welcome Back, Agent Architect!

AgentLedger Layer 2 is built on a single cryptographic primitive: **Ed25519 digital signatures**. Every piece of identity in the system — agent credentials, session tokens, manifest signatures, webhook authentication — traces back to this one mechanism.

Think of Ed25519 as a **special lock and key pair**: the private key is the lock-picker only you possess; the public key is the lock anyone can inspect. When you sign a message with your private key, anyone with your public key can verify it was really you — without ever seeing your private key.

This lesson teaches the math-free mental model and then walks through every function in `api/services/crypto.py` — the module that every other Layer 2 service imports.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain the Ed25519 key pair and what "signing" and "verifying" mean
- ✅ Explain JWK (JSON Web Key) format and the OKP key type
- ✅ Describe `canonical_json_bytes()` and why deterministic serialization is required for signing
- ✅ Trace `sign_json()` and `verify_json_signature()` end to end
- ✅ Explain `b64url_encode` / `b64url_decode` and the padding convention
- ✅ Understand the dependency-tolerant import guard and why it exists

**Estimated time:** 60 minutes
**Prerequisites:** Lesson 01 (Big Picture). No cryptography background required.

---

## 🔍 What This Component Does

```
Any Layer 2 caller (identity.py, sessions.py, service_identity.py, federation.py)
          │
          │  import from api.services.crypto
          │
          ├─── sign_json(payload, private_jwk)     → base64url signature string
          ├─── verify_json_signature(payload, sig, public_jwk) → bool
          ├─── canonical_json_bytes(payload)        → deterministic bytes for signing
          ├─── load_private_key_from_jwk(jwk)      → Ed25519PrivateKey object
          ├─── load_public_key_from_jwk(jwk)       → Ed25519PublicKey object
          ├─── public_jwk_from_ed25519_public_key(key) → OKP JWK dict
          ├─── b64url_encode(bytes)                → URL-safe base64 string (no padding)
          └─── b64url_decode(str)                  → bytes
```

**Key file:** [`api/services/crypto.py`](../../api/services/crypto.py) (110 lines)

---

## 🧩 Mental Model: Signatures in 60 Seconds

```
Private key (secret, 32 bytes)  ──sign──►  Signature (64 bytes)
                                                │
Public key (shareable, 32 bytes) ──verify──►   ✓ or ✗
```

- **Signing:** Takes the private key + a message → produces a 64-byte signature. This signature is unique to both the key and the exact message bytes. Change one bit in the message → completely different signature.
- **Verifying:** Takes the public key + the original message + the signature → returns true/false. Verification never touches the private key.
- **Key insight:** You can publish your public key to the world. Anyone can verify your signatures. No one can forge them without your private key.

**Why Ed25519 specifically?**
- 32-byte keys (compact, easy to embed in JWTs and JSON)
- 64-byte signatures (smaller than RSA-2048's 256 bytes)
- Deterministic: the same message always produces the same signature with the same key (no random nonce, no signature malleability)
- Fast: ~70,000 signatures/second on modern hardware
- Used by Signal, OpenSSH, Ethereum, Polygon

---

## 🏗️ The JWK Format

JWK = JSON Web Key. It's the standard way to represent cryptographic keys as JSON objects.

An Ed25519 **public key** as a JWK:
```json
{
  "kty": "OKP",
  "crv": "Ed25519",
  "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
}
```

An Ed25519 **private key** as a JWK (also includes the public key):
```json
{
  "kty": "OKP",
  "crv": "Ed25519",
  "d": "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A",
  "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
}
```

| Field | Meaning |
|-------|---------|
| `kty` | Key type: `"OKP"` = Octet Key Pair (the JOSE standard term for EdDSA keys) |
| `crv` | Curve: `"Ed25519"` — which OKP curve |
| `x` | Public key bytes, base64url-encoded (no padding) |
| `d` | Private key scalar bytes, base64url-encoded (only in private JWK) |

**Why not RSA or ECDSA?** RSA keys are 2048+ bits, making them unwieldy in JWTs and DID documents. ECDSA (secp256k1, P-256) requires a random nonce per signature — nonce reuse is catastrophic. Ed25519 avoids both problems.

---

## 📝 Code Walkthrough: Dependency-Tolerant Import Guard

**File:** [`api/services/crypto.py`](../../api/services/crypto.py) lines 13–22

```python
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover
    serialization = None
    Ed25519PrivateKey = None
    Ed25519PublicKey = None
```

`crypto.py` is imported by the Layer 1 FastAPI app at startup — even before Layer 2 dependencies (`cryptography` package) are installed. Without this guard, a `ModuleNotFoundError` would crash the Layer 1 app if the `cryptography` package isn't installed.

**`ensure_crypto_available()`** (lines 25–30) is called at the start of every function that actually needs the crypto library:
```python
def ensure_crypto_available() -> None:
    if serialization is None or Ed25519PrivateKey is None or Ed25519PublicKey is None:
        raise RuntimeError(
            "Layer 2 crypto dependencies are unavailable; install cryptography"
        )
```

This converts an opaque `AttributeError: 'NoneType' object has no attribute 'from_private_bytes'` into a clear `RuntimeError` with actionable guidance.

---

## 📝 Code Walkthrough: Base64url Encoding

**File:** [`api/services/crypto.py`](../../api/services/crypto.py) lines 33–41

```python
def b64url_encode(data: bytes) -> str:
    """Encode bytes with URL-safe base64 and no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    """Decode URL-safe base64 with optional stripped padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)
```

**Why URL-safe base64?** Standard base64 uses `+` and `/` characters, which are special in URLs and JSON (require percent-encoding). URL-safe base64 replaces `+` with `-` and `/` with `_` — safe everywhere.

**Why strip padding?** Standard base64 pads output to a multiple of 4 characters with `=`. JWK and JWT specifications (RFC 7515, RFC 7517) mandate no padding — so `rstrip(b"=")` removes it.

**Why add padding back on decode?** Python's `base64.urlsafe_b64decode` requires length to be a multiple of 4. The formula `(-len(data) % 4)` computes exactly how many `=` characters are needed:

```python
# len=10 → (-10 % 4) = 2 → add "=="
# len=12 → (-12 % 4) = 0 → add nothing
# len=11 → (-11 % 4) = 1 → add "="
```

This is a common Python pattern worth memorizing — you'll see it wherever JWT or JWK data is decoded.

---

## 📝 Code Walkthrough: `canonical_json_bytes()`

**File:** [`api/services/crypto.py`](../../api/services/crypto.py) lines 44–51

```python
def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a JSON object deterministically for signing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
```

This function answers a subtle but critical question: **what exactly do you sign?**

A digital signature is over **bytes**. Given a Python dict, you need to convert it to bytes first. The naive approach:

```python
json.dumps(payload)  # ← WRONG for signing
```

This is wrong because `json.dumps({"b": 2, "a": 1})` might produce `'{"b": 2, "a": 1}'` or `'{"a": 1, "b": 2}'` depending on Python version and dict insertion order. If the signer and verifier serialize differently, verification fails even though the content is identical.

**`sort_keys=True`:** Always emits keys in alphabetical order — same result regardless of dict ordering.

**`separators=(",", ":")`:** Produces compact JSON with no spaces: `{"a":1,"b":2}` instead of `{"a": 1, "b": 2}`. One fewer byte per key-value pair; more importantly, no ambiguity about whitespace.

**`ensure_ascii=False`:** Allows Unicode characters as-is (e.g., `"名前"` instead of `"名前"`). Consistent between Python encoder and JavaScript/other-language verifiers.

**`.encode("utf-8")`:** Produces `bytes` from the string. Ed25519 operates on bytes, not strings.

> **Why not use the JCS standard?** JSON Canonicalization Scheme (RFC 8785) is the formal standard for this. AgentLedger's `canonical_json_bytes` implements the same logic (sort keys + compact separators + UTF-8) — it's a pragmatic implementation rather than a library dependency.

---

## 📝 Code Walkthrough: Loading Keys from JWK

**File:** [`api/services/crypto.py`](../../api/services/crypto.py) lines 74–89

```python
def load_public_key_from_jwk(jwk: dict[str, Any]):
    """Load an Ed25519 public key from an OKP JWK."""
    ensure_crypto_available()
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "x" not in jwk:
        raise ValueError("expected Ed25519 OKP public JWK")
    raw = b64url_decode(str(jwk["x"]))
    return Ed25519PublicKey.from_public_bytes(raw)


def load_private_key_from_jwk(jwk: dict[str, Any]):
    """Load an Ed25519 private key from an OKP JWK."""
    ensure_crypto_available()
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "d" not in jwk:
        raise ValueError("expected Ed25519 OKP private JWK")
    raw = b64url_decode(str(jwk["d"]))
    return Ed25519PrivateKey.from_private_bytes(raw)
```

Each function:
1. Calls `ensure_crypto_available()` — fails fast with a clear error if the library isn't installed
2. Validates the JWK fields — `kty`, `crv`, and the required key component (`x` for public, `d` for private)
3. Decodes the base64url key material to raw bytes
4. Constructs the `cryptography` library key object

**Why validate `kty` and `crv`?** An attacker who can control the JWK being loaded might supply an RSA key (`kty=RSA`) and cause a type confusion bug. The explicit field checks ensure only Ed25519 OKP keys are accepted.

---

## 📝 Code Walkthrough: `sign_json()` and `verify_json_signature()`

**File:** [`api/services/crypto.py`](../../api/services/crypto.py) lines 92–110

```python
def sign_json(payload: dict[str, Any], private_jwk: dict[str, Any]) -> str:
    """Sign a canonical JSON payload and return a base64url signature."""
    private_key = load_private_key_from_jwk(private_jwk)
    signature = private_key.sign(canonical_json_bytes(payload))
    return b64url_encode(signature)


def verify_json_signature(
    payload: dict[str, Any],
    signature: str,
    public_jwk: dict[str, Any],
) -> bool:
    """Verify a canonical JSON payload signature."""
    public_key = load_public_key_from_jwk(public_jwk)
    try:
        public_key.verify(b64url_decode(signature), canonical_json_bytes(payload))
    except Exception:
        return False
    return True
```

**`sign_json` in three steps:**
1. Load the private key from JWK format
2. Compute canonical bytes of the payload and sign them → 64-byte signature
3. Return the signature as base64url string (suitable for HTTP headers, JWT fields, JSON)

**`verify_json_signature` in three steps:**
1. Load the public key from JWK format
2. Compute canonical bytes of the payload
3. Call `public_key.verify()` — if the signature doesn't match, it raises an exception (Ed25519's API doesn't return a boolean; it raises on failure)

**Why `except Exception: return False`?** The `cryptography` library raises `cryptography.exceptions.InvalidSignature` on failure, but wrapping in a broad `except Exception` means any key loading error, base64 decoding error, or library error also returns `False` rather than propagating. For a verification function, "any error means not verified" is the correct security posture.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Generate a key pair and sign a message

```bash
docker compose exec api python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from api.services.crypto import (
    public_jwk_from_ed25519_public_key,
    b64url_encode,
    sign_json,
    verify_json_signature,
)

# Generate a fresh key pair
private_key = Ed25519PrivateKey.generate()
public_key = private_key.public_key()

# Export both as JWK
public_jwk = public_jwk_from_ed25519_public_key(public_key)
private_jwk = {**public_jwk, 'd': b64url_encode(
    private_key.private_bytes_raw()
)}

print('Public JWK:', public_jwk)
print('Private JWK keys:', list(private_jwk.keys()))

# Sign a payload
payload = {'agent': 'test-agent', 'action': 'register', 'nonce': 'abc123'}
signature = sign_json(payload, private_jwk)
print('Signature (base64url):', signature[:20], '...')
print('Signature length:', len(signature), 'chars')

# Verify it
valid = verify_json_signature(payload, signature, public_jwk)
print('Valid:', valid)

# Tamper with the payload — should fail
tampered = {**payload, 'action': 'TAMPERED'}
tampered_valid = verify_json_signature(tampered, signature, public_jwk)
print('Tampered valid:', tampered_valid)
"
```

**Expected output:**
```
Public JWK: {'kty': 'OKP', 'crv': 'Ed25519', 'x': '<86-char base64url>'}
Private JWK keys: ['kty', 'crv', 'x', 'd']
Signature (base64url): <first 20 chars> ...
Signature length: 86 chars
Valid: True
Tampered valid: False
```

The signature is 64 bytes × 4/3 (base64 expansion) ≈ 86 base64url characters.

### 🔬 Exercise 2: Observe canonical JSON determinism

```bash
docker compose exec api python3 -c "
from api.services.crypto import canonical_json_bytes

# Same content, different insertion order
d1 = {'b': 2, 'a': 1, 'z': 26}
d2 = {'z': 26, 'a': 1, 'b': 2}

b1 = canonical_json_bytes(d1)
b2 = canonical_json_bytes(d2)

print('Dict 1 bytes:', b1)
print('Dict 2 bytes:', b2)
print('Equal:', b1 == b2)
print()

# Whitespace variation — also equal
import json
standard = json.dumps(d1, sort_keys=True).encode()
canonical = canonical_json_bytes(d1)
print('Standard JSON:', standard)
print('Canonical JSON:', canonical)
print('Equal:', standard == canonical)
"
```

**Expected output:**
```
Dict 1 bytes: b'{"a":1,"b":2,"z":26}'
Dict 2 bytes: b'{"a":1,"b":2,"z":26}'
Equal: True

Standard JSON: b'{"a": 1, "b": 2, "z": 26}'
Canonical JSON: b'{"a":1,"b":2,"z":26}'
Equal: False
```

Standard JSON has spaces after `:` and `,`. Canonical JSON has none. Signing the standard form would produce a signature that canonical-form verifiers reject.

### 🔬 Exercise 3 (Failure): Load wrong key type

```bash
docker compose exec api python3 -c "
from api.services.crypto import load_public_key_from_jwk

# Try to load an RSA key where Ed25519 is expected
rsa_jwk = {'kty': 'RSA', 'n': 'abc123', 'e': 'AQAB'}
try:
    load_public_key_from_jwk(rsa_jwk)
except ValueError as e:
    print('Caught:', e)

# Try a JWK with wrong curve
wrong_curve = {'kty': 'OKP', 'crv': 'X25519', 'x': 'abc123'}
try:
    load_public_key_from_jwk(wrong_curve)
except ValueError as e:
    print('Caught:', e)
"
```

**Expected:**
```
Caught: expected Ed25519 OKP public JWK
Caught: expected Ed25519 OKP public JWK
```

---

## 📊 Summary Reference Card

| Function | Location | Purpose |
|----------|----------|---------|
| `b64url_encode(bytes)` | `crypto.py:33` | bytes → URL-safe base64 string, no padding |
| `b64url_decode(str)` | `crypto.py:38` | URL-safe base64 string → bytes, adds padding |
| `canonical_json_bytes(dict)` | `crypto.py:44` | dict → sorted compact UTF-8 JSON bytes |
| `load_public_key_from_jwk(jwk)` | `crypto.py:74` | OKP JWK → Ed25519PublicKey object |
| `load_private_key_from_jwk(jwk)` | `crypto.py:83` | OKP JWK → Ed25519PrivateKey object |
| `public_jwk_from_ed25519_public_key(key)` | `crypto.py:54` | Ed25519PublicKey → `{kty, crv, x}` dict |
| `public_jwk_from_private_jwk(jwk)` | `crypto.py:68` | Private JWK → Public JWK |
| `sign_json(payload, private_jwk)` | `crypto.py:92` | dict + private JWK → base64url signature |
| `verify_json_signature(payload, sig, public_jwk)` | `crypto.py:99` | dict + sig + public JWK → bool |
| `ensure_crypto_available()` | `crypto.py:25` | Guard: raises RuntimeError if library missing |

| Concept | Value |
|---------|-------|
| Key size | 32 bytes (Ed25519) |
| Signature size | 64 bytes → 86 base64url chars |
| JWK key type | `"kty": "OKP"`, `"crv": "Ed25519"` |
| Public key field | `"x"` (base64url of 32-byte public scalar) |
| Private key field | `"d"` (base64url of 32-byte seed/scalar) |
| Canonical JSON | `sort_keys=True`, `separators=(",",":")`, UTF-8 |

---

## 📚 Interview Preparation

**Q: Why does AgentLedger use Ed25519 instead of RSA or ECDSA?**

**A:** Three reasons: size, security, and determinism. Ed25519 keys are 32 bytes vs. RSA-2048's 256 bytes — compact enough to embed in JWT `sub` claims and DID documents. Ed25519 signatures are 64 bytes vs. ECDSA's variable 71–72 bytes. Most importantly, Ed25519 is deterministic: the same private key + same message always produces the same signature. ECDSA requires a random nonce per signature — if that nonce is ever reused (even accidentally), the private key can be recovered. Ed25519 has no such vulnerability.

**Q: Why must JSON be canonicalized before signing?**

**A:** A digital signature is over specific bytes. JSON is a text format with many valid serializations of the same data: `{"a":1,"b":2}` and `{"b": 2, "a": 1}` represent the same object but are different byte sequences. If the signer uses one serialization and the verifier uses another, verification fails even though the content is identical. `sort_keys=True` and `separators=(",",":")` ensure both parties produce the exact same bytes from the same data.

**Q: What's the security risk if `verify_json_signature` raised exceptions instead of returning `False`?**

**A:** Callers that don't catch the exception would crash, potentially causing a denial-of-service. More subtly, if the caller catches specific exception types (`except InvalidSignature:`) but the library changes its exception class name in a new version, valid tampered signatures might slip through. Returning `False` for any failure — including unexpected library errors — is the conservative, fail-safe design: when in doubt, reject.

---

## ✅ Key Takeaways

- Ed25519 is the cryptographic foundation for all Layer 2 identity: 32-byte keys, 64-byte signatures, deterministic
- JWK format represents keys as JSON with `kty="OKP"`, `crv="Ed25519"`, `x` (public), `d` (private)
- `canonical_json_bytes()` produces identical bytes regardless of dict order — required so signer and verifier agree on what was signed
- `sign_json()` and `verify_json_signature()` are the two public entry points used by every other Layer 2 service
- The import guard (`try/except ImportError`) lets the Layer 1 app run without Layer 2 dependencies installed

---

## 🚀 Ready for Lesson 12?

Next up: **The Name Badge — DID Methods**. We'll learn how Ed25519 public keys become globally unique, self-describing identifiers called `did:key`, and how services publish their keys at a well-known HTTPS path using `did:web`.

*Remember: The lock is public. The key is secret. The signature proves you have the key — without revealing it.* 🔐
