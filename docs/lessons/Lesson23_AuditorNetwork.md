# 🎓 Lesson 23: The Badge Office — Auditor Registration & Credentialing

## 🛡️ Welcome Back, Agent Architect!

You know the switchboard. Now let's meet the people who use it: **auditors** — security firms, compliance bodies, and certification authorities who evaluate AI services and stamp their approval on-chain.

But they can't just walk in and start attesting. Every airport has a badge office that verifies credentials and assigns a scoped access pass. AgentLedger's auditor system works the same way.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain the `auditors` database table and what each column means
- ✅ Trace `register_auditor()` from HTTP request to database row
- ✅ Explain `_scope_allows()` and the wildcard ontology scope system
- ✅ Describe why scope is enforced at both the Python layer and the EVM layer
- ✅ Explain `credential_hash` and why it's computed on registration
- ✅ Understand what happens when an auditor's credential expires

**Estimated time:** 60 minutes  
**Prerequisites:** Lessons 21–22

---

## 🔍 What This Component Does

```
POST /v1/auditors/register
           |
           v
🏢 auditor.register_auditor()   ← validate DID, scope, chain_address
           |                       compute credential_hash
           |                       upsert into auditors table
           v
📁 auditors table  (did, name, ontology_scope[], chain_address, credential_hash)
           |
           v
🔗 Used by: attestation.submit_attestation() to validate scope
            chain.py to generate auditor_chain_id for on-chain writes
```

---

## 🏗️ The `auditors` Database Table

**File:** [`db/migrations/versions/004_layer3_trust_verification.py`](../../db/migrations/versions/004_layer3_trust_verification.py) lines 19–34

