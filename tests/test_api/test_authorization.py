"""Tests for Phase 5 authorization queue services."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from api.services import authorization


class _FakeMappings:
    """Minimal SQLAlchemy mappings wrapper for service tests."""

    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row

    def all(self):
        if self._row is None:
            return []
        if isinstance(self._row, list):
            return self._row
        return [self._row]


class _FakeResult:
    """Minimal SQLAlchemy result wrapper for service tests."""

    def __init__(self, row=None, scalar_value=None):
        self._row = row
        self._scalar_value = scalar_value

    def mappings(self):
        return _FakeMappings(self._row)

    def scalar_one(self):
        return self._scalar_value


class _FakeSession:
    """Async DB session double with queued execute results."""

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)
        self.execute_calls: list[tuple[tuple, dict]] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        if self._results:
            return self._results.pop(0)
        return _FakeResult()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def test_list_pending_authorizations_returns_queue():
    """Pending queue reads should return unexpired requests with service metadata."""
    db = _FakeSession(
        [
            _FakeResult(),
            _FakeResult(
                row=[
                    {
                        "id": "00000000-0000-0000-0000-000000000111",
                        "agent_did": "did:key:z6MkPendingAgent",
                        "service_domain": "records.example",
                        "ontology_tag": "health.records.retrieve",
                        "sensitivity_tier": 3,
                        "request_context": {"record_type": "summary"},
                        "status": "pending",
                        "approver_id": None,
                        "decided_at": None,
                        "expires_at": datetime(2026, 4, 13, 12, 5, tzinfo=timezone.utc),
                        "created_at": datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                    }
                ]
            ),
        ]
    )

    result = asyncio.run(authorization.list_pending_authorizations(db=db))

    assert result.total == 1
    assert result.results[0].service_did == "did:web:records.example"
    assert db.committed is True


def test_approve_authorization_request_issues_linked_session(monkeypatch):
    """Approving a pending request should issue a linked approved session assertion."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    approved_expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    db = _FakeSession(
        [
            _FakeResult(
                row={
                    "id": "00000000-0000-0000-0000-000000000222",
                    "agent_did": "did:key:z6MkApprovedAgent",
                    "service_id": "00000000-0000-0000-0000-000000000333",
                    "ontology_tag": "health.records.retrieve",
                    "status": "pending",
                    "expires_at": expires_at,
                    "service_domain": "records.example",
                    "is_active": True,
                    "is_banned": False,
                    "last_verified_at": datetime.now(timezone.utc),
                    "agent_is_active": True,
                    "agent_is_revoked": False,
                }
            ),
            _FakeResult(scalar_value="00000000-0000-0000-0000-000000000444"),
            _FakeResult(),
            _FakeResult(),
        ]
    )
    webhook_calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        authorization.credentials,
        "issue_session_assertion",
        lambda **kwargs: (
            "header.payload.signature.with-enough-length",
            "assertion-jti-1",
            approved_expires_at,
        ),
    )

    async def fake_dispatch(event_type: str, payload: dict):
        webhook_calls.append((event_type, payload))

    monkeypatch.setattr(authorization, "dispatch_authorization_webhook", fake_dispatch)

    result = asyncio.run(
        authorization.approve_authorization_request(
            db=db,
            authorization_request_id="00000000-0000-0000-0000-000000000222",
            approver_id="test-admin-key",
        )
    )

    assert result.status == "approved"
    assert result.session_id == "00000000-0000-0000-0000-000000000444"
    assert result.service_did == "did:web:records.example"
    assert db.committed is True
    assert webhook_calls[0][0] == "authorization.approved"


def test_deny_authorization_request_marks_request_denied(monkeypatch):
    """Denying a pending request should return a denied decision result."""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db = _FakeSession(
        [
            _FakeResult(
                row={
                    "id": "00000000-0000-0000-0000-000000000555",
                    "agent_did": "did:key:z6MkDeniedAgent",
                    "service_id": "00000000-0000-0000-0000-000000000333",
                    "ontology_tag": "health.records.retrieve",
                    "status": "pending",
                    "expires_at": expires_at,
                    "service_domain": "records.example",
                }
            ),
            _FakeResult(),
            _FakeResult(),
        ]
    )
    webhook_calls: list[tuple[str, dict]] = []

    async def fake_dispatch(event_type: str, payload: dict):
        webhook_calls.append((event_type, payload))

    monkeypatch.setattr(authorization, "dispatch_authorization_webhook", fake_dispatch)

    result = asyncio.run(
        authorization.deny_authorization_request(
            db=db,
            authorization_request_id="00000000-0000-0000-0000-000000000555",
            approver_id="test-admin-key",
        )
    )

    assert result.status == "denied"
    assert result.session_id is None
    assert db.committed is True
    assert webhook_calls[0][0] == "authorization.denied"


def test_dispatch_authorization_webhook_posts_when_configured(monkeypatch):
    """Webhook dispatch should post an event envelope when configured."""
    calls: list[tuple[str, dict, dict]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json=None, headers=None):
            calls.append((url, json, headers))
            return _FakeResponse()

    monkeypatch.setattr(authorization.settings, "authorization_webhook_url", "https://hooks.example.com/agentledger")
    monkeypatch.setattr(authorization.settings, "authorization_webhook_secret", "topsecret")
    monkeypatch.setattr(authorization.settings, "authorization_webhook_timeout_seconds", 1.5)
    monkeypatch.setattr(authorization.httpx, "AsyncClient", lambda timeout: _FakeClient())

    asyncio.run(
        authorization.dispatch_authorization_webhook(
            "authorization.pending",
            {"authorization_request_id": "abc-123", "status": "pending"},
        )
    )

    assert calls[0][0] == "https://hooks.example.com/agentledger"
    assert calls[0][1]["event"] == "authorization.pending"
    assert calls[0][2]["X-AgentLedger-Event"] == "authorization.pending"
    assert "X-AgentLedger-Signature" in calls[0][2]
