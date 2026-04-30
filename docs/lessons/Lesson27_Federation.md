# 🎓 Lesson 27: The Neighborhood Watch — Federation & Blocklist Distribution

> **Beginner frame:** Federation is how one registry shares security warnings with another. In AgentLedger, revocations and blocklists can travel as signed alerts instead of staying trapped inside one database.

## 🏘️ Welcome Back, Agent Architect!

You can stamp attestations and revoke services on-chain. But right now that revocation is a secret. Every other AgentLedger registry on the planet continues trusting a service that has just been globally revoked — until they independently discover the on-chain event themselves.

Think of a **coordinated neighborhood watch**: when one member confirms a threat, they broadcast it to everyone on the network within minutes. AgentLedger's federation layer works the same way — confirmed revocations fan out as signed webhook pushes to every subscribed registry, while lightweight consumers can poll or stream the blocklist without authentication.

---

## 🎯 Learning Objectives

By the end of this lesson you will be able to:

- ✅ Explain the `get_blocklist()` `since` parameter and why it enables incremental sync
- ✅ Describe the SSE streaming pattern used by `stream_blocklist()`
- ✅ Trace `subscribe_registry()` from HTTP request to `federated_registries` upsert
- ✅ Explain how `dispatch_revocation_pushes()` builds per-subscriber differential payloads
- ✅ Describe the Ed25519 signature header and how a subscriber verifies it
- ✅ Explain the `.well-known/agentledger-blocklist.json` discovery endpoint and who uses it

**Estimated time:** 75 minutes
**Prerequisites:** Lessons 22 (Chain Abstraction), 24 (Attestation Pipeline)

---

## 🔍 What This Component Does

```
Revocation confirmed on-chain
          |
          v  (via confirm_pending_events → recompute_service_trust)
chain_events row: event_type='revocation', is_confirmed=true
          |
          +——————————————→ GET /v1/federation/blocklist          (poll)
          |                GET /v1/federation/blocklist/stream   (SSE)
          |                GET /.well-known/agentledger-blocklist.json
          |
          v  (Celery push_revocations task, every 60s)
dispatch_revocation_pushes(db)
          |
          |  For each active subscriber in federated_registries:
          |  ① get_blocklist(since=last_push_at)     ← differential
          |  ② sign payload with Ed25519 private key ← tamper-evident
          |  ③ POST to webhook_url                   ← push delivery
          |  ④ UPDATE last_push_at (on success only)  ← cursor advance
          v
Subscriber receives signed blocklist payload
```

**Key files:**
- [`api/services/federation.py`](../../api/services/federation.py) — all federation logic
- [`api/routers/federation.py`](../../api/routers/federation.py) — endpoint wiring
- [`api/services/crypto.py`](../../api/services/crypto.py) — `sign_json` / `verify_json_signature`
- [`api/services/sse.py`](../../api/services/sse.py) — `format_sse` helper

---

## 🏗️ The `federated_registries` Table

From `db/migrations/versions/004_layer3_trust_verification.py`:

```sql
CREATE TABLE federated_registries (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    endpoint        TEXT UNIQUE NOT NULL,   -- the registry's base URL (conflict key)
    webhook_url     TEXT,                   -- POST target for push delivery
    public_key_pem  TEXT,                  -- registry's public key for verification
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_push_at    TIMESTAMPTZ,            -- cursor: last successful push timestamp
    last_push_status TEXT,                 -- 'success' or 'failed'
    push_failure_count INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`last_push_at` is the **incremental sync cursor**. Every successful delivery advances it to `NOW()`. The next delivery for that subscriber sends only revocations confirmed after that timestamp. If a delivery fails, `last_push_at` stays frozen — the next attempt automatically retries the same window.

---

## 📝 Code Walkthrough: `get_blocklist()`

**File:** [`api/services/federation.py`](../../api/services/federation.py) lines 32–85

```python
async def get_blocklist(
    db: AsyncSession,
    page: int = 1,
    limit: int = 50,
    since: datetime | None = None,
) -> FederationBlocklistResponse:
    """Return the confirmed global revocation list."""
    since_key = since.isoformat() if since is not None else "none"
    cache_key = f"blocklist:{page}:{limit}:{since_key}"
    cached = runtime_cache.get(cache_key)
    if cached is not None:
        return cached
