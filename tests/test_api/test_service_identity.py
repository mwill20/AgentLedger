"""Tests for Layer 2 service identity helpers and routes."""

from __future__ import annotations

import asyncio
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from fastapi import HTTPException

from api.models.identity import ServiceDidResolutionResponse
from api.models.manifest import ServiceManifest
from api.services import service_identity
from api.services.crypto import b64url_encode, public_jwk_from_private_jwk, sign_json


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


def _signed_manifest(private_jwk: dict[str, str]) -> ServiceManifest:
    """Build a valid signed service manifest."""
    public_jwk = public_jwk_from_private_jwk(private_jwk)
    payload = {
        "manifest_version": "1.0",
        "service_id": "00000000-0000-0000-0000-000000000555",
        "name": "PayService",
        "domain": "payservice.com",
        "public_key": json.dumps(public_jwk),
        "capabilities": [
            {
                "id": "send-payment",
                "ontology_tag": "commerce.payments.send",
                "description": "Send payments with amount, beneficiary, and settlement context.",
            }
        ],
        "pricing": {"model": "per_transaction", "tiers": [], "billing_method": "api_key"},
        "context": {
            "required": [{"name": "amount", "type": "number"}],
            "optional": [],
            "data_retention_days": 30,
            "data_sharing": "none",
        },
        "operations": {
            "uptime_sla_percent": 99.9,
            "rate_limits": {"rpm": 100, "rpd": 10000},
            "sandbox_url": "https://sandbox.payservice.com",
        },
        "identity": {
            "did": "did:web:payservice.com",
            "verification_method": "did:web:payservice.com#key-1",
        },
        "signature": {
            "alg": "EdDSA",
            "value": "placeholder-signature-123456",
        },
        "last_updated": "2026-04-13T12:00:00Z",
    }
    unsigned_manifest = ServiceManifest.model_validate(payload)
    payload["signature"] = {
        "alg": "EdDSA",
        "value": sign_json(
            service_identity.build_manifest_signing_payload(unsigned_manifest),
            private_jwk,
        ),
    }
    return ServiceManifest.model_validate(payload)


class _FakeMappings:
    """Minimal SQLAlchemy mappings wrapper for unit tests."""

    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    """Minimal SQLAlchemy result wrapper for unit tests."""

    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _FakeMappings(self._row)


class _FakeSession:
    """Async session double with queued result rows."""

    def __init__(self, rows: list[dict | None]) -> None:
        self._rows = list(rows)
        self.execute_calls: list[tuple[tuple, dict]] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        row = self._rows.pop(0) if self._rows else None
        return _FakeResult(row)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def test_validate_signed_manifest_accepts_matching_did_document(monkeypatch):
    """A signed manifest should validate against a matching did:web document."""
    private_jwk = _generate_private_jwk()
    public_jwk = public_jwk_from_private_jwk(private_jwk)
    manifest = _signed_manifest(private_jwk)

    async def fake_fetch(domain: str) -> dict:
        return {
            "id": "did:web:payservice.com",
            "verificationMethod": [
                {
                    "id": "did:web:payservice.com#key-1",
                    "type": "JsonWebKey2020",
                    "controller": "did:web:payservice.com",
                    "publicKeyJwk": public_jwk,
                }
            ],
            "authentication": ["did:web:payservice.com#key-1"],
            "assertionMethod": ["did:web:payservice.com#key-1"],
        }

    monkeypatch.setattr(service_identity, "_fetch_did_web_document", fake_fetch)

    resolution = asyncio.run(
        service_identity.validate_signed_manifest(manifest=manifest, force_refresh=True)
    )

    assert resolution.did == "did:web:payservice.com"
    assert resolution.did_document["id"] == "did:web:payservice.com"


def test_validate_signed_manifest_rejects_bad_signature(monkeypatch):
    """A signed manifest should be rejected when the detached signature is wrong."""
    private_jwk = _generate_private_jwk()
    public_jwk = public_jwk_from_private_jwk(private_jwk)
    manifest = _signed_manifest(private_jwk)
    manifest.signature.value = "invalid-signature-value-123456"

    async def fake_fetch(domain: str) -> dict:
        return {
            "id": "did:web:payservice.com",
            "verificationMethod": [
                {
                    "id": "did:web:payservice.com#key-1",
                    "type": "JsonWebKey2020",
                    "controller": "did:web:payservice.com",
                    "publicKeyJwk": public_jwk,
                }
            ],
            "authentication": ["did:web:payservice.com#key-1"],
            "assertionMethod": ["did:web:payservice.com#key-1"],
        }

    monkeypatch.setattr(service_identity, "_fetch_did_web_document", fake_fetch)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            service_identity.validate_signed_manifest(manifest=manifest, force_refresh=True)
        )
    assert exc_info.value.status_code == 422


def test_activate_service_identity_updates_trust_score(monkeypatch):
    """Activation should mark the service verified and assign a positive attestation score."""
    private_jwk = _generate_private_jwk()
    manifest = _signed_manifest(private_jwk)
    db = _FakeSession(
        rows=[
            {
                "id": "00000000-0000-0000-0000-000000000555",
                "trust_tier": 2,
                "public_key": None,
            },
            {
                "raw_json": manifest.model_dump(mode="json"),
            },
            {
                "total_count": 1,
                "verified_count": 1,
            },
            {
                "uptime_sla_percent": 99.9,
            },
            {
                "success_count": 5,
                "failure_count": 1,
            },
            None,
            None,
        ]
    )

    async def fake_validate_signed_manifest(*, manifest, redis=None, force_refresh=False):
        return ServiceDidResolutionResponse(
            did="did:web:payservice.com",
            did_document={"id": "did:web:payservice.com", "verificationMethod": []},
            cache_status="miss",
            validated_at=manifest.last_updated,
        )

    monkeypatch.setattr(
        service_identity,
        "validate_signed_manifest",
        fake_validate_signed_manifest,
    )
    
    async def fake_recompute_service_trust(db, service_id):
        return {"trust_score": 62.5}

    monkeypatch.setattr(
        service_identity.trust,
        "recompute_service_trust",
        fake_recompute_service_trust,
    )

    result = asyncio.run(
        service_identity.activate_service_identity(
            db=db,
            domain="payservice.com",
            force_refresh=True,
        )
    )

    assert result.identity_status == "active"
    assert result.did == "did:web:payservice.com"
    assert result.attestation_score == 1.0
    assert result.trust_score > 0
    assert db.committed is True
    assert db.rolled_back is False
