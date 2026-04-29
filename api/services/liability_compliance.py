"""Layer 6 regulatory compliance export generation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from textwrap import wrap
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.liability import ComplianceExportType

_PAGE_WIDTH, _PAGE_HEIGHT = LETTER
_LEFT_MARGIN = 54
_TOP_MARGIN = _PAGE_HEIGHT - 54
_BOTTOM_MARGIN = 54
_LINE_HEIGHT = 14
_WRAP_WIDTH = 96


@dataclass(frozen=True)
class ExportScope:
    """Resolved execution scope for a liability compliance export."""

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


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    """Render a scalar or timestamp for PDF output."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _csv(values: Any) -> str:
    """Render array-like values without exposing field contents beyond names."""
    if not values:
        return ""
    if isinstance(values, (list, tuple, set)):
        return ", ".join(str(value) for value in values)
    return str(values)


def _json_list(value: Any) -> list[Any]:
    """Normalize JSONB list values from DB rows and test doubles."""
    if isinstance(value, list):
        return value
    return []


def _json_dict(value: Any) -> dict[str, Any]:
    """Normalize JSONB dict values from DB rows and test doubles."""
    if isinstance(value, dict):
        return value
    return {}


def _draw_wrapped_line(
    pdf: canvas.Canvas,
    text_value: str,
    y: float,
    *,
    font: str = "Helvetica",
    size: int = 9,
    indent: int = 0,
) -> float:
    """Draw wrapped text and return the next y position."""
    pdf.setFont(font, size)
    lines = wrap(text_value, width=max(20, _WRAP_WIDTH - indent)) or [""]
    for line in lines:
        if y < _BOTTOM_MARGIN:
            pdf.showPage()
            y = _TOP_MARGIN
            pdf.setFont(font, size)
        pdf.drawString(_LEFT_MARGIN + indent, y, line)
        y -= _LINE_HEIGHT
    return y


def _draw_heading(pdf: canvas.Canvas, title: str, y: float) -> float:
    """Draw a PDF section heading."""
    if y < _BOTTOM_MARGIN + 36:
        pdf.showPage()
        y = _TOP_MARGIN
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(_LEFT_MARGIN, y, title)
    return y - (_LINE_HEIGHT * 1.5)