```

**Cache key construction (lines 39–43):** The cache key encodes all three query parameters — page, limit, and the `since` timestamp. The `since` parameter makes the cache key specific enough that `since=None` (full list) and `since=2026-01-01T00:00:00Z` (incremental) never collide. TTL is 2 seconds (`_BLOCKLIST_TTL_SECONDS = 2.0`) — short enough to reflect new revocations within 2 seconds, long enough to absorb a burst of concurrent readers.

**The SQL query (lines 58–76):**

```python
SELECT
    COALESCE(s.domain, ce.event_data->>'domain') AS domain,
    ce.event_data->>'reason_code' AS reason,
    COALESCE(ce.confirmed_at, ce.indexed_at) AS revoked_at,
    ce.tx_hash
FROM chain_events ce
LEFT JOIN services s ON s.id = ce.service_id
WHERE ce.event_type = 'revocation'
  AND ce.is_confirmed = true
  [AND COALESCE(ce.confirmed_at, ce.indexed_at) >= :since]
ORDER BY COALESCE(ce.confirmed_at, ce.indexed_at) DESC
LIMIT :limit OFFSET :offset
```

**Why `COALESCE(s.domain, ce.event_data->>'domain')`?** Chain events can arrive for services that have since been deleted from the `services` table. The `LEFT JOIN` preserves the revocation record. `COALESCE` falls back to the domain stored in `event_data` JSONB when the services row no longer exists — so deleted services remain on the blocklist permanently.

**Why `COALESCE(ce.confirmed_at, ce.indexed_at)`?** In `CHAIN_MODE=local`, `confirmed_at` is set immediately (synthetic events are pre-confirmed). In `CHAIN_MODE=web3`, there's a delay between `indexed_at` (when the event was first seen) and `confirmed_at` (when 20-block window passed). Using the confirmed timestamp as the primary sort key ensures the blocklist is ordered by when the revocation became final, not when it was first observed.

**Pagination (lines 78–84):**

```python
next_page = page + 1 if len(rows) == limit else None
```

If the result set is exactly `limit` rows, there might be more — increment the page. If fewer rows come back than requested, we've reached the end. This is the standard keyset-style page cursor for REST APIs.

---

## 📝 Code Walkthrough: `stream_blocklist()` — SSE

**File:** [`api/services/federation.py`](../../api/services/federation.py) lines 88–96

```python
async def stream_blocklist(
    db: AsyncSession,
    since: datetime | None = None,
) -> AsyncIterator[str]:
    """Yield a simple SSE snapshot of the current blocklist state."""
    snapshot = await get_blocklist(db=db, page=1, limit=100, since=since)
    for revocation in snapshot.revocations:
        yield format_sse("revocation", revocation.model_dump_json())
    yield format_sse("end", "{}")
```

**`format_sse(event, data)`** (from `api/services/sse.py` line 6):
```python
def format_sse(event: str, data: str) -> str:
    """Render one SSE frame."""
    return f"event: {event}\ndata: {data}\n\n"
```

A Server-Sent Events frame is structured text over a long-lived HTTP connection:

```
event: revocation
data: {"domain":"bad-agent.io","reason":"security_incident","revoked_at":"...","tx_hash":"0x..."}

event: revocation
data: {"domain":"scam-service.com","reason":"policy_violation","revoked_at":"...","tx_hash":"0x..."}

event: end
data: {}

