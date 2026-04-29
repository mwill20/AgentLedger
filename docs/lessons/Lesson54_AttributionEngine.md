# Lesson 54: The Scales of Justice — The 11-Factor Attribution Engine

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/services/liability_attribution.py`
**Prerequisites:** Lesson 53
**Estimated time:** 75 minutes

---

## Welcome Back, Agent Architect!

An insurance adjuster doesn't assign blame arbitrarily after an accident. They examine physical evidence, review inspection records, look at maintenance logs, and check whether safety systems were operating. The resulting report says "the vehicle's brakes were last inspected 14 months ago, the manufacturer's recommended interval is 12 months, and brake failure was the proximate cause" — a structured, evidence-based attribution, not a snap judgment.

Layer 6's attribution engine is that adjuster. Given a claim, a frozen snapshot, and gathered evidence from eight sources, `compute_attribution()` evaluates eleven factors across four actors and returns a normalized set of responsibility weights.

---

## Learning Objectives

By the end of this lesson you will be able to:

- State the starting weights for each actor and why they are equal
- Name all eleven factors, the actor each shifts weight to, and the base contribution magnitude
- Trace `compute_attribution()` through its weight-update loop
- Explain `_normalize_weights()` and why the final sum guarantee matters
- Explain the confidence formula and what 0.3 represents as the floor
- Describe the "anti-gaming" property: using an undertrusted service shifts weight to the agent

---

## The Four Actors and Starting Weights

Every attribution begins with equal priors:

```python
# api/services/liability_attribution.py:416–421
weights = {
    "agent": 0.25,
    "service": 0.25,
    "workflow_author": 0.25,
    "validator": 0.25,
}
```

**Why 25/25/25/25?** Without any evidence, all four actors contributed equally to the conditions that made the harm possible. The agent chose to run the workflow with a particular service. The service declared certain capabilities and received context. The workflow author specified the trust thresholds and context requirements. The validator reviewed and approved the spec. Equal priors are the fair starting point before evidence shifts the balance.

**Who are the four actors?**

| Actor | Who they are | What they controlled |
|-------|-------------|---------------------|
| `agent` | The agent DID that ran the workflow | Which service to use, whether to proceed despite warnings |
| `service` | The Layer 1 registered service that received context | What capabilities it declared, how it handled context |
| `workflow_author` | The author DID from `workflows.author_did` | Trust thresholds, step design, fallback logic |
| `validator` | The validator DID from `workflow_validations` | Whether the checklist was applied correctly |

---

## The Eleven Factors

### Factors That Increase Agent Weight

| Factor | Base contribution | What it detects |
|--------|-----------------|----------------|
| `service_trust_below_step_minimum` | +0.15 | Agent used a service whose `trust_score` was below the step's `min_trust_score` at execution time |
| `service_trust_tier_below_step_minimum` | +0.20 | Agent used a service whose `trust_tier` was below the step's `min_trust_tier` |
| `service_revoked_before_execution` | +0.25 | Service was revoked before the execution was reported — agent proceeded with a known-bad service |
| `critical_context_mismatch_ignored` | +0.20 | Layer 4 detected a critical-severity context mismatch during the execution window |

**The anti-gaming property:** If an agent deliberately uses an undertrusted service (because it's cheaper, faster, or more permissive), `service_trust_below_step_minimum` shifts weight to the agent — not away from them. The attribution engine does not reward the choice to ignore trust requirements.

### Factors That Increase Service Weight

| Factor | Base contribution | What it detects |
|--------|-----------------|----------------|
| `service_capability_not_verified` | +0.15 | The service declared a capability but it was not independently verified (no Layer 3 attestation) |
| `service_context_over_request` | +0.20 | A context mismatch event shows the service requested more context than it was permitted |
| `service_revoked_after_execution_for_related_reason` | +0.15 | The service was revoked after the execution for a reason related to the claim type |

**Revocation timing matters:** `service_revoked_before_execution` (+0.25 → agent) and `service_revoked_after_execution_for_related_reason` (+0.15 → service) are distinct factors. A pre-execution revocation means the agent knowingly used a revoked service. A post-execution revocation for a related reason (e.g., a `service_failure` claim and a capability-related revocation) suggests the service had a systematic problem that later became visible.

### Factors That Increase Workflow Author Weight

| Factor | Base contribution | What it detects |
|--------|-----------------|----------------|
| `workflow_quality_score_low_at_execution` | +0.10 | Workflow `quality_score` was below 60.0 at execution time (per the snapshot) |
| `workflow_trust_threshold_inadequate` | +0.15 | A required step with `sensitivity_tier >= 3` has `min_trust_tier < 3` |
| `workflow_no_fallback_for_critical_step` | +0.10 | A required step with `sensitivity_tier >= 3` has no fallback step |

**Quality score threshold is 60.0, not 70.0.** The 70.0 unverifiable cap from Layer 5 is a display and search ranking gate. For attribution, a workflow that was running with `quality_score < 60.0` is a signal that the author submitted a low-confidence workflow that was used in a consequential execution.

### Factors That Increase Validator Weight

| Factor | Base contribution | What it detects |
|--------|-----------------|----------------|
| `validator_approved_inadequate_trust_threshold` | +0.10 | Checklist said `trust_thresholds_appropriate=true` but a threshold is actually inadequate |
| `validator_approved_non_minimal_context` | +0.10 | Checklist said `context_minimal=true` but a mismatch event shows over-request |

**Validator factors are conditional.** `_validator_approved_inadequate_trust_threshold()` only fires if the checklist shows `trust_thresholds_appropriate=true` AND the thresholds are actually inadequate. If the validator didn't check that box, they can't be held responsible for failing to enforce it — a different factor would fire instead (or no factor fires for the validator).

---

## `compute_attribution()` — The Weight-Update Loop

```python
# api/services/liability_attribution.py:405–459
def compute_attribution(*, claim, snapshot, evidence, workflow, workflow_steps, execution):
    weights = {"agent": 0.25, "service": 0.25, "workflow_author": 0.25, "validator": 0.25}
    applied_factors = []

    for factor_name, factor_def in ATTRIBUTION_FACTORS.items():
        applies, evidence_ids = factor_applies(factor_name, ...)
        if not applies:
            continue

        actor = factor_def["shifts_weight_to"]
        contribution = float(factor_def["base_contribution"])
        other_actors = [a for a in ACTORS if a != actor]
        per_other = contribution / len(other_actors)

        weights[actor] += contribution
        for other in other_actors:
            weights[other] = max(0.0, weights[other] - per_other)

        applied_factors.append(AttributionFactor(
            factor=factor_name,
            actor=actor,
            weight_contribution=contribution,
            evidence_ids=evidence_ids,
        ))

    confidence = min(1.0, 0.3 + len(applied_factors) * 0.1)
    return AttributionResult(
        weights=_normalize_weights(weights),
        applied_factors=applied_factors,
        confidence=round(confidence, 2),
    )
