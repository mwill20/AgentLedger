# Lesson 59: The Foundation — Migration 007 & Layer 6 Schema

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `db/migrations/versions/007_layer6_liability.py`
**Prerequisites:** Lesson 51
**Estimated time:** 45 minutes

---

## Welcome Back, Agent Architect!

A courthouse's case management system is designed so that once a case file is opened, records can only be added, never erased. This design isn't accidental — it's a deliberate structural guarantee that prevents evidence tampering. Layer 6's database schema enforces the same principle through constraints, unique indexes, and the absence of DELETE operations anywhere in the service layer.

This lesson traces all five Layer 6 tables, explains each constraint and index, and shows how the schema's structure enforces the append-only evidence design at the database level.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Name all five Layer 6 tables and their primary constraints
- Explain the `UNIQUE(execution_id)` on `liability_snapshots` and why it's both a constraint and an index
- Explain `UNIQUE(execution_id, claimant_did)` on `liability_claims` and the deduplication it enforces
- Explain `UNIQUE(claim_id, source_table, source_id)` on `liability_evidence` and the `ON CONFLICT DO NOTHING` pattern
- Describe all eight indexes, why each exists, and which queries they serve
- Explain what `downgrade()` uses `CASCADE` and why ordering matters

---

## Migration 007 — Five Tables

Migration `007_layer6_liability.py` depends on `006` (Layer 5 workflows) — it references `workflow_executions(id)` and `workflows(id)` from Layer 5, and `services(id)` from Layer 1.

```python
revision = "007"
down_revision = "006"
```

**Dependency chain:** 001 (Layer 1) → 002 (Layer 2 identity) → 003 (Layer 2 sessions) → 004 (Layer 3 trust) → 005 (Layer 4 context) → 006 (Layer 5 workflows) → 007 (Layer 6 liability)

---

## Table 1: `liability_snapshots`

```sql
CREATE TABLE liability_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    execution_id UUID NOT NULL UNIQUE REFERENCES workflow_executions(id),
    workflow_id UUID NOT NULL REFERENCES workflows(id),
    agent_did TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    workflow_quality_score FLOAT NOT NULL,
    workflow_author_did TEXT NOT NULL,
    workflow_validator_did TEXT,
    workflow_validation_checklist JSONB,
    step_trust_states JSONB NOT NULL DEFAULT '[]',
    context_summary JSONB NOT NULL DEFAULT '{}',
    critical_mismatch_count INTEGER NOT NULL DEFAULT 0,
    agent_profile_default_policy TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Key constraints:**

- `execution_id NOT NULL UNIQUE` — one snapshot per execution, enforced at the DB level. The `UNIQUE` constraint also creates an implicit B-tree index, making `WHERE execution_id = :id` a fast primary key-equivalent lookup.
- `step_trust_states JSONB NOT NULL DEFAULT '[]'` — never NULL; always an array (possibly empty for a zero-step workflow).
- `context_summary JSONB NOT NULL DEFAULT '{}'` — never NULL; always a dict.
- `critical_mismatch_count INTEGER NOT NULL DEFAULT 0` — a first-class column (not inside JSONB) to enable index filtering.
- `workflow_validator_did TEXT` — nullable; NULL when no validator was assigned.

---

## Table 2: `liability_claims`

```sql
CREATE TABLE liability_claims (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    execution_id UUID NOT NULL REFERENCES workflow_executions(id),
    snapshot_id UUID NOT NULL REFERENCES liability_snapshots(id),
    claimant_did TEXT NOT NULL,
    claim_type TEXT NOT NULL,
    description TEXT NOT NULL,
    harm_value_usd FLOAT,
    status TEXT NOT NULL DEFAULT 'filed',
    reviewer_did TEXT,
    resolution_note TEXT,
    filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    evidence_gathered_at TIMESTAMPTZ,
    determined_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(execution_id, claimant_did)
);
```

**Key constraints:**

- `UNIQUE(execution_id, claimant_did)` — one claimant can file only one claim per execution. **Note:** The spec defines deduplication as `(execution_id, claimant_did, claim_type)`, but the DB constraint is `(execution_id, claimant_did)` — a claimant can't file both `service_failure` and `data_misuse` against the same execution at the database level. The service layer's `_check_duplicate_claim()` check uses `(execution_id, claimant_did, claim_type)`, providing a softer deduplication before the DB constraint is reached.
- `snapshot_id NOT NULL REFERENCES liability_snapshots(id)` — every claim is anchored to a snapshot. The service layer enforces this (422 if snapshot missing), and the FK constraint provides a final safety net.
- Status lifecycle timestamps (`evidence_gathered_at`, `determined_at`, `resolved_at`) are nullable — they are set at each transition. NULL means the transition hasn't occurred yet.

---

## Table 3: `liability_evidence`

```sql
CREATE TABLE liability_evidence (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id UUID NOT NULL REFERENCES liability_claims(id),
    evidence_type TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_id UUID NOT NULL,
    source_layer INTEGER NOT NULL,
    summary TEXT NOT NULL,
    raw_data JSONB NOT NULL DEFAULT '{}',
    gathered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(claim_id, source_table, source_id)
);
```

**Key constraints:**

- `UNIQUE(claim_id, source_table, source_id)` — one evidence record per `(claim, source)` pair. The same `workflow_executions` row can't be attached as evidence twice to the same claim. This is the database-level deduplication that backs the `ON CONFLICT DO NOTHING` in `_insert_evidence_if_missing()`.
- `raw_data JSONB NOT NULL DEFAULT '{}'` — never NULL; empty dict if no data was copied (e.g., GDPR-erased disclosure).
- No ON DELETE behavior — evidence records are never removed. The `liability_claims` FK is `REFERENCES liability_claims(id)` without `ON DELETE CASCADE`. Cascades would enable deletion. The absence of CASCADE is itself a design decision: evidence must be preserved even if the claim record were somehow deleted.

---

## Table 4: `liability_determinations`

```sql
CREATE TABLE liability_determinations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id UUID NOT NULL REFERENCES liability_claims(id),
    determination_version INTEGER NOT NULL DEFAULT 1,
    agent_weight FLOAT NOT NULL DEFAULT 0.0,
    service_weight FLOAT NOT NULL DEFAULT 0.0,
    workflow_author_weight FLOAT NOT NULL DEFAULT 0.0,
    validator_weight FLOAT NOT NULL DEFAULT 0.0,
    agent_did TEXT NOT NULL,
    service_id UUID REFERENCES services(id),
    workflow_author_did TEXT,
    validator_did TEXT,
    attribution_factors JSONB NOT NULL DEFAULT '[]',
    confidence FLOAT NOT NULL DEFAULT 0.5,
    determined_by TEXT NOT NULL DEFAULT 'system',
    determined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Key constraints:**