```

The double newline `\n\n` is the SSE frame separator. Each frame has an `event:` line (the event type, consumed by `EventSource.addEventListener('revocation', ...)` in browsers) and a `data:` line (the JSON payload).

**The `end` event** signals to the client that the snapshot is complete. This is a **snapshot stream**, not a persistent live stream — `stream_blocklist` fetches the current state once and streams it, then terminates. A client wanting continuous updates must reconnect or poll the REST endpoint.

**Router wiring** (`api/routers/federation.py` line 44–53):
```python
@router.get("/federation/blocklist/stream")
async def stream_blocklist(...) -> StreamingResponse:
    return StreamingResponse(
        federation.stream_blocklist(db=db, since=since),
        media_type="text/event-stream",
    )
```

`StreamingResponse` wraps the async generator. FastAPI holds the HTTP connection open and flushes each `yield` to the client as it arrives. The `text/event-stream` MIME type signals SSE to browsers — they can use the native `EventSource` API.

> **Recommended (not implemented here):** A production SSE stream would use `asyncio.Queue` or Redis pub/sub to push new revocations as they're confirmed, rather than a one-shot snapshot. The current design re-establishes a connection per request, which is fine for lightweight consumers but doesn't scale to thousands of concurrent subscribers.

---

## 📝 Code Walkthrough: `subscribe_registry()`

**File:** [`api/services/federation.py`](../../api/services/federation.py) lines 99–151

```python
async def subscribe_registry(
    db: AsyncSession,
    request: FederationRegistrySubscribeRequest,
) -> FederationRegistrySubscribeResponse:
    """Register or refresh one downstream federation subscriber."""
    result = await db.execute(
        text("""
            INSERT INTO federated_registries (
                name, endpoint, webhook_url, public_key_pem,
                is_active, created_at
            )
            VALUES (:name, :endpoint, :webhook_url, :public_key_pem, true, NOW())
            ON CONFLICT (endpoint) DO UPDATE
            SET name = EXCLUDED.name,
                webhook_url = EXCLUDED.webhook_url,
                public_key_pem = EXCLUDED.public_key_pem,
                is_active = true
            RETURNING id
        """),
        {...}
    )
    subscriber_id = result.scalar_one()
    await db.commit()
```

**`ON CONFLICT (endpoint) DO UPDATE`:** `endpoint` (the registry's base URL) is the unique business key. Re-subscribing the same endpoint refreshes the webhook URL, public key, and reactivates the subscriber if it was deactivated. `last_push_at` is intentionally NOT reset on upsert — a re-subscription doesn't erase the delivery history. A re-subscribing registry will receive only revocations after its last successful delivery.

**Why require `public_key_pem`?** Not used during subscription itself, but stored for future use in a *bidirectional* federation where this registry might push to the subscriber, and the subscriber needs to verify signatures from us in return. Currently the field is stored but not validated at subscription time.

---

## 📝 Code Walkthrough: `dispatch_revocation_pushes()`

**File:** [`api/services/federation.py`](../../api/services/federation.py) lines 205–276

This is the Celery task's workhorse — called every 60 seconds by `push_revocations` in `crawler/tasks/push_revocations.py`.

### Step 1 — Fetch active subscribers (lines 207–217)

```python
registries_result = await db.execute(
    text("""
        SELECT id, webhook_url, last_push_at
        FROM federated_registries
        WHERE is_active = true
          AND webhook_url IS NOT NULL
    """)
)
registries = registries_result.mappings().all()
```

Only active subscribers with a webhook URL are targeted. Subscribers that registered only for pull access (no `webhook_url`) are skipped.

### Step 2 — Build differential payload per subscriber (lines 220–228)

```python
for registry in registries:
    payload = await get_blocklist(
        db=db,
        page=1,
        limit=100,
        since=registry["last_push_at"],   # ← per-subscriber cursor
    )
    if not payload.revocations:
        continue                           # nothing new → skip