```sql
CREATE TABLE auditors (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    did                  TEXT UNIQUE NOT NULL,        -- e.g. "did:web:auditor.example.com"
    name                 TEXT NOT NULL,               -- e.g. "Secure AI Labs"
    ontology_scope       TEXT[] NOT NULL,             -- e.g. ["health.*", "finance.payments"]
    accreditation_refs   JSONB NOT NULL DEFAULT '[]', -- external cert references
    chain_address        TEXT,                        -- 0x-prefixed wallet address
    credential_hash      TEXT,                        -- keccak256 of {did, name, scope, chain_address}
    is_active            BOOLEAN NOT NULL DEFAULT true,
    approved_at          TIMESTAMPTZ,
    credential_expires_at TIMESTAMPTZ,                -- 365 days from registration
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Column by column:**

| Column | Purpose |
|--------|---------|
| `did` | Decentralized Identifier — the auditor's unique global handle (`did:web:firm.io`) |
| `ontology_scope` | PostgreSQL text array of scopes this auditor is authorized to cover |
| `chain_address` | The Ethereum/Polygon wallet address that will hold `AUDITOR_ROLE` on-chain |
| `credential_hash` | Tamper-evident fingerprint of the registration payload |
| `credential_expires_at` | Always set to `NOW() + 365 days` on registration — creds must be renewed annually |

**Why `TEXT[]` for ontology_scope?** PostgreSQL native arrays allow efficient index-based queries and work naturally with Python list-to-array binding. An auditor can hold multiple scopes: `["health.*", "travel.booking"]`.

---

## 📝 Code Walkthrough: `AuditorRegistrationRequest` (Pydantic model)

**File:** [`api/models/layer3.py`](../../api/models/layer3.py) lines 50–85

```python
class AuditorRegistrationRequest(_SanitizedModel):
    """Request payload for registering a Layer 3 auditor."""

    did: str = Field(min_length=10, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    ontology_scope: list[str] = Field(min_length=1)      # at least one scope required
    accreditation_refs: list[dict[str, Any]] = Field(default_factory=list)
    chain_address: str | None = Field(default=None, max_length=128)

    @field_validator("did")
    @classmethod
    def validate_did(cls, value: str) -> str:
        if not value.startswith("did:"):
            raise ValueError("auditor DID must start with did:")
        return value

    @field_validator("ontology_scope")
    @classmethod
    def validate_ontology_scope(cls, value: list[str]) -> list[str]:
        invalid = [scope for scope in value if not _is_valid_scope(scope)]
        if invalid:
            raise ValueError(f"invalid ontology_scope values: {', '.join(sorted(invalid))}")
        return value

    @field_validator("chain_address")
    @classmethod
    def validate_chain_address(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.lower()
        if not re.fullmatch(r"0x[a-f0-9]{40}", normalized):
            raise ValueError("chain_address must be a 0x-prefixed 40-byte hex address")
        return normalized
```

**`_is_valid_scope(value)`** (lines 19–31) is the scope validator:
```python
def _is_valid_scope(value: str) -> bool:
    """Return whether a scope string is an exact tag or a supported prefix."""
    parts = value.split(".")
    if not 1 <= len(parts) <= 3:
        return False
    for index, part in enumerate(parts):
        if not part:
            return False
        if part == "*":
            return index == len(parts) - 1    # wildcard only allowed as the last segment
        if not part.replace("_", "").isalnum() or not part.islower():
            return False
    return True
```

Valid scope examples:
- `"health"` — exact top-level tag
- `"health.*"` — wildcard covers all health sub-tags
- `"finance.payments"` — exact two-level tag
- `"travel.booking.*"` — wildcard covers all `travel.booking.*` sub-tags

Invalid: `"*"` (top-level wildcard not allowed), `"health.*.records"` (wildcard must be last), `"Health.*"` (must be lowercase).

---

## 📝 Code Walkthrough: `register_auditor()`

**File:** [`api/services/auditor.py`](../../api/services/auditor.py) lines 21–95

```python
async def register_auditor(
    db: AsyncSession,
    request: AuditorRegistrationRequest,
) -> AuditorRegistrationResponse:
    """Register or refresh one active auditor."""

    # Step 1: Set credential expiry to 365 days from now
    credential_expires_at = datetime.now(timezone.utc) + timedelta(days=365)

    # Step 2: Compute a tamper-evident hash of the registration payload
    credential_hash = canonical_hash({
        "did": request.did,
        "name": request.name,
        "ontology_scope": request.ontology_scope,
        "chain_address": request.chain_address,
    })

    # Step 3: Upsert — insert new or refresh existing
    result = await db.execute(text("""
        INSERT INTO auditors (did, name, ontology_scope, accreditation_refs,
                              chain_address, credential_hash, is_active,
                              approved_at, credential_expires_at, created_at)
        VALUES (...)
        ON CONFLICT (did) DO UPDATE
            SET name = EXCLUDED.name,
                ontology_scope = EXCLUDED.ontology_scope,
                chain_address = EXCLUDED.chain_address,
                credential_hash = EXCLUDED.credential_hash,
                is_active = true,
                approved_at = NOW(),
                credential_expires_at = EXCLUDED.credential_expires_at
        RETURNING id
    """), {...})
```

**Why `ON CONFLICT (did) DO UPDATE`?** Re-registering an auditor refreshes their credentials. A security firm that changes its Polygon wallet address, expands its scope, or renews expiring credentials can re-POST to `/v1/auditors/register`. The DID is the stable identifier; everything else can change.

**What is `credential_hash` used for?** It's a tamper-evident fingerprint of what was registered. If someone modifies the `ontology_scope` column directly in the database, the `credential_hash` no longer matches what would be computed from the current values — creating a detectable inconsistency. In a hardened production deployment, a background job would periodically recompute and compare credential hashes.

---

## 📝 Code Walkthrough: `_scope_allows()` — The Scope Gate

**File:** [`api/services/attestation.py`](../../api/services/attestation.py) lines 30–39

```python
def _scope_allows(allowed_scopes: list[str], requested_scope: str) -> bool:
    """Return whether an auditor scope authorizes one attestation scope."""
    for scope in allowed_scopes:
        if scope == "*" or scope == requested_scope:
            return True
        if scope.endswith(".*"):
            prefix = scope[:-2]   # strip the ".*"
            # "health.*" covers "health" exactly and everything starting with "health."
            if requested_scope == prefix or requested_scope.startswith(prefix + "."):
                return True
    return False
```

Walk-through of cases:
```python
_scope_allows(["health.*"], "health.records")    # True  — "health." prefix matches
_scope_allows(["health.*"], "health")            # True  — exact prefix match
_scope_allows(["health.*"], "finance.payments")  # False — no prefix match
_scope_allows(["finance.payments"], "finance")   # False — specific tag ≠ parent
_scope_allows(["*"], "anything")                 # True  — global wildcard
_scope_allows(["health.*", "travel.*"], "travel.booking")  # True — second scope matches
```

**Why enforce this in Python AND in the EVM contract?**

- Python enforcement: rejects requests at the API layer before spending gas
- EVM enforcement: the contract's `onlyRole(AUDITOR_ROLE)` means that even if someone bypasses the Python API entirely (direct contract call), they can only attest if they hold `AUDITOR_ROLE` — but the scope check is entirely off-chain. This is an intentional design trade-off: on-chain scope enforcement would require expensive per-auditor scope storage in the contract.

> **Recommended (not implemented here):** A production hardening could add an on-chain scope registry — a mapping from `keccak256(auditorAddress) → allowedScopeHash`. Any attestation event could then be validated against this mapping by downstream verifiers.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Register two auditors with different scopes

```bash
# Register a health auditor
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:healthauditor.example.com",
    "name": "Health Security Labs",
    "ontology_scope": ["health.*"],
    "chain_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
  }' | python3 -m json.tool

# Register a finance auditor
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "did": "did:web:financeauditor.example.com",
    "name": "Finance Compliance Corp",
    "ontology_scope": ["finance.*"],
    "chain_address": "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
  }' | python3 -m json.tool

# List all active auditors
curl -s http://localhost:8000/v1/auditors \
  -H "X-API-Key: dev-local-only" | python3 -m json.tool