def _new_pdf(title: str, scope: ExportScope) -> tuple[BytesIO, canvas.Canvas, float]:
    """Create a PDF with a cover block."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)
    y = _TOP_MARGIN
    y = _draw_wrapped_line(pdf, title, y, font="Helvetica-Bold", size=18)
    y -= _LINE_HEIGHT
    y = _draw_wrapped_line(pdf, f"Export type: {scope.export_type}", y, size=11)
    y = _draw_wrapped_line(pdf, f"Export timestamp (UTC): {_iso(_utc_now())}", y, size=11)
    y = _draw_wrapped_line(pdf, f"Execution count: {len(scope.executions)}", y, size=11)
    y = _draw_wrapped_line(
        pdf,
        f"Agents in scope: {_csv(sorted(scope.agent_dids_in_scope))}",
        y,
        size=11,
    )
    pdf.showPage()
    return buffer, pdf, _TOP_MARGIN


def _finish_pdf(buffer: BytesIO, pdf: canvas.Canvas) -> bytes:
    """Finalize and return PDF bytes."""
    pdf.save()
    return buffer.getvalue()


def _execution_ids(scope: ExportScope) -> list[UUID]:
    """Return execution ids in scope."""
    return [row["id"] for row in scope.executions]


def _scope_date_window(scope: ExportScope) -> tuple[datetime | None, datetime | None]:
    """Return the broad execution evidence window for scoped records."""
    timestamps = [row["reported_at"] for row in scope.executions if row.get("reported_at")]
    if not timestamps:
        return scope.from_date, scope.to_date
    return min(timestamps) - timedelta(minutes=35), max(timestamps) + timedelta(minutes=5)


async def _load_execution_by_id(
    db: AsyncSession,
    execution_id: UUID,
) -> list[Mapping[str, Any]]:
    """Load a single execution row."""
    result = await db.execute(
        text(
            """
            SELECT
                we.id,
                we.workflow_id,
                we.agent_did,
                we.outcome,
                we.steps_completed,
                we.steps_total,
                we.failure_step_number,
                we.failure_reason,
                we.duration_ms,
                we.reported_at,
                we.verified,
                w.name AS workflow_name
            FROM workflow_executions we
            JOIN workflows w ON w.id = we.workflow_id
            WHERE we.id = :execution_id
            """
        ),
        {"execution_id": execution_id},
    )
    return list(result.mappings().all())


async def _load_execution_by_claim(
    db: AsyncSession,
    claim_id: UUID,
) -> list[Mapping[str, Any]]:
    """Load the execution referenced by one claim."""
    result = await db.execute(
        text(
            """
            SELECT
                we.id,
                we.workflow_id,
                we.agent_did,
                we.outcome,
                we.steps_completed,
                we.steps_total,
                we.failure_step_number,
                we.failure_reason,
                we.duration_ms,
                we.reported_at,
                we.verified,
                w.name AS workflow_name
            FROM liability_claims lc
            JOIN workflow_executions we ON we.id = lc.execution_id
            JOIN workflows w ON w.id = we.workflow_id
            WHERE lc.id = :claim_id
            """
        ),
        {"claim_id": claim_id},
    )
    return list(result.mappings().all())


async def _load_executions_by_agent(
    db: AsyncSession,
    *,
    agent_did: str | None,
    from_date: datetime | None,
    to_date: datetime | None,
) -> list[Mapping[str, Any]]:
    """Load executions by agent/date filters."""
    conditions = []
    params: dict[str, Any] = {}
    if agent_did is not None:
        conditions.append("we.agent_did = :agent_did")
        params["agent_did"] = agent_did
    if from_date is not None:
        conditions.append("we.reported_at >= :from_date")
        params["from_date"] = from_date
    if to_date is not None:
        conditions.append("we.reported_at <= :to_date")
        params["to_date"] = to_date
    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    result = await db.execute(
        text(
            f"""
            SELECT
                we.id,
                we.workflow_id,
                we.agent_did,
                we.outcome,
                we.steps_completed,
                we.steps_total,
                we.failure_step_number,
                we.failure_reason,
                we.duration_ms,
                we.reported_at,
                we.verified,
                w.name AS workflow_name
            FROM workflow_executions we
            JOIN workflows w ON w.id = we.workflow_id
            {where_sql}
            ORDER BY we.reported_at DESC
            """
        ),
        params,
    )
    return list(result.mappings().all())


async def _load_workflow_steps(
    db: AsyncSession,
    workflow_id: UUID,
) -> list[Mapping[str, Any]]:
    """Load steps and sensitivity tiers for a workflow."""
    result = await db.execute(
        text(
            """
            SELECT
                ws.id,
                ws.workflow_id,
                ws.step_number,
                ws.name,
                ws.ontology_tag,
                ws.service_id,
                ws.is_required,
                ws.fallback_step_number,
                ws.context_fields_required,
                ws.context_fields_optional,
                ws.min_trust_tier,
                ws.min_trust_score,
                COALESCE(ot.sensitivity_tier, 1) AS sensitivity_tier
            FROM workflow_steps ws
            LEFT JOIN ontology_tags ot ON ot.tag = ws.ontology_tag
            WHERE ws.workflow_id = :workflow_id
            ORDER BY ws.step_number ASC
            """
        ),
        {"workflow_id": workflow_id},
    )
    return list(result.mappings().all())


async def resolve_export_scope(
    *,
    export_type: ComplianceExportType,
    agent_did: str | None,
    execution_id: UUID | None,
    claim_id: UUID | None,
    from_date: datetime | None,
    to_date: datetime | None,
    db: AsyncSession,
) -> ExportScope:
    """Resolve and validate the execution scope for a compliance export."""
    if execution_id is not None:
        executions = await _load_execution_by_id(db, execution_id)
    elif claim_id is not None:
        executions = await _load_execution_by_claim(db, claim_id)
    else:
        executions = await _load_executions_by_agent(
            db,
            agent_did=agent_did,
            from_date=from_date,
            to_date=to_date,
        )

    if not executions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no records found in compliance export scope",
        )

    workflow_steps: dict[UUID, list[Mapping[str, Any]]] = {}
    ontology_tags: set[str] = set()
    sensitivity_tiers: dict[str, int] = {}
    for execution in executions:
        workflow_id = execution["workflow_id"]
        if workflow_id not in workflow_steps:
            workflow_steps[workflow_id] = await _load_workflow_steps(db, workflow_id)
        for step in workflow_steps[workflow_id]:
            ontology_tags.add(step["ontology_tag"])
            sensitivity_tiers[step["ontology_tag"]] = int(step["sensitivity_tier"] or 1)

    if export_type == "hipaa" and not any(tag.startswith("health.") for tag in ontology_tags):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="HIPAA export requires health.* ontology tags in scope",
        )
    if export_type == "sec" and not any(
        tag.startswith("finance.investment.") for tag in ontology_tags
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SEC export requires finance.investment.* ontology tags in scope",
        )

    low_risk_eu_scope = export_type == "eu_ai_act" and not any(
        tier >= 3 for tier in sensitivity_tiers.values()
    )
    return ExportScope(
        export_type=export_type,
        executions=executions,
        workflow_steps=workflow_steps,
        ontology_tags_in_scope=ontology_tags,
        sensitivity_tiers=sensitivity_tiers,
        agent_dids_in_scope={row["agent_did"] for row in executions},
        from_date=from_date,
        to_date=to_date,
        requested_agent_did=agent_did,
        requested_execution_id=execution_id,
        requested_claim_id=claim_id,
        low_risk_eu_scope=low_risk_eu_scope,
    )


async def _fetch_snapshots(
    db: AsyncSession,
    scope: ExportScope,
) -> list[Mapping[str, Any]]:
    """Load liability snapshots for scoped executions."""
    rows: list[Mapping[str, Any]] = []
    for execution_id in _execution_ids(scope):
        result = await db.execute(
            text(
                """
                SELECT
                    id,
                    execution_id,
                    workflow_id,
                    captured_at,
                    workflow_quality_score,
                    workflow_author_did,
                    workflow_validator_did,
                    step_trust_states,
                    context_summary,
                    critical_mismatch_count
                FROM liability_snapshots
                WHERE execution_id = :execution_id
                """
            ),
            {"execution_id": execution_id},
        )
        row = result.mappings().first()
        if row is not None:
            rows.append(row)
    return rows


async def _fetch_context_disclosures(
    db: AsyncSession,
    scope: ExportScope,
) -> list[Mapping[str, Any]]:
    """Load context disclosures in the broad execution window."""
    from_window, to_window = _scope_date_window(scope)
    rows: list[Mapping[str, Any]] = []
    for agent_did in scope.agent_dids_in_scope:
        result = await db.execute(
            text(
                """
                SELECT
                    id,
                    agent_did,
                    service_id,
                    ontology_tag,
                    fields_requested,
                    fields_disclosed,
                    fields_withheld,
                    fields_committed,
                    disclosure_method,
                    trust_score_at_disclosure,
                    trust_tier_at_disclosure,
                    erased,
                    created_at
                FROM context_disclosures
                WHERE agent_did = :agent_did
                  AND (:from_date IS NULL OR created_at >= :from_date)
                  AND (:to_date IS NULL OR created_at <= :to_date)
                ORDER BY created_at ASC
                """
            ),
            {"agent_did": agent_did, "from_date": from_window, "to_date": to_window},
        )
        rows.extend(result.mappings().all())
    return rows


async def _fetch_context_bundles(
    db: AsyncSession,
    scope: ExportScope,
) -> list[Mapping[str, Any]]:
    """Load approved context bundles for scoped agents."""
    rows: list[Mapping[str, Any]] = []
    for agent_did in scope.agent_dids_in_scope:
        result = await db.execute(
            text(
                """
                SELECT id, workflow_id, approved_fields, user_approved_at, created_at
                FROM workflow_context_bundles
                WHERE agent_did = :agent_did
                  AND status = 'approved'
                ORDER BY user_approved_at DESC NULLS LAST, created_at DESC
                """
            ),
            {"agent_did": agent_did},
        )
        rows.extend(result.mappings().all())
    return rows


async def _fetch_session_assertions(
    db: AsyncSession,
    scope: ExportScope,
) -> tuple[list[Mapping[str, Any]], str | None]:
    """Load Layer 2 session assertions if available."""
    rows: list[Mapping[str, Any]] = []
    try:
        for agent_did in scope.agent_dids_in_scope:
            result = await db.execute(
                text(
                    """
                    SELECT
                        id,
                        agent_did,
                        service_id,
                        ontology_tag,
                        issued_at,
                        expires_at,
                        authorization_ref,
                        was_used,
                        used_at
                    FROM session_assertions
                    WHERE agent_did = :agent_did
                    ORDER BY issued_at DESC
                    """
                ),
                {"agent_did": agent_did},
            )
            rows.extend(result.mappings().all())
    except SQLAlchemyError:
        return [], "Layer 2 session assertion records not available in this environment"
    return rows, None


async def _fetch_claims(
    db: AsyncSession,
    scope: ExportScope,
) -> list[Mapping[str, Any]]:
    """Load claims for executions in scope."""
    rows: list[Mapping[str, Any]] = []
    for execution_id in _execution_ids(scope):
        result = await db.execute(
            text(
                """
                SELECT id, execution_id, claim_type, status, filed_at, determined_at, resolved_at
                FROM liability_claims
                WHERE execution_id = :execution_id
                ORDER BY filed_at ASC
                """
            ),
            {"execution_id": execution_id},
        )
        rows.extend(result.mappings().all())
    return rows


async def _fetch_liability_evidence(
    db: AsyncSession,
    claims: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Load evidence for scoped claims."""
    rows: list[Mapping[str, Any]] = []
    for claim in claims:
        result = await db.execute(
            text(
                """
                SELECT id, claim_id, evidence_type, source_layer, summary, raw_data, gathered_at
                FROM liability_evidence
                WHERE claim_id = :claim_id
                ORDER BY gathered_at ASC
                """
            ),
            {"claim_id": claim["id"]},
        )
        rows.extend(result.mappings().all())
    return rows