```

Each subscriber gets its own slice of the blocklist. `last_push_at = None` (new subscriber) fetches all confirmed revocations. `last_push_at = 2026-04-26T10:00:00Z` fetches only revocations confirmed after that time. If there's nothing new, the subscriber is skipped — no unnecessary HTTP calls.

### Step 3 — Sign the payload with Ed25519 (lines 230–237)

```python
headers = {"Content-Type": "application/json"}
body = payload.model_dump(mode="json")
if settings.issuer_private_jwk:
    try:
        private_jwk = json.loads(settings.issuer_private_jwk)
        headers["X-AgentLedger-Signature"] = sign_json(body, private_jwk)
    except Exception:
        headers["X-AgentLedger-Signature"] = ""
```

`sign_json` (from `api/services/crypto.py` line 92):
```python
def sign_json(payload: dict[str, Any], private_jwk: dict[str, Any]) -> str:
    """Sign a canonical JSON payload and return a base64url signature."""
    private_key = load_private_key_from_jwk(private_jwk)
    signature = private_key.sign(canonical_json_bytes(payload))
    return b64url_encode(signature)
```

**What `canonical_json_bytes` guarantees:** The payload is serialized with `sort_keys=True, separators=(",", ":")` — same key order, no extra whitespace. This means the subscriber can reproduce the exact byte sequence by applying the same serialization to the received JSON, and verify the signature against it. If the payload was tampered in transit (any field modified), the signature check fails.

**Signature header:** `X-AgentLedger-Signature: <base64url-encoded-Ed25519-signature>`

The subscriber verifies it with `verify_json_signature(payload, signature, publisher_public_jwk)` (from `api/services/crypto.py` lines 99–110):
```python
def verify_json_signature(payload, signature, public_jwk) -> bool:
    public_key = load_public_key_from_jwk(public_jwk)
    try:
        public_key.verify(b64url_decode(signature), canonical_json_bytes(payload))
    except Exception:
        return False
    return True
```

**Why Ed25519 over HMAC?** Ed25519 is an asymmetric scheme: the publisher signs with a private key that never leaves the server. The subscriber only needs the publisher's public key — they can't forge signatures, only verify them. HMAC requires sharing a secret key, which means anyone who knows it can forge messages.

**Graceful signature failure (line 237):** If the signing fails for any reason (malformed JWK, missing key), the header is set to `""` rather than blocking the push. The payload is still sent — signature validation on the subscriber side is optional in the current implementation.

> **Recommended (not implemented here):** Subscribers should reject push payloads with a missing or invalid `X-AgentLedger-Signature` header. The current implementation sends the push regardless of signature status, which means a misconfigured signing key would silently produce unauthenticated pushes.

### Step 4 — HTTP fan-out with httpx (lines 239–249)

```python
async with httpx.AsyncClient(timeout=5.0) as client:
    response = await client.post(
        registry["webhook_url"],
        headers=headers,
        json=body,
    )
    response.raise_for_status()
```

5-second timeout. If the subscriber's webhook is slow, unreachable, or returns a non-2xx status, `raise_for_status()` raises an exception caught by the outer `except Exception` block.

> **Note:** The current implementation pushes subscribers sequentially in a `for` loop. With many subscribers, this means delivery time grows linearly. A production deployment would use `asyncio.gather()` to fan out to all subscribers concurrently, or push each subscriber as its own Celery sub-task.

### Step 5 — Cursor advance on success only (lines 251–273)

```python
await db.execute(
    text("""
        UPDATE federated_registries
        SET last_push_at = CASE
                WHEN :status_name = 'success' THEN NOW()
                ELSE last_push_at
            END,
            last_push_status = :status_name,
            push_failure_count = CASE
                WHEN :status_name = 'failed' THEN push_failure_count + 1
                ELSE push_failure_count
            END
        WHERE id = :registry_id
    """),
    {"registry_id": registry["id"], "status_name": status_name},
)
```

**The `CASE` pattern for conditional update:** If delivery succeeded, `last_push_at` advances to `NOW()`. If it failed, `last_push_at` stays frozen at its previous value — the next 60-second beat will retry the exact same window. Failed deliveries increment `push_failure_count`, which can be used by an operator to identify unreachable subscribers.

The `await db.commit()` at line 275 commits all registry updates at once — all-or-nothing for the cursor advances.

---

## 🌐 The `.well-known` Discovery Endpoint

**File:** [`api/routers/federation.py`](../../api/routers/federation.py) lines 22–30

```python
@router.get(
    "/.well-known/agentledger-blocklist.json",
    response_model=FederationBlocklistResponse,
)
async def get_well_known_blocklist(db: AsyncSession = Depends(get_db)):
    return await federation.get_blocklist(db=db, page=1, limit=1000, since=None)
