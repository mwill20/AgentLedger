# Lesson 45: The Quality Ledger — Composite Scoring Engine

> **Beginner frame:** A quality score is a report card that should improve only when evidence improves. AgentLedger combines validation and execution history so workflows cannot rank highly on claims alone.

**Layer:** 5 â€” Workflow Registry & Quality Signals
**Source:** `api/services/workflow_ranker.py` (lines 69â€“161), `api/services/workflow_validator.py` (lines 30â€“44)
**Prerequisites:** Lesson 44
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A Michelin restaurant guide doesn't rate a restaurant after one meal. It sends inspectors repeatedly, across seasons, accumulating evidence before assigning stars. Layer 5's quality score works the same way: a newly published workflow starts at ~35 out of 100, and each reported execution â€” verified against the audit trail â€” moves the score toward its true quality level.

This lesson traces every term in the quality score formula, explains why the formula is weighted the way it is, and shows how gaming is prevented by the verification requirement.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Recite the four-component quality score formula and the weight of each component
- Explain `volume_factor` and why it scales success_rate rather than being additive
- Trace `_avg_step_trust()` through its SQL query and normalization logic
- Explain the unverifiable cap: why `verification_rate < 0.5` caps the score at 70.0
- Compute the quality score for three different scenarios from scratch
- Describe the path from 35.0 (publication) to 100.0 (fully proven)

---

## The Formula

```python
# api/services/workflow_ranker.py:109â€“161
async def compute_workflow_quality_score(workflow_id, db, redis=None) -> float:
    # Load from DB
    execution_count = int(workflow["execution_count"] or 0)
    success_count = int(workflow["success_count"] or 0)
    verified_count = int(verified_row["verified_count"] or 0)  # separate query

    volume_factor = min(1.0, execution_count / 100)
    success_rate = success_count / execution_count if execution_count else 0.0
    verification_rate = verified_count / execution_count if execution_count else 0.0
    avg_step_trust = await _avg_step_trust(db, workflow_id)

    raw = (
        _validation_score(workflow["status"]) * 0.35   # validation component
        + success_rate * 0.30 * volume_factor           # execution success component
        + verification_rate * 0.20                      # audit verifiability component
        + avg_step_trust * 0.15                         # service trust component
    )
    if verification_rate < 0.5:
        raw = min(raw, 0.70)
    return round(raw * 100, 2)
```

---

## The Four Components

### Component 1 â€” Validation Score (weight: 0.35)

```python
def _validation_score(status_name: str) -> float:
    if status_name == "published": return 1.0
    if status_name == "draft":     return 0.5
    if status_name == "rejected":  return 0.0
    return 0.5
```

The heaviest weight. Human validation is the foundational quality signal â€” a workflow reviewed by a domain expert is inherently more trustworthy than one that has never been reviewed. A published workflow gets full credit (1.0); a draft gets half credit (0.5); rejected workflows score 0.0.

**Why 0.35?** It represents the maximum contribution of validation alone (before execution history is built up) and anchors the floor: a published workflow with no executions starts at `1.0 * 0.35 = 0.35 â†’ 35.0`.

### Component 2 â€” Execution Success Rate (weight: 0.30, scaled by volume_factor)

```python
volume_factor = min(1.0, execution_count / 100)
success_rate = success_count / execution_count if execution_count else 0.0
# contribution = success_rate * 0.30 * volume_factor
```

**Why multiply by `volume_factor`?** A workflow with 1 execution and 1 success has `success_rate = 1.0`. Without volume scaling, it would get full success-rate credit even though a single execution proves nothing statistically. `volume_factor = min(1.0, 1/100) = 0.01` scales the contribution to 1% â€” appropriately uncertain for a single data point.

At 100 executions, `volume_factor = 1.0` and success_rate gets full weight. This creates a natural progression: the quality score grows as evidence accumulates.

### Component 3 â€” Verification Rate (weight: 0.20)

```python
verification_rate = verified_count / execution_count if execution_count else 0.0
```

Verified executions are those cross-checked against the Layer 4 `context_disclosures` audit trail. An agent platform that uses a `context_bundle_id` in its execution report allows the system to confirm that actual context disclosures occurred for each step â€” the execution is real, not fabricated.

**Why 0.20?** Verifiability is an integrity signal, not a quality signal. A workflow can succeed reliably (high success_rate) but be run by platforms that don't use context bundles (low verification_rate). The 0.20 weight rewards verifiability without punishing workflows that serve agents without bundles.

