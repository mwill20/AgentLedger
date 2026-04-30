# Lesson 39: The Compliance Dossier — PDF Export & Regulatory Package

> **Beginner frame:** A compliance export is a review package, not a certificate. AgentLedger gathers disclosure history into a PDF-style dossier so humans can inspect context decisions without querying raw tables.

**Layer:** 4 — Context Matching & Selective Disclosure  
**Source:** `api/services/context_compliance.py`, `api/routers/context.py`  
**Prerequisites:** Lesson 38  
**Estimated time:** 60 minutes

---

## Welcome Back, Agent Architect!

A financial regulator walks into your office and asks: "Show me every piece of user data your system has processed in the last six months." You hand them a binder. Inside: the agent's permission rules, every disclosure event (service, date, fields shared), every time a service over-requested, and every erasure action taken. Every claim in the binder can be traced to a database row.

`GET /v1/context/compliance/export/{agent_did}` generates that binder as a PDF. One HTTP call, one file, everything a regulator needs to assess GDPR/CCPA compliance for a single agent's context history.

---

## Learning Objectives

By the end of this lesson you will be able to:

- Describe the four sections of the compliance PDF and what each proves
- Trace the PDF generation flow through `generate_compliance_pdf()`
- Explain how ReportLab is used (canvas approach) and why word-wrap matters
- Identify the four data sources and the SQL queries that populate each section
- Explain why field values are absent from the PDF even though the PDF claims to be comprehensive
- Describe how erased disclosures appear in the PDF

---

## The Four-Section PDF

The compliance PDF is built by four drawing functions in sequence:

```
1. Cover page          — agent DID, export timestamp, API version
2. Context Profile     — active profile: default_policy + rules in table format
3. Disclosure History  — every context_disclosures row (field names, not values)
4. Mismatch Events     — every context_mismatch_events row for this agent
5. Erasure Records     — subset of disclosures where erased=true
```

Sections 3, 4, and 5 pull from the same tables but serve different regulatory purposes:

- **Disclosure History**: answers "what data did we process for this agent?"
- **Mismatch Events**: answers "did any service over-request data from this agent?"
- **Erasure Records**: answers "did this agent exercise their right to erasure, and when?"

---

## The Generation Flow

```python
async def generate_compliance_pdf(db, agent_did, api_version) -> bytes:
    profile = await _fetch_profile(db, agent_did)
    disclosures = await _fetch_disclosures(db, agent_did)
    mismatches = await _fetch_mismatches(db, agent_did)

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=LETTER)

    _draw_cover_page(canvas, agent_did, api_version)
    _draw_profile_section(canvas, profile)
    _draw_disclosure_section(canvas, disclosures)
    _draw_mismatch_section(canvas, mismatches)
    _draw_erasure_section(canvas, [d for d in disclosures if d["erased"]])

    canvas.save()
    return buffer.getvalue()
```

**Why `io.BytesIO()` instead of writing to a file?**  
The API returns the PDF bytes directly in the HTTP response (`media_type="application/pdf"`). Writing to a file would require cleanup logic and create a concurrency hazard if two requests export for the same agent simultaneously. An in-memory buffer is simpler and safer.

---

## ReportLab Canvas Approach

Layer 4 uses ReportLab's low-level `Canvas` API rather than the higher-level `platypus` (flow-based layout) API. This gives precise control over page coordinates but requires manual page-break handling.

### Layout constants

```python
_PAGE_WIDTH, _PAGE_HEIGHT = LETTER      # 612 x 792 points (8.5" x 11")
_LEFT_MARGIN = 54                        # 0.75"
_TOP_MARGIN = _PAGE_HEIGHT - 54          # 738 from bottom
_BOTTOM_MARGIN = 54
_LINE_HEIGHT = 14                        # points per line
_WRAP_WIDTH = 96                         # characters per wrapped line
```

### Word-wrap with auto page break

```python
def _draw_wrapped_line(canvas, y, text, x=_LEFT_MARGIN, font="Helvetica", size=9):
    lines = textwrap.wrap(str(text), _WRAP_WIDTH) or [""]
    for line in lines:
        if y <= _BOTTOM_MARGIN:
            canvas.showPage()            # auto page break
            canvas.setFont(font, size)
            y = _TOP_MARGIN
        canvas.drawString(x, y, line)
        y -= _LINE_HEIGHT
    return y
```

Every drawing function returns the updated `y` position so the next section can continue from where the previous one ended — on any page.

---

## The Four Data Fetches

### `_fetch_profile()`

```sql
SELECT p.id, p.default_policy, r.priority, r.scope_type, r.scope_value,
       r.permitted_fields, r.denied_fields, r.action
FROM context_profiles p
LEFT JOIN context_profile_rules r ON r.profile_id = p.id
WHERE p.agent_did = :agent_did AND p.is_active = true
ORDER BY r.priority ASC
```

If the agent has no profile, the PDF section renders "No active profile found."

### `_fetch_disclosures()`

