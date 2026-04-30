# Lesson 31: The Bouncer's Rulebook — Layer 4 Overview & Architecture

> **Beginner frame:** Context architecture is the privacy rulebook. It decides what an agent may share with a service, why that sharing is allowed, and what evidence remains afterward.

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `spec/LAYER4_SPEC.md`, `api/routers/context.py`, `db/migrations/versions/005_layer4_context.py`  
**Prerequisites:** Lessons 01–30 — this lesson opens the Layer 4 series  
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

You've built a trust-verified service registry. Services are registered (Layer 1), agent identities are cryptographically proven (Layer 2), and third-party attestations anchor trust to Polygon Amoy (Layer 3). Now a verified agent wants to interact with a verified service.

Here's the question Layer 4 answers:

> **"The service wants my name, email, and date of birth. It declared it needs the first two. My profile says I'll share name but not DOB with anyone below trust tier 3. What actually gets disclosed — and how do we prove it without lying to anyone?"**

Layer 4 is the **bouncer's rulebook**: the service at the door has declared what it needs, the agent has pre-written rules about what it'll share, and Layer 4 enforces the intersection — without letting either side cheat.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Explain the three-part invariant that governs every context disclosure
- Describe all five Layer 4 database tables and why each exists
- Map the Layer 4 API surface (10 endpoints) to the five service modules
- Trace the two-phase flow: Match → Disclose
- Explain why field *values* are never stored in the audit trail
- Identify what Layer 4 does not do (and why that boundary matters)

---

## Where Layer 4 Fits

```
🔍 Layer 1: Registry       — Does this service exist? What can it do?
🔐 Layer 2: Identity       — Is this agent's DID verified?
🏛️  Layer 3: Trust          — Has this service earned attestation?
🔎 Layer 4: Context        — What data can this service see about this agent?
                             ↓
              [ Layer 5: Workflow Registry — multi-step orchestration ]
```

Layers 1–3 answer questions about services. Layer 4 is the first layer that answers questions about the **agent**. It sits at the moment of interaction: just before an agent calls a service, it asks Layer 4 "is it safe to proceed, and what do I share?"

---

## The Three-Part Invariant

Every context disclosure must satisfy **all three** conditions or it does not happen:

```
context flows only when:
  (1) manifest declared it      — the service said it needs this field
  (2) user profile permits it   — the agent's rules allow sharing with this service
  (3) service trust clears threshold — the service's trust tier meets the field's sensitivity requirement
```

This is stated explicitly at `spec/LAYER4_SPEC.md:69`. If any condition fails, the field is withheld. The rule is enforced in code at `api/services/context_matcher.py` (steps 4, 5, 6 of the match flow).

---

## The Five Modules

Layer 4 is implemented across five service modules, each with a single responsibility:

| Module | File | Responsibility |
|--------|------|----------------|
| Profile CRUD | `api/services/context_profiles.py` | Create, read, update agent permission rules |
| Mismatch Detection | `api/services/context_mismatch.py` | Detect when services over-request context |
| Matching Engine | `api/services/context_matcher.py` | 8-step match flow; produce permit/withhold/commit verdict |
| Selective Disclosure | `api/services/context_disclosure.py` | Generate commitments; release nonces; write audit records |
| Compliance Export | `api/services/context_compliance.py` | Generate GDPR/CCPA compliance PDF |

---

## The Five Database Tables

Migration `005_layer4_context.py` adds five tables. None modify any Layer 1–3 tables.

```
context_profiles          — one row per agent per named profile
context_profile_rules     — N rules per profile (scoped, prioritised)
context_commitments       — pending HMAC commitments (15-min TTL)
context_disclosures       — append-only audit log (field NAMES only, not values)
context_mismatch_events   — append-only violations log (also never deleted)
```

The critical design decision: **field values are never stored anywhere in the database**. The audit trail records which fields were disclosed, not what their values were. This is the privacy-preserving property that makes Layer 4 GDPR-friendly.

---

## The Two-Phase Flow

Every context interaction takes two API calls:

```
Phase 1 — MATCH
POST /v1/context/match
  → Service asks: "can I see these fields for this agent?"
  → Layer 4 evaluates: manifest ∩ profile ∩ trust
  → Returns: permitted fields (plaintext OK), committed fields (HMAC only), withheld fields
  → Creates commitment rows in context_commitments (TTL: 5 minutes)

Phase 2 — DISCLOSE
POST /v1/context/disclose
  → Agent confirms: "yes, release the committed fields"
  → Layer 4 re-checks trust (trust can drop between phases)
  → Releases nonces for committed fields
  → Writes append-only record to context_disclosures
```

