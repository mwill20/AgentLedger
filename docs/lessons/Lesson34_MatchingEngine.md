# Lesson 34: The Gatekeeper — The Matching Engine

> **Beginner frame:** The matching engine is a gatekeeper that compares service needs, agent policy, trust tier, and sensitivity. Its output decides which fields are required, optional, withheld, or committed.

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_matcher.py`  
**Prerequisites:** Lessons 32, 33  
**Estimated time:** 90 minutes

---

## Welcome Back, Agent Architect!

A nightclub gatekeeper checks three things before letting anyone in: ID (are you who you say you are?), guest list (are you on it?), and dress code (do you meet the standard?). They do this in a specific order — there's no point checking the dress code if the ID is fake.

`match_context_request()` in `context_matcher.py` is AgentLedger's gatekeeper. It runs **eight sequential checks** before producing a verdict on each requested field. Skip any step and the entire security model breaks down. Understanding the order is the key insight of this lesson.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Recite the 8 steps of `match_context_request()` from memory
- Explain why session assertion verification comes before mismatch detection
- Trace `evaluate_profile()` for a given rule set and field
- Explain the fallback mode for unverified session assertions
- Describe the `ServiceContext` dataclass and what each field is used for
- Identify what gets written to Redis at the end of a successful match

---

## The 8-Step Flow

```
match_context_request(db, redis, request)
│
├─ 1. Rate limit check            — 100 matches / agent_did / 60 seconds
├─ 2. Session assertion verify    — JWT signature + expiry (fallback: decode unverified)
├─ 3. Load service context        — DB: service trust state + declared fields
├─ 4. Detect mismatch             — compare declared vs. requested; record event
├─ 5. Trust threshold gate        — reject required fields, withhold optional if trust insufficient
├─ 6. Load agent profile          — DB (or Redis cache): active profile + rules
├─ 7. Classify fields             — per-field: permit / withhold / commit
└─ 8. Generate commitments        — HMAC-SHA256 for committed fields; cache result
```

Each step's output feeds the next. If step 2 fails hard (no fallback), steps 3–8 never run.

---

## Step 1 — Rate Limiting

```python
await _enforce_match_rate_limit(redis, request.agent_did)
```

Uses Redis INCR + EXPIRE: increments a counter keyed by `f"ratelimit:match:{agent_did}"` and raises 429 if count exceeds 100 within 60 seconds. This is best-effort — if Redis is unavailable, the check is skipped (the function swallows the exception) and the match proceeds. Rate limiting is a DoS defence, not a security gate.

**Why rate-limit per `agent_did` and not per IP?** Because the match endpoint is called programmatically by agent platforms. A single IP might proxy requests for thousands of agents. Per-agent rate limiting targets the right resource unit.

---

## Step 2 — Session Assertion Verification

```python
claims = await _verify_session_assertion(db, request.session_assertion)
```

A session assertion is a JWT from Layer 2 (issued by `POST /v1/identity/sessions/assert`). Verification checks:
- JWT signature against the issuing agent's public key
- `exp` claim (not expired)
- The `sub` claim matches `request.agent_did`

**Fallback mode** (`context_matcher.py` ~lines 48–63): If full verification fails and the system is in `fallback_mode`, the JWT is decoded without signature verification to extract claims. This was added to allow Layer 4 to operate when Layer 2 is temporarily unavailable during development. The fallback result sets a `verified=false` flag on the match that flows through to the audit record.

---

## Step 3 — Load Service Context

```python
service = await _load_service_context(db, request.service_id, request.requested_fields)
```

The `ServiceContext` dataclass is built from a multi-table JOIN:

```python
@dataclass(frozen=True)
class ServiceContext:
    service_id: UUID
    domain: str          # e.g., "agentledger-perftest-service-0.example.com"
    did: str             # did:web:{domain}
    ontology_tag: str
    ontology_domain: str # e.g., "TRAVEL"
    trust_tier: int
    trust_score: float
    declared_required_fields: list[str]
    declared_optional_fields: list[str]
    field_sensitivity_tiers: dict[str, int]  # precomputed for all declared fields