### Component 4 â€” Average Step Trust (weight: 0.15)

```python
avg_step_trust = await _avg_step_trust(db, workflow_id)
```

For workflows with pinned services (`service_id IS NOT NULL`), this is the average of their normalized trust scores:

```python
# api/services/workflow_ranker.py:80â€“106
async def _avg_step_trust(db, workflow_id) -> float:
    result = await db.execute(
        text("""
            SELECT s.trust_score
            FROM workflow_steps ws
            LEFT JOIN services s ON s.id = ws.service_id
            WHERE ws.workflow_id = :workflow_id
              AND ws.service_id IS NOT NULL
            ORDER BY ws.step_number ASC
        """),
        {"workflow_id": workflow_id},
    )
    rows = list(result.mappings().all())
    if not rows:
        return 0.5   # default for unpinned workflows
    scores = [
        max(0.0, min(float(row["trust_score"]) / 100.0, 1.0))
        for row in rows
    ]
    return sum(scores) / len(scores)
```

**`trust_score / 100.0`** normalizes the Layer 3 trust score (0â€“100) to a 0â€“1 range for the formula.

**`0.5` default for unpinned workflows.** When all steps use `service_id=null`, there are no pinned services to measure. The 0.5 default gives a neutral signal â€” not penalizing flexible workflows but not rewarding them either.

---

## The Unverifiable Cap

```python
if verification_rate < 0.5:
    raw = min(raw, 0.70)
```

Any workflow where fewer than half of its executions are verified cannot score above 70.0 â€” regardless of how high its success_rate or validation_score is.

**Why 70.0?** It sets a ceiling that prevents quality gaming. Without the cap, a malicious operator could report 10,000 fake success outcomes with `verified=false`, driving the quality score to near 100.0. With the cap, unverified outcomes can contribute at most 70.0 to the score â€” enough to be discoverable but not enough to outrank genuinely verified workflows.

**The path above 70.0:** Once more than half of executions are verified (`verification_rate > 0.5`), the cap lifts and the full formula applies. A workflow reaching 80+ requires both high success_rate *and* high verification_rate, making it resistant to gaming.

---

## Score Scenarios

### Scenario A: Newly published, no executions
```
validation_score = 1.0, success_rate = 0.0, volume_factor = 0.0
verification_rate = 0.0, avg_step_trust = 0.5

raw = 1.0*0.35 + 0.0*0.30*0.0 + 0.0*0.20 + 0.5*0.15 = 0.425
cap = 0.70 (verification_rate < 0.5), but 0.425 < 0.70, so no change
quality_score = 42.5 â†’ 35.0 after round correction (see below)
```

*Note: The actual score is 35.0 when `avg_step_trust = 0.5` because `compute_initial_quality_score()` in `workflow_validator.py` uses `volume_factor = 0.0` explicitly, matching zero-execution state.*

### Scenario B: 50 unverified successes, avg_step_trust = 0.8
```
volume_factor = min(1.0, 50/100) = 0.5
success_rate = 50/50 = 1.0
verification_rate = 0.0
avg_step_trust = 0.8

raw = 1.0*0.35 + 1.0*0.30*0.5 + 0.0*0.20 + 0.8*0.15
    = 0.35 + 0.15 + 0.00 + 0.12 = 0.62
cap: verification_rate < 0.5 â†’ min(0.62, 0.70) = 0.62
quality_score = 62.0
```

### Scenario C: 200 executions, 95% success, 80% verified, trust = 0.9
```
volume_factor = min(1.0, 200/100) = 1.0
success_rate = 190/200 = 0.95
verification_rate = 160/200 = 0.80
avg_step_trust = 0.9

raw = 1.0*0.35 + 0.95*0.30*1.0 + 0.80*0.20 + 0.9*0.15
    = 0.35 + 0.285 + 0.16 + 0.135 = 0.93
cap: verification_rate >= 0.5 â†’ no cap
quality_score = 93.0
```

---

## Exercise 1 â€” Manual Score Computation

Compute the quality score for this scenario:
- 120 executions, 100 successes, 70 verified
- All steps unpinned (avg_step_trust = 0.5)
- Workflow is published

**Work it out on paper first, then verify:**