async def _fetch_current_manifests(
    db: AsyncSession,
    service_ids: set[UUID],
) -> list[Mapping[str, Any]]:
    """Load current manifests for services involved in scope."""
    rows: list[Mapping[str, Any]] = []
    for service_id in service_ids:
        result = await db.execute(
            text(
                """
                SELECT id, service_id, manifest_hash, manifest_version, crawled_at
                FROM manifests
                WHERE service_id = :service_id
                  AND is_current = true
                LIMIT 1
                """
            ),
            {"service_id": service_id},
        )
        row = result.mappings().first()
        if row is not None:
            rows.append(row)
    return rows


def _service_ids_from_snapshots(snapshots: list[Mapping[str, Any]]) -> set[UUID]:
    """Return all service IDs found in snapshot step trust states."""
    service_ids: set[UUID] = set()
    for snapshot in snapshots:
        for step in _json_list(snapshot["step_trust_states"]):
            service_id = step.get("service_id")
            if service_id is not None:
                service_ids.add(service_id)
    return service_ids


async def generate_eu_ai_act_pdf(scope: ExportScope, db: AsyncSession) -> bytes:
    """Generate an EU AI Act compliance PDF."""
    snapshots = await _fetch_snapshots(db, scope)
    disclosures = await _fetch_context_disclosures(db, scope)
    bundles = await _fetch_context_bundles(db, scope)
    sessions, session_note = await _fetch_session_assertions(db, scope)
    claims = await _fetch_claims(db, scope)
    evidence = await _fetch_liability_evidence(db, claims)
    manifests = await _fetch_current_manifests(db, _service_ids_from_snapshots(snapshots))

    buffer, pdf, y = _new_pdf("AgentLedger EU AI Act Compliance Export", scope)
    if scope.low_risk_eu_scope:
        y = _draw_wrapped_line(
            pdf,
            "Scope note: no sensitivity_tier >= 3 ontology tags were found; this is a low-risk transparency export.",
            y,
            font="Helvetica-Bold",
        )

    y = _draw_heading(pdf, "SECTION 1 - System Identification", y)
    for execution in scope.executions:
        y = _draw_wrapped_line(
            pdf,
            (
                f"execution_id={execution['id']} | agent_did={execution['agent_did']} | "
                f"workflow_id={execution['workflow_id']} | workflow={execution['workflow_name']} | "
                f"reported_at={_iso(execution['reported_at'])} | outcome={execution['outcome']}"
            ),
            y,
        )
    for snapshot in snapshots:
        for step in _json_list(snapshot["step_trust_states"]):
            y = _draw_wrapped_line(
                pdf,
                (
                    f"service={step.get('service_name')} ({step.get('service_id')}) | "
                    f"trust_tier={step.get('trust_tier')} | trust_score={step.get('trust_score')}"
                ),
                y,
                indent=2,
            )

    y = _draw_heading(pdf, "SECTION 2 - Human Oversight Records", y)
    if not bundles:
        y = _draw_wrapped_line(pdf, "No approved context bundles found for this scope", y)
    for bundle in bundles:
        y = _draw_wrapped_line(
            pdf,
            (
                f"bundle_id={bundle['id']} | workflow_id={bundle['workflow_id']} | "
                f"approved_fields={_json_dict(bundle['approved_fields'])} | "
                f"approved_at={_iso(bundle['user_approved_at'])}"
            ),
            y,
        )
    if session_note:
        y = _draw_wrapped_line(pdf, session_note, y)
    elif not sessions:
        y = _draw_wrapped_line(pdf, "No high-sensitivity session assertions found", y)
    for session in sessions:
        y = _draw_wrapped_line(
            pdf,
            (
                f"session_assertion={session['id']} | ontology_tag={session['ontology_tag']} | "
                f"issued_at={_iso(session['issued_at'])} | expires_at={_iso(session['expires_at'])}"
            ),
            y,
        )

    y = _draw_heading(pdf, "SECTION 3 - Transparency Records", y)
    for execution in scope.executions:
        tags = [step["ontology_tag"] for step in scope.workflow_steps[execution["workflow_id"]]]
        y = _draw_wrapped_line(
            pdf,
            f"execution_id={execution['id']} authorized_ontology_tags={_csv(tags)}",
            y,
        )
    for disclosure in disclosures:
        y = _draw_wrapped_line(
            pdf,
            (
                f"disclosure_id={disclosure['id']} | ontology_tag={disclosure['ontology_tag']} | "
                f"fields_disclosed={_csv(disclosure['fields_disclosed'])} | "
                f"fields_withheld={_csv(disclosure['fields_withheld'])}"
            ),
            y,
        )

    y = _draw_heading(pdf, "SECTION 4 - Auditability Chain", y)
    events: list[tuple[datetime, str]] = []
    for manifest in manifests:
        events.append((manifest["crawled_at"], f"L1 manifest crawled: {manifest['id']}"))
    for disclosure in disclosures:
        events.append((disclosure["created_at"], f"L4 context disclosure: {disclosure['id']}"))
    for execution in scope.executions:
        events.append((execution["reported_at"], f"L5 execution reported: {execution['id']}"))
    for snapshot in snapshots:
        events.append((snapshot["captured_at"], f"L6 liability snapshot: {snapshot['id']}"))
    for _, event in sorted(events, key=lambda item: item[0]):
        y = _draw_wrapped_line(pdf, event, y)

    y = _draw_heading(pdf, "SECTION 5 - Incident Records", y)
    mismatch_evidence = [row for row in evidence if row["evidence_type"] == "context_mismatch"]
    threshold_failures = []
    for snapshot in snapshots:
        for step in _json_list(snapshot["step_trust_states"]):
            if step.get("trust_tier") is not None and step.get("trust_tier") < step.get("min_trust_tier", 0):
                threshold_failures.append(step)
    if not mismatch_evidence and not threshold_failures and not claims:
        y = _draw_wrapped_line(pdf, "No incidents recorded for this scope", y)
    for row in mismatch_evidence:
        y = _draw_wrapped_line(pdf, f"context_mismatch evidence: {row['summary']}", y)
    for step in threshold_failures:
        y = _draw_wrapped_line(
            pdf,
            (
                f"trust threshold failure: {step.get('ontology_tag')} "
                f"trust_tier={step.get('trust_tier')} min={step.get('min_trust_tier')}"
            ),
            y,
        )
    for claim in claims:
        y = _draw_wrapped_line(
            pdf,
            f"claim_id={claim['id']} | type={claim['claim_type']} | status={claim['status']}",
            y,
        )
    return _finish_pdf(buffer, pdf)