```

**Expected output (list):**
```json
[
  {
    "did": "did:web:financeauditor.example.com",
    "name": "Finance Compliance Corp",
    "ontology_scope": ["finance.*"],
    "is_active": true,
    "credential_expires_at": "2027-04-27T..."
  },
  {
    "did": "did:web:healthauditor.example.com",
    "name": "Health Security Labs",
    "ontology_scope": ["health.*"],
    "is_active": true,
    "credential_expires_at": "2027-04-27T..."
  }
]
```

### 🔬 Exercise 2: Attempt cross-scope attestation (should fail)

This requires a registered service. If you don't have one, register a dummy service first.

```bash
# First, find a registered service's domain from your DB:
docker compose exec db psql -U agentledger -d agentledger \
  -c "SELECT domain FROM services LIMIT 1;"

# Attempt to attest a service with the WRONG scope (health auditor → finance service)
# Note: you'd need to know the service domain is finance-related
curl -s -X POST http://localhost:8000/v1/attestations \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "auditor_did": "did:web:healthauditor.example.com",
    "service_domain": "<YOUR_SERVICE_DOMAIN>",
    "ontology_scope": "finance.payments",
    "evidence_package": {"type": "manual_review", "result": "pass"}
  }' | python3 -m json.tool
```

**Expected output:**
```json
{"detail": "attestation scope is outside the auditor's approved ontology scope"}
```

### 🔬 Exercise 3 (Failure): Submit without `did:` prefix

```bash
curl -s -X POST http://localhost:8000/v1/auditors/register \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "did": "notadid",
    "name": "Bad Auditor",
    "ontology_scope": ["health.*"],
    "chain_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
  }' | python3 -m json.tool
```

**Expected output:**
```json
{
  "detail": [{"msg": "Value error, auditor DID must start with did:", "type": "value_error"}]
}
```

---

## 📊 Summary Reference Card

| Item | Location |
|------|----------|
| Auditor registration | `api/services/auditor.py:register_auditor()` |
| Scope wildcard matching | `api/services/attestation.py:_scope_allows()` |
| Scope string validation | `api/models/layer3.py:_is_valid_scope()` |
| Pydantic model | `api/models/layer3.py:AuditorRegistrationRequest` |
| Router endpoint | `api/routers/attestation.py` line 25 |
| Database table | `004_layer3_trust_verification.py` lines 19–34 |
| Credential expiry | 365 days from `NOW()` on each registration |
| ON CONFLICT key | `did` (unique per auditor) |

---

## 📚 Interview Preparation

**Q: Why does an auditor need a `chain_address`? Can they register without one?**

**A:** `chain_address` is the Polygon wallet that must hold `AUDITOR_ROLE` on-chain for live attestations to work. The model allows `None` because in local/test mode no wallet is needed — `_remote_write()` is skipped entirely. In production, the chain_address must be granted `AUDITOR_ROLE` via `grant_roles.js` before any attestation can reach the contract. The API doesn't enforce this because the Python layer can run in local mode indefinitely.

**Q: What happens when an auditor's credential expires?**

**A:** `credential_expires_at` is stored but not automatically enforced in the current implementation. The `list_auditors()` query only filters on `is_active = true`, not on expiry. In a production deployment, the `expire_identity_records` Celery task (which already handles Layer 2 credential expiry) should be extended to set `is_active = false` for auditors past `credential_expires_at`.

> **Recommended (not implemented here):** Add a nightly `expire_auditor_credentials` Celery task that runs `UPDATE auditors SET is_active = false WHERE credential_expires_at < NOW() AND is_active = true`.

**Q: What is `credential_hash` and why compute it on registration?**

**A:** It's `keccak256({did, name, ontology_scope, chain_address})` — a tamper-evident fingerprint. If someone directly modifies the `ontology_scope` column in the database without going through the API (bypassing the scope validation), the stored `credential_hash` will no longer match the recomputed hash. This creates an auditable discrepancy. In a production compliance setup, the credential hash can also be published externally so third parties can verify the auditor registration hasn't been tampered with post-registration.

---

## ✅ Key Takeaways

- Auditors are registered via `POST /v1/auditors/register` which upserts into the `auditors` table and issues a 365-day credential
- `credential_hash = canonical_hash({did, name, scope, chain_address})` — a tamper-evident fingerprint stored alongside the auditor record
- `_scope_allows()` implements wildcard scope matching: `"health.*"` covers `"health"` and `"health.records"` but NOT `"finance.payments"`
- Scope is enforced at the Python layer (fast fail before gas spend) and implicitly at the EVM layer (only `AUDITOR_ROLE` holders can call the contract)
- Credentials expire after 365 days — automatic enforcement requires a Celery extension (recommended, not yet implemented)

---

## 🚀 Ready for Lesson 24?

Next up: **The Stamp of Approval — The Attestation Pipeline**. We'll trace the full journey of a service attestation from API request to on-chain event to confirmed database row, including the revocation path and the verify endpoint.

*Remember: The badge office controls who gets to stamp things. Without a valid badge, the blockchain rejects you.* 🛡️
