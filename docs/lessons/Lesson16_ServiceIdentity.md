# Lesson 16 — The Business Card: Service Identity & did:web Activation

> **Beginner frame:** Service identity checks whether a service's public face matches the key and DID it advertises. It is the difference between a storefront sign and a verified business card.

**Layer:** 2 — Identity & Credentials  
**File:** `api/services/service_identity.py` (451 lines)  
**Prerequisites:** Lesson 12 (DID Methods — `did:web` resolution), Lesson 13 (Credential Issuance — signature verification pattern)  
**Estimated time:** 75 minutes

---

## Welcome

A business card carries a name, a title, and contact details. Anyone can print one. What makes a business card *credible* is that the contact details on it work — the phone number rings a real office, the email bounces back from the company's mail server. The identity is anchored in verifiable reality.

`did:web` works the same way. Any service can claim any domain-based DID. What makes the claim credible is that the service controls the HTTPS endpoint at that domain, and the public key it advertises there matches the key it used to sign its manifest. Control of the endpoint is the credential.

By the end of this lesson you will be able to:

- Explain the `did:web` resolution protocol and why it requires HTTPS
- Trace the 5-step manifest identity validation in `validate_signed_manifest()`
- Describe the `resolve_service_did_document()` cache strategy and its TTL
- Explain why a service must be trust-tier 2 before activating a `did:web` identity
- Describe what `activate_service_identity()` writes and why it triggers a trust recompute
- Explain why the verification method must appear in both `authentication` and `assertionMethod`

---

## What This Connects To

**Lesson 12:** `did:key` derived an identifier from a public key. `did:web` derives an identifier from a domain. Both are DIDs; only the derivation and resolution method differ.

**Lesson 15:** `request_session()` checks `last_verified_at IS NULL` to block sessions with services that haven't activated identity. Activation is what sets `last_verified_at`.

**Lesson 19:** The background worker `revalidate_service_identity.py` calls `activate_service_identity()` periodically to refresh the `did:web` document and keep trust scores current. The 600-second Redis cache serves both manual activations and background revalidations.

**Lesson 25 (Layer 3):** Trust tier 4 requires layer 3 attestations. But the prerequisite for layer 3 attestations is that the service has passed layer 2 identity activation — `last_verified_at` must be non-null.

---

## Architecture Position

```
Service operator
    │
    │ 1. Publishes DID document at https://domain/.well-known/did.json
    │ 2. Signs manifest with private key matching DID document JWK
    │ 3. Registers manifest (Layer 1 crawl)
    │
    ▼
POST /v1/identity/services/{domain}/activate
    │
    ├─ Check: service exists AND trust_tier >= 2
    ├─ Load current manifest from DB
    ├─ validate_signed_manifest():
    │   ├─ resolve_service_did_document() ──► HTTPS fetch + Redis cache
    │   ├─ _extract_verification_method()   ► find key in DID doc
    │   ├─ _ensure_method_is_authorized()   ► check auth + assertionMethod
    │   ├─ Compare manifest public_key == DID doc JWK
    │   └─ verify_json_signature()          ► Ed25519 verify manifest
    │
    ├─ UPDATE services SET public_key, last_verified_at
    ├─ INSERT crawl_events (service_identity_activated)
    ├─ recompute_service_trust()
    └─ db.commit()
```

---

## Core Concepts

### `did:key` vs. `did:web`

| Property | `did:key` | `did:web` |
|---|---|---|
| Identifier | `did:key:z6Mk...` | `did:web:example.com` |
| Key storage | Embedded in DID string | Published at HTTPS endpoint |
| Resolution | Decode the DID string | Fetch `/.well-known/did.json` |
| Rotation | New DID required | Update endpoint; DID stays same |
| Control proof | Control of private key | Control of HTTPS endpoint + key |
| Used for | Agents (portable, self-sovereign) | Services (domain-anchored) |

`did:web` is the right choice for services because services already have domains, and a domain is a meaningful trust signal — registering a domain requires identity verification with a registrar. Agents, by contrast, are potentially ephemeral and may not own domains, so `did:key` is more appropriate.

### The Chain of Authority

For a manifest signature to be trusted, three things must be true simultaneously:
1. The manifest's `identity.did` matches `did:web:{domain}`
2. The DID document at `https://{domain}/.well-known/did.json` advertises a public key
3. The manifest's `public_key` field matches that DID document key
4. The manifest was signed with the private key corresponding to that public key

