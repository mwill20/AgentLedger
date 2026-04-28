"""Tests for Layer 4 context mismatch detection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.context import (
    ContextMismatchResolveRequest,
    ContextMismatchListResponse,
    ContextMismatchRecord,
    ContextMismatchResolveResponse,
)
from api.models.layer3 import RevocationCreateResponse
from api.routers import context as context_router
from api.services import context_mismatch


class _FakeMappings:
    """Minimal mappings wrapper for mismatch service tests."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    """Minimal SQLAlchemy result wrapper for mismatch service tests."""

    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


class _InspectableSession:
    """Async DB double that records executed SQL and supplied params."""

    def __init__(self, rows: list[list[dict]]) -> None:
        self._rows = list(rows)
        self.executed: list[tuple[str, dict]] = []
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        rows = self._rows.pop(0) if self._rows else []
        return _FakeResult(rows)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _mismatch_event(
    *,
    event_id=None,
    service_id=None,
    agent_did="did:key:z6MkContextHealthAgent",
    declared_fields=None,
    requested_fields=None,
    over_requested_fields=None,
    severity="critical",
    resolved=False,
    resolution_note=None,
):
    """Build one mismatch event row or response object."""
    return {
        "id": event_id or uuid4(),
        "service_id": service_id or uuid4(),
        "agent_did": agent_did,
        "declared_fields": declared_fields or ["user.name", "user.email"],
        "requested_fields": requested_fields or ["user.name", "user.ssn"],
        "over_requested_fields": over_requested_fields or ["user.ssn"],
        "severity": severity,
        "resolved": resolved,
        "resolution_note": resolution_note,
        "created_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
    }


def test_detect_mismatch_returns_warning_for_low_sensitivity_delta():
    """Low-sensitivity over-requested fields should be warning severity."""
    result = context_mismatch.detect_mismatch(
        requested_fields=["user.name", "user.nickname"],
        manifest_context=context_mismatch.ManifestContextBlock(
            required=["user.name"],
            optional=[],
        ),
    )

    assert result.detected is True
    assert result.over_requested_fields == ["user.nickname"]
    assert result.severity == "warning"


def test_detect_mismatch_returns_critical_for_sensitive_delta():
    """Sensitive over-requested fields should be critical severity."""
    result = context_mismatch.detect_mismatch(
        requested_fields=["user.name", "user.ssn"],
        manifest_context=context_mismatch.ManifestContextBlock(
            required=["user.name"],
            optional=[],
        ),
    )

    assert result.detected is True
    assert result.over_requested_fields == ["user.ssn"]
    assert result.severity == "critical"


def test_match_context_request_logs_mismatch_and_returns_400():
    """A match request with undeclared fields should log a mismatch and return 400."""
    service_id = uuid4()
    event_id = uuid4()
    db = _InspectableSession(
        rows=[
            [
                {"domain": "pharmacy.example", "field_name": "user.name"},
                {"domain": "pharmacy.example", "field_name": "user.email"},
            ],
            [
                _mismatch_event(
                    event_id=event_id,
                    service_id=service_id,
                    requested_fields=["user.name", "user.ssn"],
                    over_requested_fields=["user.ssn"],
                )
            ],
        ]
    )

    try:
        asyncio.run(
            context_mismatch.match_context_request(
                db=db,
                request=context_mismatch.ContextMatchRequest(
                    agent_did="did:key:z6MkContextHealthAgent",
                    service_id=service_id,
                    session_assertion="header.payload.signature.with-enough-length",
                    requested_fields=["user.name", "user.ssn"],
                ),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected mismatch HTTPException")

    assert response.status_code == 400
    assert response.detail["mismatch_detected"] is True
    assert response.detail["mismatch_id"] == str(event_id)
    assert response.detail["over_requested_fields"] == ["user.ssn"]
    assert db.commit_count == 1
    sql_text, params = db.executed[1]
    assert "INSERT INTO context_mismatch_events" in sql_text
    assert params["severity"] == "critical"


def test_list_mismatches_omits_null_guard_filters():
    """Mismatch list queries should avoid asyncpg-ambiguous NULL guards."""
    event = _mismatch_event()
    db = _InspectableSession(rows=[[event]])

    response = asyncio.run(context_mismatch.list_mismatches(db=db))

    sql_text, params = db.executed[0]
    assert response.total == 1
    assert " IS NULL OR " not in sql_text
    assert params == {"limit": 50, "offset": 0}


def test_post_context_match_returns_mismatch_detail(
    client,
    api_key_headers,
    monkeypatch,
):
    """POST /v1/context/match should surface mismatch detail from the service."""
    service_id = uuid4()
    event_id = uuid4()

    async def fake_match_context_request(db, request, redis=None):
        raise HTTPException(
            status_code=400,
            detail={
                "mismatch_detected": True,
                "mismatch_id": str(event_id),
                "service_id": str(service_id),
                "declared_fields": ["user.name", "user.email"],
                "requested_fields": ["user.name", "user.ssn"],
                "over_requested_fields": ["user.ssn"],
                "severity": "critical",
            },
        )

    monkeypatch.setattr(
        context_router.context_matcher,
        "match_context_request",
        fake_match_context_request,
    )

    response = client.post(
        "/v1/context/match",
        json={
            "agent_did": "did:key:z6MkContextHealthAgent",
            "service_id": str(service_id),
            "session_assertion": "header.payload.signature.with-enough-length",
            "requested_fields": ["user.name", "user.ssn"],
        },
        headers=api_key_headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["over_requested_fields"] == ["user.ssn"]
    assert response.json()["detail"]["severity"] == "critical"


def test_get_context_mismatches_returns_events(
    client,
    admin_api_key_headers,
    monkeypatch,
):
    """GET /v1/context/mismatches should return paginated events."""
    event = _mismatch_event()

    async def fake_list_mismatches(**kwargs):
        return ContextMismatchListResponse(
            total=1,
            limit=50,
            offset=0,
            events=[ContextMismatchRecord(**event)],
        )

    monkeypatch.setattr(
        context_router.context_mismatch,
        "list_mismatches",
        fake_list_mismatches,
    )

    response = client.get(
        "/v1/context/mismatches",
        headers=admin_api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["events"][0]["over_requested_fields"] == ["user.ssn"]


def test_post_context_mismatch_resolve_returns_resolution(
    client,
    admin_api_key_headers,
    monkeypatch,
):
    """POST /v1/context/mismatches/{id}/resolve should mark a mismatch resolved."""
    mismatch_id = uuid4()

    async def fake_resolve_mismatch(db, mismatch_id, request):
        return ContextMismatchResolveResponse(
            mismatch_id=mismatch_id,
            resolved=True,
            resolution_note=request.resolution_note,
            escalated_to_trust=request.escalate_to_trust,
        )

    monkeypatch.setattr(
        context_router.context_mismatch,
        "resolve_mismatch",
        fake_resolve_mismatch,
    )

    response = client.post(
        f"/v1/context/mismatches/{mismatch_id}/resolve",
        json={"resolution_note": "reviewed", "escalate_to_trust": False},
        headers=admin_api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["resolved"] is True
    assert response.json()["resolution_note"] == "reviewed"


def test_resolve_mismatch_can_escalate_to_trust(monkeypatch):
    """Resolving with escalation should invoke the Layer 3 revocation path."""
    mismatch_id = uuid4()
    service_id = uuid4()
    revocation_id = uuid4()
    event = _mismatch_event(
        event_id=mismatch_id,
        service_id=service_id,
        over_requested_fields=["user.ssn"],
    )
    event["service_domain"] = "pharmacy.example"
    db = _InspectableSession(
        rows=[
            [event],
            [{"did": "did:web:auditfirm.example"}],
            [
                {
                    "id": mismatch_id,
                    "resolved": True,
                    "resolution_note": (
                        "reviewed revocation_id="
                        f"{revocation_id} tx_hash=0xrevoked"
                    ),
                }
            ],
        ]
    )
    captured = {}

    async def fake_submit_revocation(db, request):
        captured["request"] = request
        return RevocationCreateResponse(
            revocation_id=revocation_id,
            tx_hash="0xrevoked",
            block_number=12,
        )

    monkeypatch.setattr(
        context_mismatch.attestation,
        "submit_revocation",
        fake_submit_revocation,
    )

    response = asyncio.run(
        context_mismatch.resolve_mismatch(
            db=db,
            mismatch_id=mismatch_id,
            request=ContextMismatchResolveRequest(
                resolution_note="reviewed",
                escalate_to_trust=True,
            ),
        )
    )

    assert response.escalated_to_trust is True
    assert response.revocation_id == revocation_id
    assert "revocation_id=" in response.resolution_note
    assert "tx_hash=0xrevoked" in response.resolution_note
    assert captured["request"].auditor_did == "did:web:auditfirm.example"
    assert captured["request"].service_domain == "pharmacy.example"
    assert captured["request"].reason_code == "context_mismatch"
