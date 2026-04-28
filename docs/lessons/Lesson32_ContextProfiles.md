# Lesson 32: The Permission Slip — Context Profiles & Rules

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_profiles.py`, `api/models/context.py`, `db/migrations/versions/005_layer4_context.py`  
**Prerequisites:** Lesson 31  
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

When you walk into a hospital, you fill out a form: "I authorise Dr. Smith to see my blood pressure history. I do not authorise anyone else to see my full medical records." You fill it out once. Then every time a nurse, specialist, or admin requests data about you, the form decides what they get.

A **context profile** is that form. It is the agent's pre-written, machine-enforced declaration of what they will share, with whom, and under what conditions. It is evaluated on every single context match — the agent does not need to be present to answer the question.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain the profile data model: one profile → N rules
- Describe all four `scope_type` values and when each is used
- Trace `create_profile()` from HTTP request to committed database rows
- Explain why rules are sorted by priority ascending (lower number = higher priority)
- Explain what `default_policy='deny'` means for an agent with no matching rules
- Understand why the Redis cache has a 60-second TTL (not longer)

---

## The Data Model

```
context_profiles (one per agent per named profile)
  id, agent_did, profile_name, is_active, default_policy, created_at, updated_at

context_profile_rules (N rules per profile)
  id, profile_id, priority, scope_type, scope_value,
  permitted_fields[], denied_fields[], action, created_at
```

An agent can have multiple named profiles (`profile_name`), but only one is `is_active=true` at a time. The matching engine always loads the active profile.

### `default_policy`

The most important field in `context_profiles`. It determines what happens when **no rule matches** a given service:

| `default_policy` | Behaviour when no rule matches |
|-----------------|-------------------------------|
| `'deny'` | All fields withheld. The safe default for new agents. |
| `'allow'` | All fields permitted (still gated by trust and sensitivity). |

New agents with no profile at all are treated as `default_policy='deny'` throughout the matching engine. There is no "profile not found" exception on a match request — absence of a profile is a silent deny.

---

## The Four Scope Types

Each rule has a `scope_type` that determines *which services* the rule applies to:

| `scope_type` | `scope_value` example | Matches |
|---|---|---|
| `'domain'` | `'HEALTH'` | All services in the HEALTH ontology domain |
| `'trust_tier'` | `'3'` | All services with `trust_tier >= 3` |
| `'service_did'` | `'did:web:myservice.example.com'` | One specific service |
| `'sensitivity'` | `'2'` | All fields with `sensitivity_tier >= 2` |

The `scope_type + scope_value` pair is evaluated inside `rule_matches_service()` in `context_matcher.py`. A rule only applies to a service match if its scope matches. Rules that don't match are skipped.

---

## Code Walkthrough: `context_profiles.py`

### Profile creation (`create_profile`, lines 239–302)

```python
async def create_profile(db, request, redis=None):
    await _ensure_agent_exists(db, request.agent_did)   # 404 if not registered + active
    await _ensure_domain_scopes_exist(db, request)      # 422 if unknown ontology domain
    # ...
    # INSERT profile
    # INSERT rules in bulk
    await db.commit()
```

Two guard clauses run before any write:

1. **`_ensure_agent_exists()`** — queries `agent_identities WHERE did=... AND is_active=true AND is_revoked=false`. If the agent isn't registered, creating a profile fails with 404. This prevents orphaned profiles.

2. **`_ensure_domain_scopes_exist()`** — for any rule with `scope_type='domain'`, validates that the `scope_value` exists as a domain in the `ontology_tags` table. You cannot write a rule for a domain that isn't in the registry.

### Rule insertion (`_insert_rules`)

Rules are inserted as a batch. Each rule row gets:

```python
{
    "profile_id": profile_id,
    "priority": rule.priority,       # lower integer = evaluated first
    "scope_type": rule.scope_type,
    "scope_value": rule.scope_value,
    "permitted_fields": rule.permitted_fields,
    "denied_fields": rule.denied_fields,
    "action": rule.action,
}
```

Note: `permitted_fields` and `denied_fields` are stored as PostgreSQL `TEXT[]` arrays. This is important — the matching engine loads them as Python lists directly from the row.

### Profile retrieval (`get_active_profile`, lines 305–345)

```python
async def get_active_profile(db, agent_did, redis=None):
    cached = await _cache_get_profile(redis, _profile_cache_key(agent_did))
    if cached:
        return cached
    # ...DB query...
    result = await _build_profile_record(db, row)
    await _cache_set_profile(redis, cache_key, result)
    return result
