# Lesson 55: The Regulatory Dossier — Compliance Export Generation

> **Beginner frame:** A compliance export is a dossier for review. It organizes evidence around regulatory themes, but it does not certify that an organization complies with any law.

> **Legal scope:** EU AI Act, HIPAA, and SEC-oriented exports are evidence packages for qualified reviewers. They are not legal advice, regulatory approval, or compliance certification.

**Layer:** 6 — Liability, Attribution & Regulatory Compliance
**Source:** `api/services/liability_compliance.py`
**Prerequisites:** Lesson 54
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A financial audit doesn't end with a spreadsheet. The auditor produces a formal report bound for regulators, with section headers, cross-references, and a cover page identifying the entity and the reporting period. Layer 6's compliance export generator does the same for agent transactions — it produces jurisdiction-specific PDF packages organized around EU AI Act, HIPAA, or SEC review themes, assembled from evidence across all six layers.

This lesson traces the `ExportScope` dataclass, the three export generators, and the scope validation that prevents a health regulator from receiving a finance-scoped export.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Describe the `ExportScope` dataclass and the four filter dimensions
- Name the three export types and the sections each PDF includes
- Explain how `_filter_tags()` and `_filter_tags_high_sensitivity()` scope exports to jurisdiction-relevant content
- Trace the `generate_eu_ai_act_pdf()` five-section structure and what Layer each section draws from
- Explain the HIPAA scope validation (health.* prefix requirement) and its 400 error path
- Describe the `ExportScope.low_risk_eu_scope` flag and when it bypasses sections

---

## The Three Export Types

```python
# api/models/liability.py
class ComplianceExportType(str, Enum):
    EU_AI_ACT = "eu_ai_act"
    HIPAA = "hipaa"
    SEC = "sec"
```

Each export type maps to a different regulatory framework and generates a different PDF structure:

| Export type | Regulatory context | Key content |
|------------|-------------------|-------------|
| `eu_ai_act` | EU AI Act (GPAI systems and high-risk AI systems) | System identification, human oversight records, transparency, auditability chain, incidents |
| `hipaa` | Health Insurance Portability and Accountability Act | PHI access log, agent identity assertions, HIPAA-flagged context disclosures |
| `sec` | Securities and Exchange Commission audit | Agent identity, all financial-domain disclosures, regulatory claims record |

---

## The `ExportScope` Dataclass

```python
# api/services/liability_compliance.py:31–46
@dataclass(frozen=True)
class ExportScope:
    export_type: ComplianceExportType
    executions: list[Mapping[str, Any]]
    workflow_steps: dict[UUID, list[Mapping[str, Any]]]
    ontology_tags_in_scope: set[str]
    sensitivity_tiers: dict[str, int]
    agent_dids_in_scope: set[str]
    from_date: datetime | None = None
    to_date: datetime | None = None
    requested_agent_did: str | None = None
    requested_execution_id: UUID | None = None
    requested_claim_id: UUID | None = None
    low_risk_eu_scope: bool = False
```

`ExportScope` is a frozen dataclass — it is computed once from the query parameters, validated, and passed to the PDF generator unchanged. The four filter dimensions:

| Dimension | How it filters |
|-----------|---------------|
| `requested_execution_id` | Export for one specific execution |
| `requested_claim_id` | Export for the execution referenced by one claim |
| `requested_agent_did` | All executions by one agent in the date window |
| `from_date` + `to_date` | All executions in the date window (admin) |

`ontology_tags_in_scope` is the union of all `ontology_tag` values from all `workflow_steps` for executions in scope. This set determines whether a jurisdiction-specific export is valid: a HIPAA export requires at least one `health.*` tag.

`low_risk_eu_scope` is computed during scope resolution: if all ontology tags in scope have `sensitivity_tier < 3`, the export is considered low-risk for EU AI Act purposes and some sections are reduced.

---

## Scope Validation: The HIPAA Guard

```python
# api/services/liability_compliance.py (generate_compliance_export)
if export_type == ComplianceExportType.HIPAA:
    health_tags = {tag for tag in scope.ontology_tags_in_scope if tag.startswith("health.")}
    if not health_tags:
        raise HTTPException(400, "HIPAA export requires at least one health.* ontology tag in scope")
```