```

This endpoint requires **no authentication** (`require_api_key` is absent from its dependency list). It serves the full blocklist (up to 1000 entries) at a standard discovery path, following the `.well-known` RFC 8615 convention.

**Who uses this?** Lightweight consumers — browser extensions, CLI tools, small integrations — that want to check whether a service is blocklisted without subscribing to the webhook infrastructure. They just fetch `https://registry.example.com/.well-known/agentledger-blocklist.json` and check if a domain appears.

---

## 🔗 The Full Revocation Push Chain (End to End)

```
1. Auditor POSTs revocation → attestation + chain event written (is_confirmed=false)
2. Celery: confirm_chain_events fires every 5s
   → finds the revocation event past the 20-block window
   → sets is_confirmed=true
   → calls recompute_service_trust() → trust_score drops, service is_banned=true
3. Celery: push_revocations fires every 60s
   → calls dispatch_revocation_pushes()
   → for each active subscriber:
     a. get_blocklist(since=last_push_at) → the new revocation is included
     b. sign payload → X-AgentLedger-Signature header
     c. POST to webhook_url
     d. UPDATE last_push_at on success
4. Subscriber receives the signed payload
   → verifies signature against publisher's known public key
   → marks the domain as locally revoked
   → does NOT need to query the chain directly
```

**Total latency:** Step 2 is within ~40 seconds on Polygon (20 blocks × ~2s/block). Step 3 fires within 60 seconds of confirmation. End-to-end: revocation → subscriber notification ≤ ~100 seconds in normal conditions.

> **The `spec/LAYER3_SPEC.md` target is "< 60 seconds end-to-end."** This applies to the `CHAIN_MODE=local` path where confirmation is immediate and the 60-second push interval dominates. In `CHAIN_MODE=web3`, the 20-block confirmation window adds ~40 seconds, making the realistic end-to-end closer to 100 seconds on Amoy.

---

## 🧪 Manual Verification Exercises

### 🔬 Exercise 1: Subscribe a local listener and observe webhook delivery

Run `nc` as a mock webhook server in one terminal, then trigger a revocation.

```bash
# Terminal 1: Start a netcat listener on port 9999
nc -l 9999
```

```bash
# Terminal 2: Subscribe the netcat listener as a federation registry
curl -s -X POST http://localhost:8000/v1/federation/registries/subscribe \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Registry",
    "endpoint": "http://localhost:9999",
    "webhook_url": "http://localhost:9999",
    "public_key_pem": "placeholder"
  }' | python3 -m json.tool
```

```bash
# Terminal 2: Submit and confirm a revocation (CHAIN_MODE=local confirms immediately)
SERVICE_DOMAIN="<YOUR_DOMAIN>"

curl -s -X POST "http://localhost:8000/v1/attestations/${SERVICE_DOMAIN}/revoke" \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "auditor_did": "did:web:healthauditor.example.com",
    "reason": "Security incident: test revocation"
  }' | python3 -m json.tool
```

```bash
# Terminal 2: Manually trigger the push task (instead of waiting 60s)
docker compose exec api python3 -c "
import asyncio
from api.db import get_db_session
from api.services.federation import dispatch_revocation_pushes

async def run():
    async for db in get_db_session():
        result = await dispatch_revocation_pushes(db)
        print(result)
        break

asyncio.run(run())
"
```

**Expected output in Terminal 1 (netcat):**
```
POST / HTTP/1.1
Host: localhost:9999
Content-Type: application/json
X-AgentLedger-Signature: <base64url-signature-or-empty>

{"revocations":[{"domain":"<YOUR_DOMAIN>","reason":"security_incident",...}],"total":1,"next_page":null}
```