Why two phases? The match phase determines *authorization* (is the service allowed to receive this field?). The disclose phase handles *revelation* (here is the actual value, cryptographically bound to what was authorized). The gap between the two allows a human or automated system to review the match result before committing.

---

## The 10-Endpoint API Surface

```
POST   /v1/context/profiles               — create a profile with rules
GET    /v1/context/profiles/{agent_did}   — retrieve active profile
PUT    /v1/context/profiles/{agent_did}   — replace active profile

POST   /v1/context/match                  — run the matching engine
POST   /v1/context/disclose               — release nonces + write audit record

GET    /v1/context/disclosures/{agent_did}       — paginated audit history
POST   /v1/context/revoke/{disclosure_id}        — GDPR erasure of one record

GET    /v1/context/compliance/export/{agent_did} — download compliance PDF

GET    /v1/context/mismatches             — admin: list mismatch events
POST   /v1/context/mismatches/{id}/resolve — admin: resolve + optional trust escalation
```

All routes live in `api/routers/context.py`. Each handler is intentionally thin — it validates auth, calls one service function, and returns the result. Business logic lives entirely in the service layer.

---

## What Layer 4 Does NOT Do

- **It does not transmit field values to services.** It releases nonces; the service uses the nonce to verify a commitment the agent provided directly.
- **It does not store field values.** The audit trail contains field *names*, not *values*.
- **It does not execute workflows.** That is Layer 5.
- **It does not implement full zero-knowledge proofs.** The HMAC commitment scheme is v0.1. ZKP (circom/snark.js) is deferred to v0.2.

---

## Exercise 1 — Read the Schema

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "\d context_profiles"
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "\d context_profile_rules"
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "\d context_commitments"
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "\d context_disclosures"
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "\d context_mismatch_events"
```

For each table, identify: the primary key, any foreign keys, and what the `created_at` vs `updated_at` pattern tells you about whether the table is append-only.

**Expected insight:** `context_disclosures` and `context_mismatch_events` have no `updated_at`. They are append-only by design.

---

## Exercise 2 — Explore the Route Layer

Read `api/routers/context.py` (220 lines). Count how many lines of business logic live in the router vs. how many are in the service layer. Every handler should be ≤10 lines of application logic.

```bash
grep -c "await" api/routers/context.py
grep -c "await" api/services/context_matcher.py
```

**Expected:** Roughly 10 awaits in the router (one per endpoint); 40+ in the matcher (complex multi-step flow).

---

## Exercise 3 — Map the Three-Part Invariant to Code

Open `api/services/context_matcher.py` and find the three enforcement points:

1. **Manifest check** — where does the code load what the service declared?
2. **Profile check** — where does `evaluate_profile()` get called?
3. **Trust check** — where does `trust_tier >= required_tier` get enforced?

Write down the approximate line numbers for all three. You'll trace each in detail in Lessons 33–35.

---

## Best Practices

**The boundary between match and disclose is intentional.** Never combine them into one endpoint. The gap allows audit, review, and re-verification of trust state. A service that dropped from tier 3 to tier 2 between match and disclose should not receive committed fields — and the disclose phase catches this.

**Recommended (not implemented here):** A human-in-the-loop review step between match and disclose for tier-4 fields — where an out-of-band notification is sent before nonces are ever released.

---

## Interview Q&A

**Q: Why does Layer 4 need to re-check trust at the disclose phase if it already checked at match?**  
A: Trust scores change. A service could lose attestation between the match request and the disclose request (e.g., a revocation is confirmed on-chain). The disclose phase re-checks to ensure the service still qualifies for every committed field at the moment of revelation.

**Q: Why are field values never stored in the database?**  
A: Storage is the biggest GDPR liability surface. If a value is never stored, it cannot be breached. The audit trail only needs to prove *what was shared* for compliance purposes — it does not need the values themselves.

**Q: What does the three-part invariant protect against?**  
A: It prevents three specific attack classes: (1) a service requesting fields it never declared in its manifest, (2) an agent profile being silently overridden, and (3) a low-trust service receiving high-sensitivity data.

---

## Key Takeaways

- Layer 4 enforces: manifest declared it AND profile permits it AND trust clears the threshold
- Five tables: profiles, rules, commitments, disclosures, mismatches — none store field values
- Two-phase flow: Match (classify) → Disclose (release) with trust re-verification
- Five service modules, each single-responsibility
- The audit trail records field names for compliance, not field values for privacy

---

## Next Lesson

**Lesson 32 — The Permission Slip: Context Profiles & Rules** walks through `context_profiles.py` in full — how rules are stored, why priority order matters, and what `default_policy='deny'` means for a new agent who hasn't written any rules yet.