The HIPAA export requires that the executions in scope touched at least one `health.*` ontology tag. Requesting a HIPAA export for an execution that only involved travel and finance tags returns 400 — there is no PHI-relevant content to export.

This validation is acceptance criterion 9: "HIPAA export returns 400 when no health.* tags in scope."

---

## `generate_eu_ai_act_pdf()` — Five Sections

The EU AI Act export is the most comprehensive. It assembles evidence from Layers 1–6:

**SECTION 1 — System Identification**
- All execution records in scope: execution ID, agent DID, workflow ID, outcome
- Per-execution step trust states from `liability_snapshots`: service name, trust tier, trust score at time of execution
- *Source layers: L5 (executions) + L6 (snapshots)*

**SECTION 2 — Human Oversight Records**
- Approved context bundles: which agent approved which fields for which workflow
- Layer 2 session assertions (if available): evidence that a human authorized high-sensitivity capabilities
- *Source layers: L5 (bundles) + L2 (session assertions)*

**SECTION 3 — Transparency Records**
- Per-execution authorized ontology tags
- Context disclosures in the execution window: fields disclosed, fields withheld, disclosure method
- *Source layers: L5 (workflow steps) + L4 (context_disclosures)*

**SECTION 4 — Auditability Chain**
- Chronological event log combining records from all layers:
  - L1 manifest crawl events
  - L4 context disclosure events
  - L5 execution reports
  - L6 liability snapshot creation
- *Source layers: L1, L4, L5, L6*

**SECTION 5 — Incident Records**
- Context mismatch events from evidence records
- Trust threshold failures (from snapshot step trust states where `trust_tier < min_trust_tier`)
- Liability claims filed against executions in scope
- *Source layers: L4 (mismatches) + L6 (snapshots + claims)*

---

## The PDF Generation Pattern

All three generators share the same ReportLab pattern from Layer 4:

```python
buffer = BytesIO()               # in-memory PDF buffer
pdf = canvas.Canvas(buffer, pagesize=LETTER)
y = _TOP_MARGIN                  # current y position on page

# Write content with y tracking
y = _draw_heading(pdf, "SECTION 1", y)
y = _draw_wrapped_line(pdf, content, y)  # auto-paginates when y < _BOTTOM_MARGIN

pdf.save()
return buffer.getvalue()         # returns bytes, not a file
```

**Auto-pagination:** `_draw_wrapped_line()` checks if `y < _BOTTOM_MARGIN` before drawing each line. When the page is full, it calls `pdf.showPage()` (starts a new page) and resets `y = _TOP_MARGIN`. This means arbitrarily long exports paginate correctly without the caller tracking page boundaries.

**`_new_pdf()` creates a cover page** with the export type, timestamp, execution count, and agent DIDs in scope. The cover page is always the first page, separate from the section content.

---

## `_scope_date_window()` — Evidence Window for the Export

```python
# api/services/liability_compliance.py:150–155
def _scope_date_window(scope):
    timestamps = [row["reported_at"] for row in scope.executions]
    return min(timestamps) - timedelta(minutes=35), max(timestamps) + timedelta(minutes=5)
```

The compliance export uses the same 35+5 minute window seen in Layer 5 verification and Layer 6 snapshots. For a multi-execution export, the window spans from 35 minutes before the earliest execution to 5 minutes after the latest. This captures all context disclosures that could be associated with any execution in scope.

---

## The `compliance_exports` Audit Log

Every call to `GET /liability/compliance/export` inserts a row in `compliance_exports`:

```sql
INSERT INTO compliance_exports (
    export_type, requested_by_did, execution_id, agent_did,
    claim_id, from_date, to_date, generated_at
)
```

The audit log records who requested what export and when. This is itself a compliance requirement: regulators often need to know not just what was in the export, but who generated it and when. The `compliance_exports` table provides that record.

---

## Exercise 1 — Generate an EU AI Act Export

After reporting at least one workflow execution:

```bash
EXECUTION_ID="<execution-uuid>"

curl -s "http://localhost:8000/v1/liability/compliance/export?\
export_type=eu_ai_act&\
execution_id=$EXECUTION_ID&\
requester_did=did:key:z6MkTestContextAgent" \
  -H "X-API-Key: dev-local-only" \
  --output /tmp/eu_ai_act_export.pdf

# Verify it's a valid PDF
head -c 5 /tmp/eu_ai_act_export.pdf  # should print: %PDF-
wc -c /tmp/eu_ai_act_export.pdf      # should be > 5000 bytes
```

