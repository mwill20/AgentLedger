"""Tests for Layer 2 crypto and credential helpers."""

from __future__ import annotations

import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from api.config import settings
from api.services import credentials, did
from api.services.crypto import b64url_encode, public_jwk_from_private_jwk


def _generate_private_jwk() -> dict[str, str]:
    """Generate an Ed25519 private JWK for tests."""
    private_key = Ed25519PrivateKey.generate()
    raw_private = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "d": b64url_encode(raw_private),
        "x": b64url_encode(raw_public),
    }


def test_did_key_roundtrip_from_public_jwk():
    """A public JWK should roundtrip through did:key derivation."""
    private_jwk = _generate_private_jwk()
    public_jwk = public_jwk_from_private_jwk(private_jwk)

    did_value = did.did_key_from_public_jwk(public_jwk)
    roundtrip_jwk = did.public_jwk_from_did_key(did_value)

    assert roundtrip_jwk == public_jwk


def test_issue_and_verify_agent_credential_roundtrip(monkeypatch):
    """A JWT VC should verify successfully with the configured issuer key."""
    issuer_private_jwk = _generate_private_jwk()
    subject_private_jwk = _generate_private_jwk()
    subject_did = did.did_key_from_public_jwk(public_jwk_from_private_jwk(subject_private_jwk))

    monkeypatch.setattr(settings, "issuer_did", "did:web:agentledger.io")
    monkeypatch.setattr(settings, "issuer_private_jwk", json.dumps(issuer_private_jwk))
    monkeypatch.setattr(settings, "credential_ttl_seconds", 3600)

    token, _ = credentials.issue_agent_credential(
        subject_did=subject_did,
        agent_name="TripPlanner",
        issuing_platform="gpt",
        capability_scope=["travel.*"],
        risk_tier="standard",
    )
    claims = credentials.verify_agent_credential(token)

    assert claims["iss"] == "did:web:agentledger.io"
    assert claims["sub"] == subject_did
    assert claims["vc"]["credentialSubject"]["agent_name"] == "TripPlanner"


def test_issue_and_verify_session_assertion_roundtrip(monkeypatch):
    """A session assertion JWT should verify successfully with the configured issuer key."""
    issuer_private_jwk = _generate_private_jwk()

    monkeypatch.setattr(settings, "issuer_did", "did:web:agentledger.io")
    monkeypatch.setattr(settings, "issuer_private_jwk", json.dumps(issuer_private_jwk))
    monkeypatch.setattr(settings, "session_assertion_ttl_seconds", 300)

    token, _, _ = credentials.issue_session_assertion(
        subject_did="did:key:z6MkSessionAgent",
        service_did="did:web:payservice.com",
        service_id="00000000-0000-0000-0000-000000000123",
        ontology_tag="commerce.payments.send",
    )
    claims = credentials.verify_session_assertion(token)

    assert claims["sub"] == "did:key:z6MkSessionAgent"
    assert claims["aud"] == "did:web:payservice.com"
    assert claims["ontology_tag"] == "commerce.payments.send"