```

The `field_sensitivity_tiers` dict is precomputed here — `get_sensitivity_tier()` is called once per declared field during service load, not once per field per rule evaluation. This is a performance optimisation: for a service with 10 declared fields and 20 profile rules, you'd otherwise call `get_sensitivity_tier()` 200 times.

---

## Step 4 — Detect Mismatch (Covered in Lesson 33)

```python
mismatch = detect_mismatch(
    service.declared_required_fields,
    service.declared_optional_fields,
    request.requested_fields,
)
if mismatch.detected:
    await _record_mismatch_event(db, mismatch, request, service)
```

Over-requested fields are excluded from further processing. The match continues for legitimate fields.

---

## Step 5 — Trust Threshold Gate

```python
permitted_fields, withheld_fields = _apply_trust_gate(service, effective_requested_fields)
```

The trust gate maps each field's sensitivity tier to a required trust tier:

```
sensitivity_tier 4 → required trust_tier 4
sensitivity_tier 3 → required trust_tier 3
sensitivity_tier 1 or 2 → required trust_tier 2
```

For **required fields** (declared as `required` in the manifest): if `service.trust_tier < required_trust_tier`, the entire match raises 403. A service requesting a required field it cannot be trusted with fails hard.

For **optional fields**: insufficient trust silently moves the field to `withheld_fields`. No error — the service simply doesn't get it.

**Why asymmetric treatment?** Required fields represent the service's stated dependency. If a flight booking service declares `user.name` as required and the trust gate blocks it, the booking cannot complete — 403 is the honest answer. Optional fields are enrichment; withholding them degrades the experience but does not break the interaction.

---

## Step 6 — Load Agent Profile

```python
profile = await context_profiles.get_active_profile(db, request.agent_did, redis=redis)
```

The profile is loaded from Redis (60s TTL) or the database. If no profile exists, a synthetic profile with `default_policy='deny'` and empty rules is used — no 404.

---

## Step 7 — Classify Fields: `evaluate_profile()`

This is the core algorithm. For each field that survived steps 4 and 5:

```python
def evaluate_profile(rules, field, service, default_policy):
    for rule in sorted(rules, key=lambda r: r.priority):
        if not rule_matches_service(rule, service):
            continue                        # rule scope doesn't apply to this service
        if field in rule.denied_fields:
            return 'withhold'              # explicit deny always wins
        if field in rule.permitted_fields:
            sensitivity = get_sensitivity_tier(field)
            return 'commit' if sensitivity >= 3 else 'permit'
    # No matching rule
    if default_policy == 'allow':
        sensitivity = get_sensitivity_tier(field)
        return 'commit' if sensitivity >= 3 else 'permit'
    return 'withhold'                      # default_policy == 'deny'
```

**Three possible verdicts per field:**
- `'permit'` — field can be shared in plaintext
- `'withhold'` — field is blocked (denied by rule or no matching rule with deny default)
- `'commit'` — field requires an HMAC commitment (sensitivity tier ≥ 3 AND profile permits it)

**The commit verdict** is the key Layer 4 innovation. A field that the profile permits but is high-sensitivity doesn't flow in plaintext. It is HMAC-committed: a hash is sent to the service now, and the actual value is only revealed after an explicit disclose request. This gives the agent a final confirmation step before sensitive data leaves the system.

### `rule_matches_service()`

```python
def rule_matches_service(rule, service):
    if rule.scope_type == 'domain':
        return service.ontology_domain == rule.scope_value
    if rule.scope_type == 'trust_tier':
        return service.trust_tier >= int(rule.scope_value)
    if rule.scope_type == 'service_did':
        return service.did == rule.scope_value
    if rule.scope_type == 'sensitivity':
        return True  # sensitivity rules apply to fields, checked in evaluate_profile
    return False
```

---

## Step 8 — Generate Commitments and Cache

For each field with verdict `'commit'`:

```python
commitment_hash, nonce = generate_commitment(field_value)
# INSERT INTO context_commitments ...
```

The HMAC commitment is covered in detail in Lesson 36. At this stage, the match result is also written to Redis (5-minute TTL) keyed by `match_id`. This cache is read during the disclose phase to reconstruct the match snapshot without a DB lookup.

---

## The Full Classification Example

Service: TRAVEL domain, trust_tier=2, declared fields: [`user.name`, `user.email`, `user.dob`]  
Agent profile: deny default, one rule: `scope_type=domain, scope_value=TRAVEL, permitted=[user.name, user.email]`  
Requested: [`user.name`, `user.email`, `user.dob`, `user.ssn`]

```
Step 4: user.ssn is not declared → over_requested=['user.ssn'], severity='critical'
         Remaining: [user.name, user.email, user.dob]