def _filter_tags(scope: ExportScope, prefix: str) -> set[str]:
    """Return tags in scope with a required prefix."""
    return {tag for tag in scope.ontology_tags_in_scope if tag.startswith(prefix)}


async def generate_hipaa_pdf(scope: ExportScope, db: AsyncSession) -> bytes:
    """Generate a HIPAA compliance PDF for health.* interactions."""
    health_tags = _filter_tags(scope, "health.")
    snapshots = await _fetch_snapshots(db, scope)
    disclosures = [
        row
        for row in await _fetch_context_disclosures(db, scope)
        if row["ontology_tag"] in health_tags
    ]
    claims = await _fetch_claims(db, scope)
    evidence = await _fetch_liability_evidence(db, claims)
    buffer, pdf, y = _new_pdf("AgentLedger HIPAA Compliance Export", scope)

    y = _draw_heading(pdf, "SECTION 1 - PHI Access Log", y)
    for execution in scope.executions:
        for step in scope.workflow_steps[execution["workflow_id"]]:
            if step["ontology_tag"] not in health_tags:
                continue
            y = _draw_wrapped_line(
                pdf,
                (
                    f"ontology_tag={step['ontology_tag']} | agent_did={execution['agent_did']} | "
                    f"timestamp={_iso(execution['reported_at'])}"
                ),
                y,
            )
    for snapshot in snapshots:
        for step in _json_list(snapshot["step_trust_states"]):
            if step.get("ontology_tag") in health_tags:
                y = _draw_wrapped_line(
                    pdf,
                    (
                        f"service={step.get('service_name')} | trust_tier={step.get('trust_tier')} | "
                        f"trust_score={step.get('trust_score')}"
                    ),
                    y,
                    indent=2,
                )

    y = _draw_heading(pdf, "SECTION 2 - Minimum Necessary Standard", y)
    if not disclosures:
        y = _draw_wrapped_line(pdf, "No health.* context disclosures found", y)
    for disclosure in disclosures:
        y = _draw_wrapped_line(
            pdf,
            (
                f"disclosure_id={disclosure['id']} | fields_disclosed={_csv(disclosure['fields_disclosed'])} | "
                f"fields_requested={_csv(disclosure['fields_requested'])} | "
                f"fields_withheld={_csv(disclosure['fields_withheld'])}"
            ),
            y,
        )

    y = _draw_heading(pdf, "SECTION 3 - Business Associate Evidence", y)
    for row in evidence:
        if row["evidence_type"] in {"service_capability", "trust_revocation"}:
            y = _draw_wrapped_line(pdf, f"{row['evidence_type']}: {row['summary']}", y)
    if not evidence:
        y = _draw_wrapped_line(pdf, "No Layer 3 or capability evidence attached", y)

    y = _draw_heading(pdf, "SECTION 4 - Breach Indicators", y)
    indicators = [
        row
        for row in evidence
        if row["evidence_type"] in {"context_mismatch", "trust_revocation", "revocation_event"}
    ]
    if not indicators:
        y = _draw_wrapped_line(pdf, "No breach indicators found for this scope", y)
    for row in indicators:
        y = _draw_wrapped_line(pdf, f"{row['evidence_type']}: {row['summary']}", y)
    return _finish_pdf(buffer, pdf)