- No `UNIQUE(claim_id)` — multiple determinations per claim are intentional (for appeals). The service layer queries `MAX(determination_version)` to get the latest.
- `service_id UUID REFERENCES services(id)` — nullable; NULL if no specific service was identified as responsible. The FK without `NOT NULL` allows service-unknown determinations.
- `workflow_author_did TEXT` and `validator_did TEXT` — nullable; NULL if the workflow had no identified author or validator in the snapshot.
- `determined_by TEXT NOT NULL DEFAULT 'system'` — defaults to `'system'` for programmatic determinations; set to a human reviewer DID in the API flow.

---

## Table 5: `compliance_exports`

```sql
CREATE TABLE compliance_exports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    export_type TEXT NOT NULL,
    agent_did TEXT,
    service_id UUID REFERENCES services(id),
    execution_id UUID REFERENCES workflow_executions(id),
    claim_id UUID REFERENCES liability_claims(id),
    from_date TIMESTAMPTZ,
    to_date TIMESTAMPTZ,
    record_count INTEGER NOT NULL DEFAULT 0,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Design notes:**

- All filter columns are nullable — an export may be scoped by any combination of `agent_did`, `execution_id`, `claim_id`, or date range.
- `record_count` records the number of executions in the export scope — useful for regulatory reporting ("this export covers N agent transactions").
- No FK to `liability_snapshots` — the compliance export references executions, claims, agents, and services, but not snapshots directly. Snapshots are loaded during export generation; they don't need to be referenced in the audit log.

---

## The Eight Indexes

```sql
-- Snapshot lookup by execution_id (primary read path)
CREATE INDEX liability_snapshots_execution ON liability_snapshots(execution_id);

-- Claim lookup by execution_id (used by evidence gathering)
CREATE INDEX liability_claims_execution ON liability_claims(execution_id);

-- Claim filtering by status (admin list endpoint: WHERE status = 'filed')
CREATE INDEX liability_claims_status ON liability_claims(status);

-- Claim lookup by claimant (agent filing history)
CREATE INDEX liability_claims_claimant ON liability_claims(claimant_did);

-- Evidence lookup by claim + layer (source_layer filter for layer-specific evidence)
CREATE INDEX liability_evidence_claim ON liability_evidence(claim_id, source_layer);

