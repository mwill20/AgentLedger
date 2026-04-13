"""Tests for Layer 2 session endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from api.dependencies import require_bearer_credential
from api.main import app
from api.models.identity import AgentCredentialPrincipal, SessionRedeemResponse, SessionStatusResponse
from api.routers import identity as identity_router


def _principal() -> AgentCredentialPrincipal:
    """Return a minimal authenticated agent principal for route tests."""
    return AgentCredentialPrincipal(
        did="did:key:z6MkSessionAgent",
        capability_scope=["travel.*", "commerce.*"],
        risk_tier="standard",
        public_key_jwk={"kty": "OKP", "crv": "Ed25519", "x": "test"},
        credential_claims={"sub": "did:key:z6MkSessionAgent"},
    )


def test_post_identity_sessions_request_requires_bearer(client):
    """Session requests should reject callers without a bearer credential."""
    payload = {
        "service_domain": "payservice.com",
        "ontology_tag": "commerce.payments.send",
        "request_context": {"amount_bucket": "100-500"},
        "proof": {
            "nonce": "nonce-12345678",
            "created_at": "2026-04-13T12:00:00Z",
            "signature": "signature-value-123456",
        },
    }

    response = client.post("/v1/identity/sessions/request", json=payload)

    assert response.status_code == 401


def test_post_identity_sessions_request_issues_assertion(client, monkeypatch):
    """POST /v1/identity/sessions/request should return an issued assertion payload."""
    payload = {
        "service_domain": "payservice.com",
        "ontology_tag": "commerce.payments.send",
        "request_context": {"amount_bucket": "100-500"},
        "proof": {
            "nonce": "nonce-12345678",
            "created_at": "2026-04-13T12:00:00Z",
            "signature": "signature-value-123456",
        },
    }
    app.dependency_overrides[require_bearer_credential] = _principal

    async def fake_request_session(db, principal, request, redis=None):
        return SessionStatusResponse(
            status="issued",
            session_id=str(uuid4()),
            assertion_jwt="header.payload.signature.with-enough-length",
            service_did="did:web:payservice.com",
            expires_at=datetime(2026, 4, 13, 12, 5, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(identity_router.sessions, "request_session", fake_request_session)

    response = client.post("/v1/identity/sessions/request", json=payload)

    app.dependency_overrides.pop(require_bearer_credential, None)

    assert response.status_code == 200
    assert response.json()["status"] == "issued"


def test_post_identity_sessions_request_returns_pending(client, monkeypatch):
    """High-risk session requests should be surfaced as pending approval."""
    payload = {
        "service_domain": "records.example",
        "ontology_tag": "health.records.retrieve",
        "request_context": {"record_type": "summary"},
        "proof": {
            "nonce": "nonce-12345678",
            "created_at": "2026-04-13T12:00:00Z",
            "signature": "signature-value-123456",
        },
    }
    app.dependency_overrides[require_bearer_credential] = _principal

    async def fake_request_session(db, principal, request, redis=None):
        return SessionStatusResponse(
            status="pending_approval",
            authorization_request_id=str(uuid4()),
            expires_at=datetime(2026, 4, 13, 12, 5, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(identity_router.sessions, "request_session", fake_request_session)

    response = client.post("/v1/identity/sessions/request", json=payload)

    app.dependency_overrides.pop(require_bearer_credential, None)

    assert response.status_code == 202
    assert response.json()["status"] == "pending_approval"


def test_get_identity_sessions_status_returns_payload(client, monkeypatch):
    """GET /v1/identity/sessions/{id} should return the current flow status."""
    session_id = uuid4()
    app.dependency_overrides[require_bearer_credential] = _principal

    async def fake_get_session_status(db, principal, session_id):
        return SessionStatusResponse(
            status="issued",
            session_id=str(session_id),
            assertion_jwt="header.payload.signature.with-enough-length",
            service_did="did:web:payservice.com",
            expires_at=datetime(2026, 4, 13, 12, 5, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(identity_router.sessions, "get_session_status", fake_get_session_status)

    response = client.get(f"/v1/identity/sessions/{session_id}")

    app.dependency_overrides.pop(require_bearer_credential, None)

    assert response.status_code == 200
    assert response.json()["session_id"] == str(session_id)


def test_post_identity_sessions_redeem_accepts_assertion(client, monkeypatch):
    """POST /v1/identity/sessions/redeem should return an accepted payload."""

    async def fake_redeem_session(db, request):
        return SessionRedeemResponse(
            status="accepted",
            agent_did="did:key:z6MkSessionAgent",
            ontology_tag="commerce.payments.send",
        )

    monkeypatch.setattr(identity_router.sessions, "redeem_session", fake_redeem_session)

    response = client.post(
        "/v1/identity/sessions/redeem",
        json={
            "assertion_jwt": "header.payload.signature.with-enough-length",
            "service_domain": "payservice.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