async def generate_sec_pdf(scope: ExportScope, db: AsyncSession) -> bytes:
    """Generate an SEC compliance PDF for finance.investment.* interactions."""
    finance_tags = _filter_tags(scope, "finance.investment.")
    snapshots = await _fetch_snapshots(db, scope)
    disclosures = [
        row
        for row in await _fetch_context_disclosures(db, scope)
        if row["ontology_tag"] in finance_tags
    ]
    claims = await _fetch_claims(db, scope)
    evidence = await _fetch_liability_evidence(db, claims)
    sessions, session_note = await _fetch_session_assertions(db, scope)
    buffer, pdf, y = _new_pdf("AgentLedger SEC Compliance Export", scope)

    y = _draw_heading(pdf, "SECTION 1 - Trade Execution Record", y)
    for execution in scope.executions:
        for step in scope.workflow_steps[execution["workflow_id"]]:
            if step["ontology_tag"] not in finance_tags:
                continue
            y = _draw_wrapped_line(
                pdf,
                (
                    f"execution_id={execution['id']} | agent_did={execution['agent_did']} | "
                    f"service_id={step['service_id']} | ontology_tag={step['ontology_tag']} | "
                    f"reported_at={_iso(execution['reported_at'])} | outcome={execution['outcome']} | "
                    f"duration_ms={execution['duration_ms']}"
                ),
                y,
            )

    y = _draw_heading(pdf, "SECTION 2 - Authorization Chain", y)
    if session_note:
        y = _draw_wrapped_line(pdf, session_note, y)
    elif not sessions:
        y = _draw_wrapped_line(pdf, "Identity verified via Layer 2 credential", y)
    for session in sessions:
        y = _draw_wrapped_line(
            pdf,
            (
                f"session_assertion={session['id']} | issued_at={_iso(session['issued_at'])} | "
                f"expires_at={_iso(session['expires_at'])}"
            ),
            y,
        )
    for snapshot in snapshots:
        for step in _json_list(snapshot["step_trust_states"]):
            if step.get("ontology_tag") in finance_tags:
                y = _draw_wrapped_line(
                    pdf,
                    (
                        f"trust state: service={step.get('service_id')} | "
                        f"tier={step.get('trust_tier')} | score={step.get('trust_score')}"
                    ),
                    y,
                )
    for disclosure in disclosures:
        y = _draw_wrapped_line(
            pdf,
            f"context authorization: disclosure_id={disclosure['id']} | created_at={_iso(disclosure['created_at'])}",
            y,
        )

    y = _draw_heading(pdf, "SECTION 3 - Audit Trail", y)
    events: list[tuple[datetime, str]] = []
    for disclosure in disclosures:
        events.append((disclosure["created_at"], f"L4 disclosure {disclosure['id']}"))
    for execution in scope.executions:
        events.append((execution["reported_at"], f"L5 execution {execution['id']}"))
    for snapshot in snapshots:
        events.append((snapshot["captured_at"], f"L6 snapshot {snapshot['id']}"))
    for _, event in sorted(events, key=lambda item: item[0]):
        y = _draw_wrapped_line(pdf, event, y)

    y = _draw_heading(pdf, "SECTION 4 - Agent Identity Verification", y)
    for agent_did in sorted(scope.agent_dids_in_scope):
        did_method = agent_did.split(":", 2)[1] if ":" in agent_did else "unknown"
        y = _draw_wrapped_line(pdf, f"agent_did={agent_did} | did_method=did:{did_method}", y)
    for row in evidence:
        if row["source_layer"] == 2:
            y = _draw_wrapped_line(pdf, f"Layer 2 evidence: {row['summary']}", y)
    return _finish_pdf(buffer, pdf)


