"""Tests for nightly service identity revalidation."""

from __future__ import annotations

from unittest.mock import MagicMock

from crawler.tasks.revalidate_service_identity import _revalidate_service_identity_impl


def test_revalidate_service_identity_updates_verified_services(monkeypatch):
    """The revalidation task should refresh verified services and log success."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = lambda s, *a: None
    cursor.fetchall.return_value = [
        (
            "00000000-0000-0000-0000-000000000001",
            "payservice.com",
            {
                "manifest_version": "1.0",
                "service_id": "00000000-0000-0000-0000-000000000001",
                "name": "PayService",
                "domain": "payservice.com",
                "public_key": '{"kty":"OKP","crv":"Ed25519","x":"abc"}',
                "capabilities": [
                    {
                        "id": "send",
                        "ontology_tag": "commerce.payments.send",
                        "description": "Send payments with settlement context and receipt handling.",
                    }
                ],
                "pricing": {"model": "per_transaction", "tiers": [], "billing_method": "api_key"},
                "context": {"required": [], "optional": [], "data_retention_days": 30, "data_sharing": "none"},
                "operations": {"uptime_sla_percent": 99.9, "rate_limits": {"rpm": 100, "rpd": 1000}},
                "identity": {
                    "did": "did:web:payservice.com",
                    "verification_method": "did:web:payservice.com#key-1",
                },
                "signature": {"alg": "EdDSA", "value": "placeholder-signature-123456"},
                "last_updated": "2026-04-13T12:00:00Z",
            },
        )
    ]
    conn.cursor.return_value = cursor

    async def fake_validate_signed_manifest(manifest, force_refresh=False, redis=None):
        return True

    monkeypatch.setattr(
        "crawler.tasks.revalidate_service_identity.get_sync_connection",
        lambda: conn,
    )
    monkeypatch.setattr(
        "crawler.tasks.revalidate_service_identity.service_identity.validate_signed_manifest",
        fake_validate_signed_manifest,
    )

    result = _revalidate_service_identity_impl()

    assert result == {"checked": 1, "revalidated": 1, "failed": 0}
    conn.commit.assert_called_once()