```

**The weight shift mechanism:** When a factor fires, the `contribution` is added to the responsible actor's weight and divided equally across the other three actors as a deduction (`per_other = contribution / 3`). Each other actor's weight is reduced by `per_other`, floored at 0.0 (`max(0.0, ...)`).

**Example:** If `service_trust_below_step_minimum` fires (contribution=0.15, shifts to `agent`):
- `agent`: 0.25 + 0.15 = 0.40
- Each other actor: 0.25 - (0.15/3) = 0.25 - 0.05 = 0.20

After one factor: `{agent: 0.40, service: 0.20, workflow_author: 0.20, validator: 0.20}` → sums to 1.00.

If multiple factors fire for the same actor, weights can grow significantly. If multiple factors fire for different actors, the system balances — no single actor's weight goes above 1.0 because other actors' floors at 0.0 prevent negative weights.

---

## `_normalize_weights()` — The Final Sum Guarantee

```python
# api/services/liability_attribution.py:394–402
def _normalize_weights(weights):
    total = sum(weights.values())
    normalized = {actor: round(value / total, 4) for actor, value in weights.items()}
    delta = round(1.0 - sum(normalized.values()), 4)
    if delta:
        actor = max(normalized, key=normalized.get)
        normalized[actor] = round(normalized[actor] + delta, 4)
    return normalized
```

After all factors are applied, floating-point rounding errors can cause the sum to be `0.9999` or `1.0001`. The normalization step:
1. Divides each weight by the total (`/total` makes them relative, not absolute)
2. Rounds each to 4 decimal places
3. Computes the residual delta (`1.0 - sum(rounded)`)
4. Adds the delta to the actor with the highest weight (assigning rounding slack to the most responsible party)

**Why must weights sum to exactly 1.0?** The weights represent a complete attribution — 100% of responsibility is distributed. A sum other than 1.0 would imply either double-counting or missing attribution. Downstream systems (insurance pricing, regulatory reports) depend on this guarantee.

---

## The Confidence Formula

```python
confidence = min(1.0, 0.3 + len(applied_factors) * 0.1)
```

| Factors applied | Confidence |
|----------------|-----------|
| 0 (no factors fired) | 0.30 |
| 1 | 0.40 |
| 3 | 0.60 |
| 5 | 0.80 |
| 7+ | 1.00 (capped) |

**Why 0.3 as the floor?** A determination with zero factors fired still carries minimum confidence — the system evaluated the evidence and found no liability signals, which is itself information. Zero confidence would imply "we have no idea," which is not true when evidence was gathered and all factors evaluated to false.

**Why 0.1 per factor?** Each factor that fires adds 10% confidence. At 7 factors, confidence reaches the maximum. This reflects the intuition that more corroborating evidence signals (each independently verifiable) produce higher determination confidence.

---

## `FACTOR_EVALUATORS` Registry

```python
FACTOR_EVALUATORS = {
    "service_trust_below_step_minimum": _service_trust_below_step_minimum,
    "service_trust_tier_below_step_minimum": _service_trust_tier_below_step_minimum,
    ...
}
```

The registry decouples factor metadata (`ATTRIBUTION_FACTORS` — name, target actor, base contribution, evidence source) from factor evaluation logic (`FACTOR_EVALUATORS` — Python functions that inspect evidence and return `(bool, [evidence_ids])`). Adding a new factor requires only:
1. A new entry in `ATTRIBUTION_FACTORS` with the factor's metadata
2. A new function in `FACTOR_EVALUATORS` that implements the detection logic

No changes to `compute_attribution()` itself.

---

## Exercise 1 — Trace a Two-Factor Determination Manually

Given:
- Starting weights: `{agent: 0.25, service: 0.25, workflow_author: 0.25, validator: 0.25}`
- `service_trust_below_step_minimum` fires (contribution=0.15, shifts to `agent`)
- `critical_context_mismatch_ignored` fires (contribution=0.20, shifts to `agent`)

Compute the final weights before normalization. Then normalize.

**Expected:**
```
After factor 1: agent=0.40, service=0.20, workflow_author=0.20, validator=0.20
After factor 2: agent=0.60, service=0.133, workflow_author=0.133, validator=0.133
Normalized: agent≈0.60, service≈0.133, workflow_author≈0.133, validator≈0.133
```

*(Exact values depend on floating-point rounding — use `max(0.0, ...)` to floor at 0.)*

---

## Exercise 2 — Trigger a Real Attribution

After filing a claim and gathering evidence (Lesson 53):

```bash
CLAIM_ID="<claim-uuid>"

curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/determine" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

**Expected:** A `determination` object with `agent_weight`, `service_weight`, `workflow_author_weight`, `validator_weight` (summing to 1.0), `confidence`, and `attribution_factors` listing which factors fired.

---

## Exercise 3 — Verify Sum = 1.0

```bash
curl -s -X POST "http://localhost:8000/v1/liability/claims/$CLAIM_ID/determine" \
  -H "X-API-Key: dev-local-only" | python -c "
import sys, json
d = json.load(sys.stdin)
det = d.get('determination', {})
total = (det.get('agent_weight', 0) +
         det.get('service_weight', 0) +
         det.get('workflow_author_weight', 0) +
         det.get('validator_weight', 0))
print(f'Sum of weights: {total:.4f}')
print(f'Exact 1.0: {abs(total - 1.0) < 0.0001}')
"
```

**Expected:** Sum = 1.0000, Exact 1.0: True.

---

## Best Practices

**Attribution is advisory, not authoritative.** The engine returns a `confidence` value precisely because its conclusions can be contested. Low confidence (0.3–0.5) should trigger mandatory human review before the claim is resolved. High confidence (0.8–1.0) still requires a reviewer — the engine produces evidence for human decision-makers, not binding rulings.

**Recommended (not implemented here):** A claims dashboard that flags high-agent-weight, high-confidence determinations for automated follow-up — e.g., a notification to the agent platform that they should investigate their service selection logic.

---

## Interview Q&A

**Q: Why do the starting weights begin at 25/25/25/25 rather than weighting the actor named in the claim type?**
A: The claim type represents the claimant's hypothesis, not an established fact. Starting with equal priors ensures the attribution is evidence-driven rather than theory-driven. A `service_failure` claim might ultimately assign most weight to the workflow author if the workflow's trust thresholds were insufficient — the claim type alone is not attribution.

**Q: Why does using an undertrusted service shift weight to the agent rather than the service?**
A: The service declared its trust tier accurately. The workflow specified a minimum trust threshold. The agent chose to use a service that didn't meet that threshold — either ignoring the Layer 5 rank endpoint's `can_disclose` warning or bypassing it. The responsibility for choosing an unsuitable service lies with the decision-maker who made the choice, not the service that accurately represented itself.

**Q: What happens if no factors fire for a claim?**
A: The weights remain at 25/25/25/25 after normalization (equal attribution). Confidence is 0.30 (the floor). The determination record is written with these values. This outcome means the attribution engine found no distinguishing evidence that any actor bore more responsibility than any other. A human reviewer can still override the weights in the resolution note.

---

## Key Takeaways

- Starting weights: 25/25/25/25 — equal priors, evidence shifts from there
- 11 factors: 4 shift to agent, 3 to service, 3 to workflow_author, 2 to validator
- Weight shift: contribution added to responsible actor, divided equally across the other three, floored at 0.0
- `_normalize_weights()` guarantees exact sum of 1.0 after floating-point rounding
- Confidence: `min(1.0, 0.3 + factors_applied * 0.1)` — 0.30 floor, 1.0 cap at 7 factors
- Attribution is advisory — weights are evidence for human decision-makers, not binding determinations

---

## Next Lesson

**Lesson 55 — The Regulatory Dossier: Compliance Export Generation** covers `liability_compliance.py` — the EU AI Act, HIPAA, and SEC-ready PDF generation flow, the `ExportScope` dataclass, jurisdiction-specific scope validation, and how the export combines evidence from all six layers.