async def generate_full_pdf(scope: ExportScope, db: AsyncSession) -> bytes:
    """Generate a broad all-layer liability export."""
    # The EU AI Act renderer is the broadest all-layer view; use it as the full v0.1 body.
    return await generate_eu_ai_act_pdf(scope, db)


async def _log_compliance_export(
    db: AsyncSession,
    *,
    scope: ExportScope,
) -> None:
    """Write an audit log record for a generated compliance export."""
    await db.execute(
        text(
            """
            INSERT INTO compliance_exports (
                export_type,
                agent_did,
                service_id,
                execution_id,
                claim_id,
                from_date,
                to_date,
                record_count,
                generated_at,
                created_at
            )
            VALUES (
                :export_type,
                :agent_did,
                NULL,
                :execution_id,
                :claim_id,
                :from_date,
                :to_date,
                :record_count,
                NOW(),
                NOW()
            )
            """
        ),
        {
            "export_type": scope.export_type,
            "agent_did": scope.requested_agent_did,
            "execution_id": scope.requested_execution_id,
            "claim_id": scope.requested_claim_id,
            "from_date": scope.from_date,
            "to_date": scope.to_date,
            "record_count": len(scope.executions),
        },
    )


def compliance_export_filename(export_type: str, exported_at: datetime) -> str:
    """Build a stable liability compliance export filename."""
    date_part = exported_at.astimezone(timezone.utc).strftime("%Y%m%d")
    return f"agentledger_{export_type}_{date_part}.pdf"


async def generate_liability_compliance_export(
    *,
    db: AsyncSession,
    export_type: ComplianceExportType,
    agent_did: str | None = None,
    execution_id: UUID | None = None,
    claim_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> tuple[bytes, str]:
    """Resolve scope, generate PDF bytes, log the export, and return bytes plus filename."""
    try:
        scope = await resolve_export_scope(
            export_type=export_type,
            agent_did=agent_did,
            execution_id=execution_id,
            claim_id=claim_id,
            from_date=from_date,
            to_date=to_date,
            db=db,
        )
        if export_type == "eu_ai_act":
            pdf_bytes = await generate_eu_ai_act_pdf(scope, db)
        elif export_type == "hipaa":
            pdf_bytes = await generate_hipaa_pdf(scope, db)
        elif export_type == "sec":
            pdf_bytes = await generate_sec_pdf(scope, db)
        else:
            pdf_bytes = await generate_full_pdf(scope, db)

        await _log_compliance_export(db, scope=scope)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to generate liability compliance export: {exc.__class__.__name__}",
        ) from exc

    return pdf_bytes, compliance_export_filename(export_type, _utc_now())