An attacker who controls only the manifest (not the domain) can't forge a valid signature — they don't have the key. An attacker who controls only the domain (not the key) can't forge a valid signature — they can change the DID document, but changing it would invalidate existing manifests that reference the old key. The two-layer proof requires simultaneous control of both.

### The 600-Second Cache

`did:web` resolution requires an HTTPS round trip to an external server. Under load, re-fetching every request would be slow and would impose significant load on service operators' infrastructure. The 600-second (10-minute) Redis cache strikes a balance:

- Fast enough that normal activation and background revalidation don't add latency
- Short enough that a key rotation (update the DID document) takes effect within 10 minutes
- `force_refresh=True` allows the background worker or an operator call to bypass the cache immediately

---

## Code Walkthrough

### 1. Manifest Signing Payload (lines 61–63)

```python
def build_manifest_signing_payload(manifest: ServiceManifest) -> dict[str, Any]:
    return manifest.model_dump(mode="json", exclude_none=True, exclude={"signature"})
```

The manifest is serialized to a dict with the `signature` field excluded. This is what the service signed when it created the manifest signature. The server reconstructs this exact dict for verification. If `signature` were included in the signed payload, verification would be circular — the signature would cover itself.

`exclude_none=True` means optional fields that were not set produce identical payloads regardless of whether they're `None` or absent. This prevents a verification failure when the server's Pydantic model includes a `None` field that the service omitted when signing.

### 2. `resolve_service_did_document` (lines 160–206)

```python
async def resolve_service_did_document(domain, redis=None, force_refresh=False):
    key = _did_document_cache_key(domain)          # "service-did:{domain}"
    if not force_refresh:
        cached = await _cache_get(redis, key)
        if cached is not None:
            cached_payload = json.loads(cached)
            return ServiceDidResolutionResponse(
                did=service_did_from_domain(domain),
                did_document=cached_payload["did_document"],
                cache_status="hit",
                validated_at=datetime.fromisoformat(cached_payload["validated_at"]),
            )

    did_document = await _fetch_did_web_document(domain)  # HTTPS GET
    expected_did = service_did_from_domain(domain)
    if did_document.get("id") != expected_did:
        raise HTTPException(422, "did:web document id does not match the service domain")

    validated_at = datetime.now(timezone.utc)
    await _cache_set(redis, key, json.dumps({
        "did_document": did_document,
        "validated_at": validated_at.isoformat(),
    }))
    return ServiceDidResolutionResponse(
        ...,
        cache_status="miss",
        validated_at=validated_at,
    )
```

The `cache_status` field (`"hit"` or `"miss"`) is returned to the caller and included in the `crawl_events` record for the activation. This lets operators see whether activations used cached DID documents — relevant for debugging key rotation issues.

The `id` check (line 184) is the `did:web` specification requirement: a DID document published at `https://example.com/.well-known/did.json` must have `id: "did:web:example.com"`. A mismatch means the service is serving someone else's DID document, which is either a configuration error or an attack.

### 3. `validate_signed_manifest` — The 5-Step Chain (lines 209–260)

Each step must pass before proceeding to the next:

**Step 1 — Identity block presence (lines 215–219):**
```python
if manifest.identity is None or manifest.signature is None:
    raise HTTPException(422, "manifest identity and signature blocks are required")
```

**Step 2 — DID consistency (lines 221–226):**
```python
expected_did = service_did_from_domain(manifest.domain)
if manifest.identity.did != expected_did:
    raise HTTPException(422, "manifest identity.did must match the service did:web identifier")
```

The manifest's declared DID must match what we'd compute from the domain. This prevents a manifest claiming `did:web:malicious.com` while published under `legitimate.com`.

**Step 3 — DID document resolution (lines 228–232):**
```python
resolution = await resolve_service_did_document(
    domain=manifest.domain,
    redis=redis,
    force_refresh=force_refresh,
)
```

Fetch (or retrieve from cache) the DID document at `https://{domain}/.well-known/did.json`.

**Step 4 — Verification method validation (lines 233–248):**
```python
method = _extract_verification_method(
    resolution.did_document,
    manifest.identity.verification_method,
)
_ensure_method_is_authorized(
    resolution.did_document,
    manifest.identity.verification_method,
)

manifest_public_jwk = parse_manifest_public_key_jwk(manifest)
verification_jwk = method["publicKeyJwk"]
if verification_jwk != manifest_public_jwk:
    raise HTTPException(422, "manifest public_key does not match the did:web verification method")
```

