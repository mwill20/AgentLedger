"""Tests for manifest registration endpoints."""

from __future__ import annotations

from uuid import UUID

from api.models.query import ManifestRegistrationResponse
from api.routers import manifests as manifests_router


def test_post_manifests_registers_service(client, api_key_headers, sample_manifest_payload, monkeypatch):
    """POST /v1/manifests should return the registration payload."""

    async def fake_register_manifest(db, manifest):
        return ManifestRegistrationResponse(
            service_id=manifest.service_id,
            trust_tier=1,
            trust_score=10.0,
            status="registered",
            capabilities_indexed=len(manifest.capabilities),
        )

    monkeypatch.setattr(manifests_router.registry, "register_manifest", fake_register_manifest)
    monkeypatch.setattr(
        manifests_router,
        "enqueue_domain_verification",
        lambda domain, service_id: True,
    )

    response = client.post("/v1/manifests", json=sample_manifest_payload, headers=api_key_headers)

    assert response.status_code == 201
    body = response.json()
    assert UUID(body["service_id"])
    assert body["trust_tier"] == 1
    assert body["status"] == "registered"
    assert body["capabilities_indexed"] == 1


def test_post_manifests_rejects_duplicate_ontology_tags(
    client, api_key_headers, sample_manifest_payload
):
    """Manifest validation should catch duplicate ontology tags before persistence."""
    sample_manifest_payload["capabilities"].append(
        {
            "id": "book-flight-duplicate",
            "ontology_tag": "travel.air.book",
            "description": "Duplicate capability entry that should fail manifest validation fast.",
        }
    )

    response = client.post("/v1/manifests", json=sample_manifest_payload, headers=api_key_headers)

    assert response.status_code == 422


def test_post_manifests_rejects_partial_identity_blocks(
    client, api_key_headers, sample_manifest_payload
):
    """Signed manifest fields must appear together when any service identity block is present."""
    sample_manifest_payload["identity"] = {
        "did": "did:web:skybridge.example",
        "verification_method": "did:web:skybridge.example#key-1",
    }

    response = client.post("/v1/manifests", json=sample_manifest_payload, headers=api_key_headers)

    assert response.status_code == 422
