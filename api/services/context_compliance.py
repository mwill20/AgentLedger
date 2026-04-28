"""Layer 4 compliance export PDF generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO
from textwrap import wrap
from typing import Any

from fastapi import HTTPException, status
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings


_PAGE_WIDTH, _PAGE_HEIGHT = LETTER
_LEFT_MARGIN = 54
_TOP_MARGIN = _PAGE_HEIGHT - 54
_BOTTOM_MARGIN = 54
_LINE_HEIGHT = 14
_WRAP_WIDTH = 96


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    """Render a timestamp or scalar for PDF output."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _csv(values: Any) -> str:
    """Render array-like field-name values without exposing field values."""
    if not values:
        return ""
    return ", ".join(str(value) for value in values)


def _safe_filename_agent(agent_did: str) -> str:
    """Keep the DID readable while avoiding path separators in the filename."""
    return agent_did.replace("/", "_").replace("\\", "_").replace('"', "")


def compliance_export_filename(agent_did: str, exported_at: datetime) -> str:
    """Build the compliance export attachment filename."""
    date_part = exported_at.astimezone(timezone.utc).strftime("%Y%m%d")
    return f"agentledger_compliance_{_safe_filename_agent(agent_did)}_{date_part}.pdf"


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
    lines = wrap(text_value, width=_WRAP_WIDTH - indent) or [""]
    for line in lines:
        if y < _BOTTOM_MARGIN:
            pdf.showPage()
            y = _TOP_MARGIN
            pdf.setFont(font, size)
        pdf.drawString(_LEFT_MARGIN + indent, y, line)
        y -= _LINE_HEIGHT
    return y


def _draw_heading(pdf: canvas.Canvas, title: str, y: float) -> float:
    """Draw a section heading."""
    if y < _BOTTOM_MARGIN + 36:
        pdf.showPage()
        y = _TOP_MARGIN
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(_LEFT_MARGIN, y, title)
    return y - (_LINE_HEIGHT * 1.5)