`_extract_verification_method` finds the entry in `did_document.verificationMethod[]` whose `id` matches `manifest.identity.verification_method`. `_ensure_method_is_authorized` checks that this method appears in both `authentication` and `assertionMethod`.

**Why both `authentication` and `assertionMethod`?**

The W3C DID specification defines several "relationship lists" in a DID document that express *how* a key may be used:
- `authentication`: the key can prove control of the DID
- `assertionMethod`: the key can sign claims/credentials on behalf of the DID

A key that appears only in `authentication` can prove identity but not sign manifests (credentials). A key that appears only in `assertionMethod` can sign credentials but cannot prove identity. Service identity activation requires both: it's both a control proof (the service controls this domain) and a signing act (the manifest is signed with this key).

**Step 5 — Manifest signature verification (lines 250–258):**
```python
if not verify_json_signature(
    payload=build_manifest_signing_payload(manifest),
    signature=manifest.signature.value,
    public_jwk=verification_jwk,
):
    raise HTTPException(422, "manifest signature verification failed")
```

The signature is verified against the JWK from the DID document — not from the manifest's own `public_key` field. Even though we checked that they match in Step 4, using the DID document's JWK as the authority is correct: the DID document is the ground truth, the manifest is the claim.

### 4. `activate_service_identity` (lines 321–450)

```python
async def activate_service_identity(db, domain, redis=None, force_refresh=False):
    # Prerequisite: service exists with trust_tier >= 2
    service_row = ...
    if int(service_row["trust_tier"]) < 2:
        raise HTTPException(412, "service must be domain-verified before identity activation")

    # Load current manifest
    manifest = ServiceManifest.model_validate(manifest_row["raw_json"])

    # Run the 5-step chain
    resolution = await validate_signed_manifest(manifest, redis, force_refresh)

    # Compute trust score inputs
    capability_probe_score, attestation_score, operational_score, reputation_score = \
        await _compute_service_trust_components(db, str(service_row["id"]), True)

    # Write activation
    await db.execute(text("""
        UPDATE services
        SET public_key = :public_key,
            last_verified_at = :verified_at,
            updated_at = NOW()
        WHERE id = :service_id
    """), {...})

    # Log the event
    await db.execute(text("""
        INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
        VALUES (:service_id, 'service_identity_activated', :domain, CAST(:details AS JSONB), NOW())
    """), {...})

    # Recompute trust score
    trust_snapshot = await trust.recompute_service_trust(db=db, service_id=str(service_row["id"]))
    await db.commit()
```

**`trust_tier >= 2` prerequisite (line 345):**

Trust tier 2 is the domain verification tier — the service's domain resolves to the correct manifest endpoint (Layer 1 crawler). An identity activation on a domain with no verified manifest is meaningless: the `did:web` document must be accessible, but so must the manifest that references it. Requiring tier 2 ensures the domain's crawl state is valid before attempting DID resolution.

`HTTP 412 Precondition Failed` is semantically appropriate: the request is well-formed, but a prerequisite (domain verification) has not been met.

**Trust recompute on activation (line 428):**

Identity activation changes the `has_active_service_identity` flag used in trust score computation. Rather than letting the score stale out until the next background recompute, `activate_service_identity` calls `recompute_service_trust()` inline before the commit. The score update and the identity activation land in the same transaction — there's no window where `last_verified_at` is set but the trust score still reflects an unverified service.

### 5. Trust Score Integration (`_compute_service_trust_components`, lines 263–318)

This internal helper gathers the four trust score inputs before writing them:

```python
# Capability probe score: verified_capabilities / total_capabilities
capability_probe_score = 0.0 if total_count == 0 else verified_count / total_count

# Operational score: uptime SLA percentage
operational_score = 0.5 if uptime is None else max(0.0, min(float(uptime) / 100.0, 1.0))

# Reputation score: session outcomes over 30 days
reputation_score = compute_reputation_score(success_count, failure_count)

# Attestation score: 1.0 if has_active_service_identity, else 0.0
attestation_score = compute_attestation_score(has_active_service_identity)
```

The `has_active_service_identity=True` argument to `compute_attestation_score` reflects that this call always happens in the context of activation — we're computing what the score will be *after* activation, not what it is now.

---

## Exercises

### Exercise 1 — Inspect the did:web resolution flow