**Expected:** A PDF file starting with `%PDF-` header, containing five sections.

---

## Exercise 2 — Verify HIPAA Scope Validation

Request a HIPAA export for an execution that doesn't involve health tags:

```bash
curl -s "http://localhost:8000/v1/liability/compliance/export?\
export_type=hipaa&\
execution_id=$EXECUTION_ID&\
requester_did=did:key:z6MkTestContextAgent" \
  -H "X-API-Key: dev-local-only" | python -m json.tool
```

**Expected:** 400 Bad Request with `"HIPAA export requires at least one health.* ontology tag in scope"` — unless the execution involved a step with a `health.*` ontology tag.

---

## Exercise 3 — Inspect the Audit Log

```bash
docker exec agentledger-db-1 psql -U agentledger -d agentledger \
  -c "SELECT export_type, requested_by_did, generated_at FROM compliance_exports ORDER BY generated_at DESC LIMIT 5;"
```

**Expected:** One row per `GET /liability/compliance/export` call, with the export type and requester DID.

---

## Best Practices

**The export is for regulators, not end users.** The PDF format is designed for professional review by compliance officers and legal teams. Field names use technical identifiers (`execution_id=...`, `agent_did=...`) rather than human-friendly labels — regulators need precision, not abstraction.

**`_draw_wrapped_line()` respects content boundaries.** Long field values (base64-encoded commitment hashes, full DID strings) are wrapped at 96 characters. The wrapping width is intentionally generous — `textwrap.wrap(text, width=96)` keeps identifiers intact rather than breaking them at hyphens.

**Recommended (not implemented here):** A streaming export for large date ranges (thousands of executions). The current implementation loads all data into memory before generating the PDF. For multi-month exports, a chunked generator with `pdf.showPage()` at data boundaries would prevent OOM errors.

---

## Interview Q&A

**Q: Why does the HIPAA export require `health.*` tags rather than checking sensitivity_tier directly?**
A: HIPAA applies specifically to protected health information. The `health.*` ontology tag prefix is the canonical marker for health-domain capabilities in AgentLedger. Sensitivity tier ≥ 3 covers multiple domains (health, finance, legal) — checking only tier would produce HIPAA exports for finance transactions, which is incorrect. The prefix check scopes the export to health-domain interactions specifically.

**Q: Why is the compliance export a PDF rather than structured JSON?**
A: Regulatory submissions often require human-readable documentation that can be printed, signed, and physically submitted. JSON is machine-readable but not regulatory-review. The PDF format, using the same ReportLab pattern as the Layer 4 GDPR export, produces a document suitable for professional review without manually assembling the report from database rows. Structured JSON is available via the evidence API for programmatic consumers.

**Q: What is the `low_risk_eu_scope` flag and when does it matter?**
A: When all ontology tags in scope have `sensitivity_tier < 3`, the export scope is classified as low-risk for EU AI Act purposes. The EU AI Act distinguishes between GPAI (general-purpose AI) systems and high-risk systems — Section 5 (Incident Records) requirements are relaxed for low-risk systems. The `low_risk_eu_scope` flag signals to the EU AI Act PDF generator that it can omit or simplify the incident records section.

---

## Key Takeaways

- Three export types: `eu_ai_act` (5 sections), `hipaa` (health.* filtered), `sec` (finance-domain)
- `ExportScope` is a frozen dataclass computed once from query parameters and passed to the PDF generator
- HIPAA scope validation: 400 if no `health.*` tags in the executions; prevents cross-jurisdiction exports
- EU AI Act PDF structure: System ID (L5+L6) → Human Oversight (L5+L2) → Transparency (L5+L4) → Auditability Chain (L1+L4+L5+L6) → Incidents (L4+L6)
- All exports log to `compliance_exports` table — the export itself is auditable
- PDF generation uses in-memory `BytesIO` + ReportLab `canvas.Canvas` with auto-pagination via `y < _BOTTOM_MARGIN` checks

---

## Next Lesson

**Lesson 56 — The Claims Lifecycle: Determination, Resolution & Appeals** traces the status transitions from `under_review` to `determined` to `resolved` and `appealed`, explains the `determination_version` incrementing pattern, and shows how the Redis claim status cache is maintained across transitions.