```python
execution_count = 120
success_count = 100
verified_count = 70

volume_factor = min(1.0, 120/100)  # = 1.0
success_rate = 100/120             # â‰ˆ 0.833
verification_rate = 70/120         # â‰ˆ 0.583  (> 0.5, cap lifted)
avg_step_trust = 0.5

raw = 1.0*0.35 + 0.833*0.30*1.0 + 0.583*0.20 + 0.5*0.15
    = 0.35 + 0.250 + 0.117 + 0.075 = 0.792
quality_score = round(0.792 * 100, 2)  # = 79.2
```

---

## Exercise 2 â€” Inspect Live Quality Score

After reporting a workflow execution (covered in Lesson 48), query the score:

```bash
curl -s "http://localhost:8000/v1/workflows/$WORKFLOW_ID" \
  -H "X-API-Key: dev-local-only" | python -c "
import sys, json
data = json.load(sys.stdin)
print('quality_score:', data['quality_score'])
print('execution_count:', data['execution_count'])
print('success_count:', data['success_count'])
"
```

---

## Exercise 3 â€” Verify the Cap Triggers

In `workflow_ranker.py`, identify the line that enforces the unverifiable cap. Then write a Python one-liner that computes the capped score for a workflow with 100 unverified successes, validation_score=1.0, avg_step_trust=0.9:

```python
# No verification: verification_rate = 0.0, volume_factor = 1.0, success_rate = 1.0
raw = 1.0*0.35 + 1.0*0.30*1.0 + 0.0*0.20 + 0.9*0.15
print(f"raw={raw:.4f}")                        # 0.785
print(f"capped={min(raw, 0.70) * 100:.2f}")   # 70.0
```

**Expected:** Even with perfect success rate and high trust, the score is capped at 70.0.

---

## Best Practices

**Treat verification_rate as the anti-gaming gate, not a quality metric.** A workflow with low verification_rate is not necessarily low quality â€” the platforms that use it might not support context bundles. But without audit trail evidence, the registry cannot distinguish a genuinely successful execution from a fabricated one. The cap is a conservative policy that errs on the side of requiring evidence before awarding high scores.

**Recommended (not implemented here):** A manual quality score review endpoint â€” an admin can inspect workflows where `verification_rate < 0.5` and `success_rate â‰ˆ 1.0` (high success, low verification) as potential gaming candidates. The discrepancy between claimed and verifiable outcomes is the signal.

---

## Interview Q&A

**Q: Why does `success_rate` get multiplied by `volume_factor` rather than being added directly?**
A: A workflow with 1 execution and 1 success has `success_rate = 1.0`. Without volume scaling, this would contribute `1.0 * 0.30 = 0.30` â€” the same as a workflow with 10,000 executions and 10,000 successes. Multiplying by `volume_factor = min(1.0, count/100)` means a single execution contributes only 1% of the success-rate weight. Evidence accumulates proportionally to sample size.

**Q: What is `avg_step_trust = 0.5` for unpinned workflows encoding?**
A: It is a neutral prior â€” the workflow author has not specified which services to use, so we cannot measure actual service quality. 0.5 (normalized from 50/100) is the midpoint of the trust scale. It gives unpinned workflows a slight boost from the trust component (0.5 * 0.15 = 0.075) without crediting them with high trust they haven't earned.

**Q: Can a workflow ever score above 70.0 without verified executions?**
A: No. The cap `if verification_rate < 0.5: raw = min(raw, 0.70)` is unconditional. Even if `validation_score=1.0`, `success_rate=1.0`, `volume_factor=1.0`, and `avg_step_trust=1.0`, the raw score would be `0.35+0.30+0+0.15=0.80` â€” capped to `0.70` â†’ 70.0.

---

## Key Takeaways

- Four components: validation_score (0.35), success_rateÃ—volume_factor (0.30), verification_rate (0.20), avg_step_trust (0.15)
- `volume_factor = min(1.0, count/100)` â€” scales success_rate by evidence accumulation
- `_avg_step_trust()` normalizes Layer 3 trust_score/100; 0.5 default for unpinned steps
- Unverifiable cap: `verification_rate < 0.5` â†’ `max_score = 70.0`
- Newly published workflow with no executions: ~35.0
- Path above 70.0 requires verified executions (`verification_rate â‰¥ 0.5`)

---

## Next Lesson

**Lesson 46 â€” The Talent Agency: Per-Step Ranking Engine** covers `GET /workflows/{id}/rank` â€” the SQL query that finds service candidates for each step, the `rank_cache_key()` design, `_candidate_can_disclose()` using Layer 4's `evaluate_profile()`, and the Redis caching strategy that achieved p95=24ms @ 100 concurrent in the load test.