```bash
# Serve a minimal DID document locally using Python's HTTP server
# (requires the service to already have a domain that resolves)

# In a separate terminal, create the .well-known directory
mkdir -p /tmp/well-known
cat > /tmp/well-known/did.json << 'EOF'
{
  "id": "did:web:localhost:8080",
  "verificationMethod": [{
    "id": "did:web:localhost:8080#key-1",
    "type": "JsonWebKey2020",
    "controller": "did:web:localhost:8080",
    "publicKeyJwk": {
      "kty": "OKP",
      "crv": "Ed25519",
      "x": "<base64url-encoded-public-key>"
    }
  }],
  "authentication": ["did:web:localhost:8080#key-1"],
  "assertionMethod": ["did:web:localhost:8080#key-1"]
}
EOF

# Serve it
python -m http.server 8080 --directory /tmp

# In the Python REPL:
import asyncio
from api.services.service_identity import resolve_service_did_document

async def run():
    result = await resolve_service_did_document("localhost:8080")
    print("DID:", result.did)
    print("Cache status:", result.cache_status)
    print("Validated at:", result.validated_at)

asyncio.run(run())
```

Expected output:
```
DID: did:web:localhost:8080
Cache status: miss
Validated at: 2026-04-27 12:00:00+00:00
```

### Exercise 2 — Trace the signed manifest validation

```python
# In the Python REPL
from api.services.service_identity import validate_signed_manifest, build_manifest_signing_payload
from api.services.crypto import generate_ed25519_keypair, sign_json
from api.models.manifest import ServiceManifest, ServiceIdentityBlock, SignatureBlock
import json, asyncio

# Generate a keypair for the service
private_jwk, public_jwk = generate_ed25519_keypair()
domain = "example.local"

# Build the unsigned manifest
manifest_dict = {
    "name": "Test Service",
    "version": "1.0.0",
    "domain": domain,
    "protocol": "https",
    "capabilities": [],
    "identity": {
        "did": f"did:web:{domain}",
        "verification_method": f"did:web:{domain}#key-1",
    },
    "public_key": json.dumps(public_jwk),
}

# Sign it
manifest_for_signing = {k: v for k, v in manifest_dict.items() if k != "signature"}
signature = sign_json(manifest_for_signing, private_jwk)
manifest_dict["signature"] = {"value": signature}

manifest = ServiceManifest.model_validate(manifest_dict)
print("Signing payload keys:", list(build_manifest_signing_payload(manifest).keys()))
```

Expected output:
```
Signing payload keys: ['name', 'version', 'domain', 'protocol', 'capabilities', 'identity', 'public_key']
```

Note that `signature` is excluded from the signing payload.

### Exercise 3 (failure) — Test the verification method authorization check

```python
# Modify the DID document to exclude the key from assertionMethod
did_doc_missing_assertion = {
    "id": "did:web:example.local",
    "verificationMethod": [{
        "id": "did:web:example.local#key-1",
        "type": "JsonWebKey2020",
        "controller": "did:web:example.local",
        "publicKeyJwk": public_jwk,
    }],
    "authentication": ["did:web:example.local#key-1"],
    # "assertionMethod" intentionally missing
}

from api.services.service_identity import _ensure_method_is_authorized

try:
    _ensure_method_is_authorized(did_doc_missing_assertion, "did:web:example.local#key-1")
except Exception as e:
    print("Error:", e.detail)
```

Expected output:
```
Error: verification method must appear in authentication and assertionMethod
```

### Exercise 4 — Observe the trust score change on activation

```bash
# Before activation
curl -s http://localhost:8000/v1/services/<service_id> | python -m json.tool | grep trust

# Activate (replace <domain> with a real registered service domain)
curl -s -X POST "http://localhost:8000/v1/identity/services/<domain>/activate" \
  -H "X-API-Key: <your_key>" | python -m json.tool

# After activation
curl -s http://localhost:8000/v1/services/<service_id> | python -m json.tool | grep trust
```

Expected change: `trust_tier` increases (often from 2 to 3) and `trust_score` increases because `attestation_score` now reflects the active identity.

---

## Best Practices

### What AgentLedger does

- **Chain of authority** — manifest signature is verified against the DID document JWK, not the manifest's own `public_key` field; the DID document is always the authority
- **10-minute Redis cache** — avoids HTTPS round trips on every activation/revalidation while still reflecting key rotations within a reasonable window
- **`force_refresh` bypass** — allows the background revalidation worker to skip the cache when needed
- **Trust recompute in the activation transaction** — score update and identity activation land atomically; no staleness window
- **`trust_tier >= 2` prerequisite** — prevents identity activation on services that haven't passed domain verification

