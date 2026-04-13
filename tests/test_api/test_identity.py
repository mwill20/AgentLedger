"""Tests for Layer 2 identity endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from api.models.identity import (
    AgentIdentityResponse,
    AgentRegistrationResponse,
    AgentRevokeResponse,
    CredentialVerificationResponse,
)
from api.routers import identity as identity_router


def test_get_issuer_did_document_returns_payload(client, monkeypatch):
    """GET /v1/identity/.well-known/did.json should expose the issuer DID document."""
    monkeypatch.setattr(
        identity_router.identity,
        "get_issuer_did_document",
        lambda: {
            "id": "did:web:agentledger.io",
            "verificationMethod": [],
            "authentication": [],
            "assertionMethod": [],
        },
    )

    response = client.get("/v1/identity/.well-known/did.json")

    assert response.status_code == 200
    assert response.json()["id"] == "did:web:agentledger.io"


def test_post_identity_agents_register_returns_credential(
    client,
    api_key_headers,
    monkeypatch,
):
    """POST /v1/identity/agents/register should return the registration payload."""
    payload = {
        "did": "did:key:z6MkhYTestDid",
        "did_document": {"id": "did:key:z6MkhYTestDid", "verificationMethod": []},
        "agent_name": "TripPlanner",
        "issuing_platform": "gpt",
        "capability_scope": ["travel.*"],
        "risk_tier": "standard",
        "proof": {
            "nonce": "nonce-12345678",
            "created_at": "2026-04-13T12:00:00Z",
            "signature": "signature-value-123456",
        },
    }

    async def fake_register_agent(db, request, redis=None):
        return AgentRegistrationResponse(
            did=request.did,
            credential_jwt="header.payload.signature",
            credential_expires_at=datetime(2027, 4, 13, tzinfo=timezone.utc),
            did_document={"id": request.did, "verificationMethod": []},
            issuer_did="did:web:agentledger.io",
        )

    monkeypatch.setattr(identity_router.identity, "register_agent", fake_register_agent)

    response = client.post(
        "/v1/identity/agents/register",
        json=payload,
        headers=api_key_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["did"] == payload["did"]
    assert body["issuer_did"] == "did:web:agentledger.io"


def test_post_identity_agents_verify_returns_status(client, monkeypatch):
    """POST /v1/identity/agents/verify should return online verification status."""

    async def fake_verify_agent_online(db, credential_jwt):
        return CredentialVerificationResponse(
            valid=True,
            did="did:key:z6MkhYTestDid",
            is_revoked=False,
            capability_scope=["travel.*"],
            risk_tier="standard",
        )

    monkeypatch.setattr(identity_router.identity, "verify_agent_online", fake_verify_agent_online)

    response = client.post(
        "/v1/identity/agents/verify",
        json={"credential_jwt": "header.payload.signature.with-enough-length"},
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_get_identity_agents_record_returns_agent(client, monkeypatch):
    """GET /v1/identity/agents/{did} should return the public agent record."""
    did_value = "did:key:z6MkhYTestDid"

    async def fake_get_agent_identity(db, did_value):
        return AgentIdentityResponse(
            did=did_value,
            did_document={"id": did_value, "verificationMethod": []},
            agent_name="TripPlanner",
            issuing_platform="gpt",
            capability_scope=["travel.*"],
            risk_tier="standard",
            is_active=True,
            is_revoked=False,
            registered_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(identity_router.identity, "get_agent_identity", fake_get_agent_identity)

    response = client.get(f"/v1/identity/agents/{did_value}")

    assert response.status_code == 200
    assert response.json()["did"] == did_value


def test_post_identity_agents_revoke_requires_admin_key(
    client,
    admin_api_key_headers,
    monkeypatch,
):
    """POST /v1/identity/agents/{did}/revoke should require admin auth and return the revocation payload."""
    did_value = "did:key:z6MkhYTestDid"

    async def fake_revoke_agent(db, did_value, request, revoked_by):
        return AgentRevokeResponse(
            did=did_value,
            revoked_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
            reason_code=request.reason_code,
        )

    monkeypatch.setattr(identity_router.identity, "revoke_agent", fake_revoke_agent)

    response = client.post(
        f"/v1/identity/agents/{did_value}/revoke",
        json={"reason_code": "key_compromised", "evidence": {"source": "unit-test"}},
        headers=admin_api_key_headers,
    )

    assert response.status_code == 200
    assert response.json()["reason_code"] == "key_compromised"