**Expected output in Terminal 2:**
```json
{"pushed": 1}
```

### 🔬 Exercise 2: SSE stream with curl

```bash
# Stream the current blocklist as SSE (press Ctrl+C to stop)
curl -s -N "http://localhost:8000/v1/federation/blocklist/stream" \
  -H "X-API-Key: dev-local-only"
```

**Expected output:**
```
event: revocation
data: {"domain":"<YOUR_DOMAIN>","reason":"security_incident","revoked_at":"2026-04-27T...","tx_hash":"0x..."}

event: end
data: {}

```

The connection closes after the `end` event because `stream_blocklist` is a snapshot streamer, not a persistent live feed.

```bash
# Same stream, but only revocations since a specific timestamp (incremental):
curl -s -N "http://localhost:8000/v1/federation/blocklist/stream?since=2026-01-01T00:00:00Z" \
  -H "X-API-Key: dev-local-only"
```

### 🔬 Exercise 3: Verify Ed25519 signature manually

```bash
# Fetch the blocklist as JSON
curl -s "http://localhost:8000/.well-known/agentledger-blocklist.json" \
  | python3 -m json.tool > /tmp/blocklist.json

# Get the raw blocklist response as a dict in the Python REPL:
docker compose exec api python3 -c "
import json
import asyncio
from api.db import get_db_session
from api.services.federation import get_blocklist
from api.services.crypto import sign_json, verify_json_signature, public_jwk_from_private_jwk
from api.config import settings

async def run():
    async for db in get_db_session():
        response = await get_blocklist(db=db, page=1, limit=100)
        body = response.model_dump(mode='json')

        if not settings.issuer_private_jwk:
            print('No issuer_private_jwk configured — signature test skipped')
            return

        private_jwk = json.loads(settings.issuer_private_jwk)
        signature = sign_json(body, private_jwk)
        public_jwk = public_jwk_from_private_jwk(private_jwk)

        # Verify: should return True
        valid = verify_json_signature(body, signature, public_jwk)
        print('Signature valid:', valid)

        # Tamper: flip one character in a domain name
        if body['revocations']:
            body['revocations'][0]['domain'] = 'TAMPERED'
        tampered = verify_json_signature(body, signature, public_jwk)
        print('Tampered signature valid:', tampered)
        break

asyncio.run(run())
"
```

**Expected output:**
```
Signature valid: True
Tampered signature valid: False
```

This demonstrates the core guarantee: modifying any field in the payload after signing causes the verification to fail. The `canonical_json_bytes` function ensures the byte sequence is deterministic, so the same payload always produces the same signature.

---

## 📊 Summary Reference Card

| Item | Location |
|------|----------|
| Blocklist query (SQL) | `federation.py:get_blocklist()` lines 58–76 |
| SSE frame format | `sse.py:format_sse()` line 6 |
| SSE stream | `federation.py:stream_blocklist()` lines 88–96 |
| Subscriber upsert (ON CONFLICT) | `federation.py:subscribe_registry()` lines 105–138 |
| Push fan-out | `federation.py:dispatch_revocation_pushes()` lines 205–276 |
| Ed25519 signing | `crypto.py:sign_json()` lines 92–96 |
| Ed25519 verification | `crypto.py:verify_json_signature()` lines 99–110 |
| Canonical JSON bytes | `crypto.py:canonical_json_bytes()` lines 44–51 |
| Discovery endpoint | `GET /.well-known/agentledger-blocklist.json` (no auth) |
| Blocklist TTL cache | 2 seconds (`_BLOCKLIST_TTL_SECONDS`) |
| Cursor advance key | `federated_registries.last_push_at` (advances only on success) |
| Celery push interval | Every 60 seconds (`push_revocations` task) |
| Signature header | `X-AgentLedger-Signature: <base64url-Ed25519>` |

---