### Recommended (not implemented here)

- **DID document signature** — the DID document itself is served over HTTPS but isn't cryptographically signed. An attacker with a TLS certificate for a domain could serve a modified DID document. A signed DID document (using the service's own key) would close this gap.
- **Key rotation handling** — if a service rotates its key (updates `/.well-known/did.json`), any manifests signed with the old key become unverifiable. An explicit key rotation flow would store a key history and allow verification against previously-valid keys with a cutoff date.
- **DID document caching with ETag** — currently the cache is time-based. An HTTP ETag-based conditional GET would reduce bandwidth when the DID document hasn't changed.

---

## Interview Q&A

**Q: Why does `did:web` resolution require HTTPS and not HTTP?**

A: The entire trust model of `did:web` rests on control of the domain. If resolution were allowed over HTTP, a network-level attacker (e.g., an on-path proxy) could intercept the request and substitute a different DID document, mapping the service's DID to an attacker-controlled key. HTTPS provides transport-layer integrity; the TLS certificate for the domain is an additional control signal that the responder controls the domain.

**Q: What is the significance of `exclude_none=True` in `build_manifest_signing_payload`?**

A: When a service signs its manifest, it may not include all optional fields. When Pydantic parses that manifest, optional fields that were absent are set to `None`. Without `exclude_none=True`, the serialized dict would include `"field": null` entries that weren't in the original payload the service signed — producing different bytes and a verification failure. `exclude_none=True` ensures the reconstructed signing payload matches what the service originally serialized.

**Q: Why must the verification method appear in both `authentication` and `assertionMethod`?**

A: The W3C DID specification defines these as capability grants, not just key registries. `authentication` grants the ability to prove control of the DID (identity proof). `assertionMethod` grants the ability to sign credentials on behalf of the DID (assertion proof). Service manifest signing is an assertion act. Requiring both means the key can be used for both purposes — it's the key the service uses for all its cryptographic operations, not a special-purpose assertion-only key.

**Q: Why require `trust_tier >= 2` before identity activation?**

A: Trust tier 2 (domain verification) means the crawler has confirmed the domain resolves to a valid manifest. If identity activation were allowed on tier-1 services, a service could activate a `did:web` identity before its domain is even accessible — creating a false impression of verified identity. Tier 2 ensures the HTTPS infrastructure that supports `did:web` resolution is actually in place.

**Q: What happens to sessions with a service if the service's `did:web` key is rotated?**

A: Existing session assertions continue to work until they expire (5 minutes) — they contain the service's DID, not the old JWK directly. New sessions can be issued as soon as the new DID document is published and the service's manifest is re-signed with the new key. The 600-second cache means there's up to a 10-minute window where the old document is served from cache, after which the new document takes effect. Calling `activate_service_identity(force_refresh=True)` immediately applies the new document.

---

## Key Takeaways

```
┌─────────────────────────────────────────────────────────────────┐
│ Lesson 16 Reference Card                                        │
├─────────────────────────────────────────────────────────────────┤
│ did:web resolution                                              │
│   URL: https://{domain}/.well-known/did.json                   │
│   id check: document.id must equal did:web:{domain}            │
│   Cache TTL: 600 seconds (force_refresh=True bypasses)         │
│                                                                 │
│ validate_signed_manifest — 5 steps                             │
│   1. identity + signature blocks present                       │
│   2. manifest.identity.did == did:web:{domain}                 │
│   3. Fetch/cache DID document                                  │
│   4. Extract verification method, check auth+assertionMethod   │
│      manifest.public_key == DID doc JWK                       │
│   5. verify_json_signature(payload_excluding_signature)        │
│                                                                 │
│ activate_service_identity prerequisites                        │
│   services.trust_tier >= 2 (domain verified)                  │
│   manifests.is_current = true (has a manifest)                 │
│                                                                 │
│ What activation writes                                         │
│   services.public_key = manifest.public_key                   │
│   services.last_verified_at = NOW()                           │
│   crawl_events: 'service_identity_activated'                  │
│   recompute_service_trust() inline (same transaction)         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Next Steps

**Lesson 17 — The Approval Desk** covers `api/services/authorization.py`: how human-in-the-loop authorization requests move through the pending → approved/denied lifecycle, how the approval triggers a session assertion issuance, and how webhook dispatch notifies both the requesting agent and the service operator.