```

The cache key is `f"context:profile:{agent_did}"`. TTL is **60 seconds**. Why not longer? Because `update_active_profile()` invalidates the cache on write, but there's a window where two app processes could serve stale data during the invalidation propagation. 60 seconds is the maximum staleness acceptable for a security-sensitive lookup.

### Rule sort order (`_build_profile_record`, lines 196–199)

```python
rules = sorted(rules_raw, key=lambda r: (r["priority"], r["created_at"], r["id"]))
```

Rules are sorted by `(priority ASC, created_at ASC, id ASC)`. Lower priority number = evaluated first. This is a "first match wins" system — the first rule whose scope matches a given service determines the outcome for a given field.

---

## A Worked Example

Agent `did:key:z6MkAlice` wants to create this profile:

```json
{
  "agent_did": "did:key:z6MkAlice",
  "profile_name": "default",
  "default_policy": "deny",
  "rules": [
    {
      "priority": 10,
      "scope_type": "domain",
      "scope_value": "TRAVEL",
      "permitted_fields": ["user.name", "user.email"],
      "denied_fields": [],
      "action": "permit"
    },
    {
      "priority": 20,
      "scope_type": "trust_tier",
      "scope_value": "3",
      "permitted_fields": ["user.name", "user.email", "user.phone"],
      "denied_fields": ["user.dob"],
      "action": "permit"
    }
  ]
}
```

**What this profile says:**
- For all TRAVEL services (priority 10, checked first): share `user.name` and `user.email`.
- For all tier-3+ services (priority 20, checked second): share name/email/phone, but explicitly deny DOB.
- For everything else: deny all fields (`default_policy='deny'`).

**Evaluation for a TRAVEL service at trust tier 3:**
- Rule priority 10 matches (scope_type=domain, scope_value=TRAVEL). Result for `user.name` → permitted.
- Priority 10 has no entry for `user.phone`. No match in this rule.
- Rule priority 20 matches (scope_type=trust_tier, scope_value=3, service is tier 3). Result for `user.phone` → permitted.
- Rule priority 20 denies `user.dob` → withheld.

---

## Exercise 1 — Create a Profile

Start the server and register a test agent (or use an existing DID). Then:

```bash
curl -s -X POST http://localhost:8000/v1/context/profiles \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_did": "did:key:z6MkTestContextAgent",
    "profile_name": "default",
    "default_policy": "deny",
    "rules": [
      {
        "priority": 10,
        "scope_type": "domain",
        "scope_value": "TRAVEL",
        "permitted_fields": ["user.name", "user.email"],
        "denied_fields": [],
        "action": "permit"
      }
    ]
  }' | python -m json.tool
```

**Expected:** 201 with `profile_id`, `agent_did`, `profile_name`, and the rule echoed back.

---

## Exercise 2 — Retrieve and Verify Sort Order

```bash
curl -s http://localhost:8000/v1/context/profiles/did:key:z6MkTestContextAgent \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

Add a second rule with a lower priority number (e.g., priority 5), update the profile, and retrieve again. Confirm the rules are returned in ascending priority order regardless of insertion order.

```bash
# Update profile with reversed insertion order (priority 20 first, then priority 5)
curl -s -X PUT http://localhost:8000/v1/context/profiles/did:key:z6MkTestContextAgent \
  -H "X-API-Key: dev-local-only" \
  -H "Content-Type: application/json" \
  -d '{
    "default_policy": "deny",
    "rules": [
      {"priority": 20, "scope_type": "trust_tier", "scope_value": "3",
       "permitted_fields": ["user.phone"], "denied_fields": [], "action": "permit"},
      {"priority": 5, "scope_type": "domain", "scope_value": "FINANCE",
       "permitted_fields": [], "denied_fields": ["user.name"], "action": "deny"}
    ]
  }' | python -m json.tool
```

**Expected:** GET response shows rules in order: priority 5, then priority 20.

---

## Exercise 3 — Observe the Cache

After retrieving a profile, check Redis:

```bash
docker exec agentledger-redis-1 redis-cli KEYS "context:profile:*"
docker exec agentledger-redis-1 redis-cli TTL "context:profile:did:key:z6MkTestContextAgent"
```

**Expected:** The key exists with a TTL ≤ 60 seconds. After a PUT update, the key disappears (cache invalidated) and reappears on the next GET.

---

## Best Practices

**Always use `default_policy='deny'` for new agents.** An allow-by-default policy means every field is shared with every service unless explicitly denied. This is almost never the correct default — it requires the agent to enumerate every sensitive field to deny, which is error-prone.

**Recommended (not implemented here):** A profile validation step that warns when a rule's `permitted_fields` contains a high-sensitivity field (tier 3+) without a corresponding trust-tier scope constraint — since that would share sensitive data with low-trust services.

---

## Interview Q&A

**Q: What happens if an agent has no profile at all?**  
A: The matching engine calls `get_active_profile()`, which raises 404 when no profile exists. The route handler passes `default_policy='deny'` and empty rules as the fallback. No context is ever shared without a matching rule in a non-deny-default scenario.

**Q: Why is priority an integer rather than an enum or enum rank?**  
A: Integer priorities allow agents to insert new rules between existing ones without renumbering (e.g., insert priority 15 between 10 and 20). Enums would require modifying existing rules whenever the ordering changes.

**Q: What prevents a rule for `scope_type='domain', scope_value='FAKE_DOMAIN'`?**  
A: `_ensure_domain_scopes_exist()` queries `ontology_tags` to confirm the domain exists. An unknown domain raises 422 before the profile is written.

---

## Key Takeaways

- One profile per agent (active), N rules per profile
- Rules are evaluated in ascending priority order — lowest number wins first
- Four scope types: `domain`, `trust_tier`, `service_did`, `sensitivity`
- `default_policy='deny'` is the safe default — no matching rule = no disclosure
- Redis cache (60s TTL) is invalidated on every profile update
- Field values are never stored — only field names in rules

---

## Next Lesson

**Lesson 33 — The Overstep Detector: Mismatch Detection & Sensitivity Tiers** covers what happens when a service requests a field it never declared in its manifest — and how Layer 4 catches, records, and escalates those violations.