## 📚 Interview Preparation

**Q: Why sign the webhook push payload if the transport is HTTPS?**

**A:** HTTPS secures the transport channel, but it doesn't prove the message content originated from the legitimate publisher. A subscriber trusts HTTPS to deliver the message without eavesdropping, but can't verify that the server sending it is actually the AgentLedger instance they registered with — a man-in-the-middle that controls DNS or a compromised CDN could inject false revocations. The Ed25519 signature over the canonical JSON payload means the subscriber can verify authenticity even if the transport layer is compromised. This is the same model used by JWTs and signed package registries.

**Q: What is the `since` parameter for, and what happens to a subscriber that was offline for two weeks?**

**A:** `since` is an incremental sync cursor — it filters the blocklist to only entries confirmed after that timestamp. When `dispatch_revocation_pushes` runs for a subscriber, it passes `registry["last_push_at"]` as `since`. A subscriber that was offline for two weeks has a `last_push_at` from two weeks ago — so the next successful push will deliver all revocations from the past two weeks in one payload (up to the 100-entry limit). The subscriber will receive a complete catch-up with a single push delivery, then resume incremental sync.

**Q: How does federation create a competitive moat for AgentLedger?**

**A:** Network effects. Every node in the AgentLedger federation shares blocklist data as soon as it confirms a revocation — a bad actor can't avoid the blocklist by switching which registry they're registered with. For AgentLedger as a product, this means the first registry to discover a malicious agent protects the entire network within ~100 seconds. A competing registry that doesn't participate in federation can only protect its own users. The more nodes join, the more valuable the network becomes — and the harder it is for a new entrant to replicate the coverage.

**Q: Why is the blocklist served without authentication at `.well-known`?**

**A:** Security information should be as easy to consume as possible. Requiring an API key to check whether a service is blocklisted creates friction — browser extensions, lightweight integrations, and compliance tools would have to manage credentials. The blocklist is intentionally public: it contains domains and revocation reasons, not any sensitive user data. Anyone knowing that `bad-agent.io` is blocklisted for a security incident is a feature, not a data leak. The `.well-known` path follows RFC 8615 convention, making it discoverable without documentation.

**Q: What happens if the issuer's private key is lost or rotated?**

**A:** Push signatures become unverifiable with the old key. Subscribers need to be notified of the new public key — the `public_key_pem` field on the subscriber record exists precisely to store the subscriber's public key for bidirectional trust. In a production deployment, key rotation would require: (1) update `ISSUER_PRIVATE_JWK` in the environment, (2) publish the new public JWK at a well-known endpoint, (3) notify all subscribers to update their trusted key. The current implementation has no automatic key rotation — it's a manual operational process.

---

## ✅ Key Takeaways

- `get_blocklist(since=...)` is the foundation of incremental sync — each subscriber gets only the revocations that happened after their last successful delivery
- `stream_blocklist` is a snapshot SSE stream, not a persistent feed — it fetches once and streams the current state, then emits `event: end` and closes
- `subscribe_registry` uses `ON CONFLICT (endpoint) DO UPDATE` — re-subscribing refreshes the webhook without resetting the delivery cursor
- `dispatch_revocation_pushes` is differential: it uses `last_push_at` as a per-subscriber cursor, advances it only on success, and increments `push_failure_count` on failure for operator visibility
- `sign_json` produces an Ed25519 signature over canonical JSON — subscribers can verify the payload was produced by the legitimate publisher without trusting the transport alone
- The `.well-known/agentledger-blocklist.json` endpoint is public and authentication-free — it's designed for lightweight consumers who just need to check a domain

---

## 🚀 Ready for Lesson 28?

Next up: **The Night Watchman — Celery Background Workers**. We'll trace all four Layer 3 Celery tasks — indexing, confirmation, anchoring, and pushing — and understand why each one exists, what it does on each beat, and how they fail safely.

*Remember: A neighborhood watch is only as good as its communication. Sign your messages, advance your cursors, and never stop watching.* 🏘️