```sql
SELECT id, service_id, ontology_tag, fields_disclosed, fields_withheld,
       disclosure_method, trust_tier_at_disclosure, erased, erased_at, created_at
FROM context_disclosures
WHERE agent_did = :agent_did
ORDER BY created_at DESC
```

Erased rows are included — they render with `[ERASED]` in place of the field-name arrays.

### `_fetch_mismatches()`

```sql
SELECT service_id, over_requested_fields, severity, resolved, created_at
FROM context_mismatch_events
WHERE agent_did = :agent_did
ORDER BY created_at DESC
```

### Erasure records

Filtered in Python from the disclosures list: `[d for d in disclosures if d["erased"]]`.

---

## The Filename

```python
def compliance_export_filename(agent_did: str) -> str:
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    safe_did = agent_did.replace(":", "_").replace("/", "_")
    return f"agentledger_compliance_{safe_did}_{date_str}.pdf"
```

The filename is set in the HTTP response `Content-Disposition` header:

```python
return Response(
    content=pdf_bytes,
    media_type="application/pdf",
    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
)
```

---

## What the PDF Proves (and Doesn't)

**Proves:**
- The agent's permission rules at the time of export
- Which services received which categories of data (field names) and when
- The trust tier of each service at disclosure time
- Whether any service over-requested data from this agent
- Whether the agent exercised their right to erasure and when

**Does not prove:**
- Field values (never stored in Layer 4)
- Whether the agent provided accurate values at disclose time
- Whether the service actually used the disclosed data appropriately

This is the honest boundary of what Layer 4 can certify. A full compliance package would also include Layer 3 attestation records (proving the service was audited) and Layer 5 workflow execution records (proving the context was used in a specific workflow).

---

## Exercise 1 — Download and Open the PDF

```bash
curl -s -o compliance_export.pdf \
  "http://localhost:8000/v1/context/compliance/export/did:key:z6MkTestContextAgent" \
  -H "X-API-Key: dev-local-only"

# On Windows, open with default PDF viewer
start compliance_export.pdf
```

Verify: the PDF contains all four sections. For a new agent with no disclosures, sections 3–5 should say "No records found."

---

## Exercise 2 — Trigger All Four Sections

Perform this sequence to populate every section before exporting:

```bash
# 1. Create a profile (Section 2: Context Profile)
# 2. Perform a match + disclose (Section 3: Disclosure History)
# 3. Trigger a mismatch (Section 4: Mismatch Events) — request an undeclared field
# 4. Revoke one disclosure (Section 5: Erasure Records)
# 5. Export the PDF and confirm all four sections have data
```

---

## Exercise 3 — Verify Erased Row Rendering

After revoking a disclosure (Lesson 38, Exercise 2), re-export the PDF. Find the erased row in the Disclosure History section. Confirm it renders as:

```
Date: 2026-04-29T10:23:00  Service: <uuid>  Fields: [ERASED]  Erased: YES
```

and that the same row also appears in the Erasure Records section.

---

## Best Practices

**The compliance PDF is a point-in-time snapshot.** It reflects the state of all four tables at the moment of generation. If an agent creates new disclosures after exporting, a new export must be generated. Consider building a timestamped archive of exported PDFs for long-running compliance programmes.

**Recommended (not implemented here):** A digital signature on the PDF bytes using the AgentLedger issuer key (from Layer 2). This would allow a regulator to verify that the PDF was generated by AgentLedger and has not been tampered with since generation — combining Layer 2's signing infrastructure with Layer 4's compliance output.

---

## Interview Q&A

**Q: Why use ReportLab's canvas API rather than a higher-level library?**  
A: The canvas API gives complete control over coordinates, which is necessary for the auto-page-break logic. The platypus (flow) API would handle page breaks automatically but makes it harder to maintain precise control over line spacing and section breaks for a structured compliance document.

**Q: Why is the compliance PDF delivered as bytes in the HTTP response rather than stored in S3 or similar?**  
A: Storage creates a new privacy surface. A compliance PDF containing an agent's full disclosure history is itself personal data. Generating it on-the-fly and never storing it avoids a second storage consent requirement. Agents who want to archive it can save the downloaded file themselves.

**Q: Can a service request a compliance PDF for an agent?**  
A: No. The endpoint is `GET /v1/context/compliance/export/{agent_did}` requiring an API key. Only the agent (or their platform) can export their own compliance dossier. An admin API key does not grant access to individual agents' compliance exports.

---

## Key Takeaways

- Four-section PDF: Cover → Profile → Disclosures → Mismatches → Erasures
- Generated in-memory using ReportLab Canvas — never written to disk
- All four data sources fetched with separate SQL queries
- Erased rows appear as `[ERASED]` — the fact of erasure is provable
- Field values are absent from the PDF — Layer 4 never stores them
- Filename includes agent DID and date for archive management

---

## Next Lesson

**Lesson 40 — The Stress Test: Hardening, Caching, Rate Limiting & Interview Readiness** covers Redis caching strategy, rate limit design, the full Layer 4 threat model, load test results, and the canonical interview questions for this layer.