-- Latest determination per claim (uses DESC ordering)
CREATE INDEX liability_determinations_claim
  ON liability_determinations(claim_id, determination_version DESC);

-- Compliance export history by agent
CREATE INDEX compliance_exports_agent
  ON compliance_exports(agent_did, generated_at DESC)
  WHERE agent_did IS NOT NULL;  -- partial index

-- Compliance export history by type
CREATE INDEX compliance_exports_type ON compliance_exports(export_type, generated_at DESC);
```

**Notable index choices:**

- `liability_snapshots_execution` is technically redundant with the `UNIQUE(execution_id)` constraint (which creates an implicit index). The explicit `CREATE INDEX` makes the index visible in explain plans and is a defensive declaration.

- `liability_determinations_claim ON (claim_id, determination_version DESC)` — the `DESC` ordering means the index is sorted in reverse version order. `SELECT ... ORDER BY determination_version DESC LIMIT 1` uses this index without a sort step.

- `compliance_exports_agent ... WHERE agent_did IS NOT NULL` — a **partial index**. Agent-did-based compliance exports are more common than system-wide exports. The partial index only indexes rows where `agent_did` is set, reducing index size while covering the most common query pattern.

---

## `downgrade()` — Cascade Ordering

```python
def downgrade():
    op.execute("DROP TABLE IF EXISTS compliance_exports CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_determinations CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_evidence CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_claims CASCADE;")
    op.execute("DROP TABLE IF EXISTS liability_snapshots CASCADE;")
```

Tables are dropped in reverse dependency order:
1. `compliance_exports` — references `liability_claims` and `workflow_executions`
2. `liability_determinations` — references `liability_claims`
3. `liability_evidence` — references `liability_claims`
4. `liability_claims` — references `liability_snapshots` and `workflow_executions`
5. `liability_snapshots` — references `workflow_executions` and `workflows`

`CASCADE` in `DROP TABLE` drops dependent foreign key constraints automatically — useful because PostgreSQL won't drop a referenced table without it. The ordering ensures no FK constraint violation: tables that reference others are dropped first.

---

## Exercise 1 — Inspect the Schema

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name LIKE 'liability_%'
ORDER BY table_name, ordinal_position;
"
```

**Expected:** All five Layer 6 tables with their columns, types, and defaults.

---

## Exercise 2 — Inspect the Indexes

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger -c "
SELECT indexname, tablename, indexdef
FROM pg_indexes
WHERE tablename LIKE 'liability_%'
ORDER BY tablename, indexname;
"
```

**Expected:** Eight indexes plus the implicit index from each UNIQUE constraint.

---

## Interview Q&A

**Q: Why is there no `UNIQUE(claim_id, determination_version)` constraint on `liability_determinations`?**
A: The appeal → re-determination flow would race without an explicit version assignment. The service layer queries `MAX(determination_version) + 1` to assign the next version — if two concurrent re-determination requests raced, both could read the same MAX and produce duplicate version numbers. A unique constraint would cause one to fail with a constraint violation. The current design accepts concurrent determination race conditions as an edge case that human review handles (the claim would have two `version=2` records, which the service returns the latest of by `created_at`). A production system might add an advisory lock around the version increment.

**Q: Why does `liability_evidence` not use `ON DELETE CASCADE` from `liability_claims`?**
A: Evidence must be preserved for regulatory and forensic purposes even if the claim itself were deleted. `ON DELETE CASCADE` would silently remove evidence if a claim were deleted — a serious compliance risk. The absence of CASCADE is a deliberate choice to prevent accidental evidence loss.

---

## Key Takeaways

- Five tables: snapshots (unique per execution), claims (unique per execution+claimant), evidence (unique per claim+source), determinations (versioned per claim), compliance_exports (audit log)
- `liability_snapshots.UNIQUE(execution_id)` — one snapshot per execution, enforced at DB level
- `liability_evidence.UNIQUE(claim_id, source_table, source_id)` — backs `ON CONFLICT DO NOTHING` in service layer
- Eight indexes: by execution, by status, by claimant, by layer, by determination version (DESC), by agent+date (partial)
- Downgrade drops tables in reverse dependency order; `CASCADE` drops dependent FKs automatically
- No `ON DELETE CASCADE` on evidence — forensic records are never removed

---

## Next Lesson

**Lesson 60 — The Final Debrief: Full Layer 6 Flow & Interview Readiness** closes the complete AgentLedger curriculum with the end-to-end liability flow, the Layer 6 invariant, five canonical interview questions, and a summary of the full six-layer stack.