Step 5: user.dob sensitivity=3, required_trust_tier=3, service trust_tier=2
         user.dob is optional → withheld silently
         Remaining for profile: [user.name, user.email]

Step 7: user.name — rule matches (TRAVEL domain), in permitted_fields → tier 1 → 'permit'
         user.email — rule matches (TRAVEL domain), in permitted_fields → tier 1 → 'permit'

Result:
  permitted_fields: [user.name, user.email]
  withheld_fields:  [user.dob]   (trust gate)
  committed_fields: []
```

---

## Exercise 1 — Trace a Match Request

Send a match request via the API and watch the classification:

```bash
curl -s -X POST http://localhost:8000/v1/context/match \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_did": "did:key:z6MkTestContextAgent",
    "service_id": "<a-registered-service-uuid>",
    "session_assertion": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJkaWQ6a2V5OnpBYmMiLCJleHAiOjk5OTk5OTk5OTl9.",
    "requested_fields": ["user.name", "user.email"]
  }' | python -m json.tool
```

Read the response and identify: `permitted_fields`, `withheld_fields`, `committed_fields`, `commitment_ids`.

---

## Exercise 2 — Add a Sensitive Field

Repeat the match request from Exercise 1 but add `user.dob` to `requested_fields`. Observe how the response changes depending on the service's trust_tier and your profile rules. Then update your profile to explicitly deny `user.dob` and repeat — confirm it moves from `withheld_fields` (trust gate) to `withheld_fields` (profile deny).

---

## Exercise 3 — Read the Rate Limit Counter

After sending 5 match requests, inspect the Redis rate limit key:

```bash
docker exec agentledger-redis-1 redis-cli GET "ratelimit:match:did:key:z6MkTestContextAgent"
docker exec agentledger-redis-1 redis-cli TTL "ratelimit:match:did:key:z6MkTestContextAgent"
```

**Expected:** Counter is 5; TTL is ≤ 60 seconds.

---

## Best Practices

**Steps must run in order.** The sequence — rate limit → verify identity → load service → detect mismatch → trust gate → load profile → classify → commit — is not arbitrary. Each step's output constrains the next. Inverting steps 5 and 6 (trust gate after profile) would allow the profile to grant access that trust rules prohibit.

**Recommended (not implemented here):** A structured audit log entry at the end of every match (regardless of verdict), separate from the disclosure record, capturing the full classification decision for each field. This would support dispute resolution without requiring the match Redis cache to still exist.

---

## Interview Q&A

**Q: Why does `evaluate_profile()` return `'commit'` rather than `'permit'` for sensitive fields?**  
A: "Permit" means the value flows in the match response — the service sees plaintext. "Commit" means only an HMAC commitment is sent in the match response; the actual value requires an explicit disclose call. This gives the agent a chance to review before high-sensitivity data leaves the system.

**Q: What happens if the same field appears in both `permitted_fields` and `denied_fields` in one rule?**  
A: `denied_fields` is checked first in `evaluate_profile()`. An explicit deny always wins over a permit in the same rule. This prevents accidental disclosure from misconfigured rules.

**Q: Why cache the match result in Redis for 5 minutes?**  
A: The disclose phase needs to reconstruct the match snapshot (what fields were classified how) without a second DB round-trip. 5 minutes is long enough for a human or automated agent to review and approve the match before disclosing, but short enough to limit exposure if the Redis key is somehow accessed.

---

## Key Takeaways

- 8 steps, strictly ordered: rate limit → verify → load service → mismatch → trust gate → load profile → classify → commit
- Trust gate is asymmetric: required field failure → 403; optional field failure → silent withhold
- `evaluate_profile()`: explicit deny wins, then permit, then default_policy
- `'commit'` verdict means sensitivity_tier ≥ 3 AND profile permits — HMAC commitment, not plaintext
- Match result cached in Redis (5-min TTL) for the disclose phase

---

## Next Lesson

**Lesson 35 — The Trust Ladder: Trust Gates & Sensitivity Enforcement** goes deeper into how sensitivity tiers map to trust requirements, what "trust tier 4 for required fields" means in practice, and how the gate integrates with Layer 3's trust score system.