async def _fetch_profile(
    db: AsyncSession,
    agent_did: str,
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    """Load the active context profile and rules for an agent."""
    profile_result = await db.execute(
        text(
            """
            SELECT id, profile_name, default_policy
            FROM context_profiles
            WHERE agent_did = :agent_did
              AND is_active = true
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"agent_did": agent_did},
    )
    profile = profile_result.mappings().first()
    if profile is None:
        return None, []

    rules_result = await db.execute(
        text(
            """
            SELECT
                priority,
                scope_type,
                scope_value,
                permitted_fields,
                denied_fields,
                action
            FROM context_profile_rules
            WHERE profile_id = :profile_id
            ORDER BY priority ASC
            """
        ),
        {"profile_id": profile["id"]},
    )
    return profile, list(rules_result.mappings().all())


async def _fetch_disclosures(
    db: AsyncSession,
    agent_did: str,
) -> list[Mapping[str, Any]]:
    """Load all disclosure records for an agent."""
    result = await db.execute(
        text(
            """
            SELECT
                id,
                service_id,
                ontology_tag,
                fields_disclosed,
                fields_committed,
                fields_withheld,
                disclosure_method,
                erased,
                erased_at,
                created_at
            FROM context_disclosures
            WHERE agent_did = :agent_did
            ORDER BY created_at DESC
            """
        ),
        {"agent_did": agent_did},
    )
    return list(result.mappings().all())


async def _fetch_mismatches(
    db: AsyncSession,
    agent_did: str,
) -> list[Mapping[str, Any]]:
    """Load all mismatch events involving an agent."""
    result = await db.execute(
        text(
            """
            SELECT
                service_id,
                over_requested_fields,
                severity,
                resolved,
                created_at
            FROM context_mismatch_events
            WHERE agent_did = :agent_did
            ORDER BY created_at DESC
            """
        ),
        {"agent_did": agent_did},
    )
    return list(result.mappings().all())


def _draw_cover_page(
    pdf: canvas.Canvas,
    agent_did: str,
    exported_at: datetime,
) -> None:
    """Draw the required cover page."""
    y = _TOP_MARGIN
    y = _draw_wrapped_line(
        pdf,
        "AgentLedger Context Compliance Export",
        y,
        font="Helvetica-Bold",
        size=18,
    )
    y -= _LINE_HEIGHT
    y = _draw_wrapped_line(pdf, f"Agent DID: {agent_did}", y, size=11)
    y = _draw_wrapped_line(pdf, f"Export timestamp (UTC): {_iso(exported_at)}", y, size=11)
    y = _draw_wrapped_line(pdf, f"AgentLedger version: {settings.api_version}", y, size=11)
    pdf.showPage()


def _draw_profile_section(
    pdf: canvas.Canvas,
    profile: Mapping[str, Any] | None,
    rules: list[Mapping[str, Any]],
    y: float,
) -> float:
    """Draw Section 1: Context Profile."""
    y = _draw_heading(pdf, "SECTION 1 - Context Profile", y)
    if profile is None:
        return _draw_wrapped_line(pdf, "No active context profile found", y)

    y = _draw_wrapped_line(pdf, f"Active profile name: {profile['profile_name']}", y)
    y = _draw_wrapped_line(pdf, f"default_policy: {profile['default_policy']}", y)
    if not rules:
        return _draw_wrapped_line(pdf, "No profile rules found", y)

    y = _draw_wrapped_line(
        pdf,
        "priority | scope_type | scope_value | permitted_fields | denied_fields | action",
        y,
        font="Helvetica-Bold",
    )
    for rule in rules:
        y = _draw_wrapped_line(
            pdf,
            (
                f"{rule['priority']} | {rule['scope_type']} | {rule['scope_value']} | "
                f"{_csv(rule['permitted_fields'])} | {_csv(rule['denied_fields'])} | "
                f"{rule['action']}"
            ),
            y,
        )
    return y - _LINE_HEIGHT


def _draw_disclosure_section(
    pdf: canvas.Canvas,
    disclosures: list[Mapping[str, Any]],
    y: float,
) -> float:
    """Draw Section 2: Disclosure History."""
    y = _draw_heading(pdf, "SECTION 2 - Disclosure History", y)
    if not disclosures:
        return _draw_wrapped_line(pdf, "No disclosure records found", y)

    y = _draw_wrapped_line(
        pdf,
        (
            "disclosure_id | service_id | ontology_tag | fields_disclosed | "
            "fields_committed | fields_withheld | disclosure_method | disclosed_at | "
            "erased"
        ),
        y,
        font="Helvetica-Bold",
    )
    for row in disclosures:
        if row["erased"]:
            line = (
                f"{row['id']} | {row['service_id']} | {row['ontology_tag']} | "
                "[ERASED] | [ERASED] | [ERASED] | "
                f"{row['disclosure_method']} | {_iso(row['created_at'])} | true"
            )
        else:
            line = (
                f"{row['id']} | {row['service_id']} | {row['ontology_tag']} | "
                f"{_csv(row['fields_disclosed'])} | {_csv(row['fields_committed'])} | "
                f"{_csv(row['fields_withheld'])} | {row['disclosure_method']} | "
                f"{_iso(row['created_at'])} | false"
            )
        y = _draw_wrapped_line(pdf, line, y)
    return y - _LINE_HEIGHT


def _draw_mismatch_section(
    pdf: canvas.Canvas,
    mismatches: list[Mapping[str, Any]],
    y: float,
) -> float:
    """Draw Section 3: Mismatch Events."""
    y = _draw_heading(pdf, "SECTION 3 - Mismatch Events", y)
    if not mismatches:
        return _draw_wrapped_line(pdf, "No mismatch events found", y)

    y = _draw_wrapped_line(
        pdf,
        "service_id | over_requested_fields | severity | resolved | created_at",
        y,
        font="Helvetica-Bold",
    )
    for row in mismatches:
        y = _draw_wrapped_line(
            pdf,
            (
                f"{row['service_id']} | {_csv(row['over_requested_fields'])} | "
                f"{row['severity']} | {row['resolved']} | {_iso(row['created_at'])}"
            ),
            y,
        )
    return y - _LINE_HEIGHT


def _draw_erasure_section(
    pdf: canvas.Canvas,
    disclosures: list[Mapping[str, Any]],
    y: float,
) -> float:
    """Draw Section 4: Erasure Records."""
    y = _draw_heading(pdf, "SECTION 4 - Erasure Records", y)
    erased = [row for row in disclosures if row["erased"]]
    if not erased:
        return _draw_wrapped_line(pdf, "No erasure records found", y)

    y = _draw_wrapped_line(
        pdf,
        "disclosure_id | erased_at",
        y,
        font="Helvetica-Bold",
    )
    for row in erased:
        y = _draw_wrapped_line(pdf, f"{row['id']} | {_iso(row['erased_at'])}", y)
    return y


async def generate_compliance_pdf(
    db: AsyncSession,
    agent_did: str,
) -> bytes:
    """Generate a complete in-memory PDF compliance export for one agent DID."""
    profile, rules = await _fetch_profile(db, agent_did)
    disclosures = await _fetch_disclosures(db, agent_did)
    mismatches = await _fetch_mismatches(db, agent_did)

    if profile is None and not disclosures and not mismatches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no compliance records found for agent_did",
        )

    exported_at = _utc_now()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=LETTER)

    _draw_cover_page(pdf, agent_did, exported_at)
    y = _TOP_MARGIN
    y = _draw_profile_section(pdf, profile, rules, y)
    y = _draw_disclosure_section(pdf, disclosures, y)
    y = _draw_mismatch_section(pdf, mismatches, y)
    _draw_erasure_section(pdf, disclosures, y)
    pdf.save()

    return buffer.getvalue()
