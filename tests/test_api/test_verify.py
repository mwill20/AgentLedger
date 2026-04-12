"""Tests for the domain verification endpoint."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from uuid import uuid4


def test_verify_service_not_found(client, api_key_headers):
    """Verifying a non-existent service should return 404."""
    fake_id = uuid4()

    def mock_get_sync_connection():
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.__enter__ = lambda s: s
        cursor.__exit__ = lambda s, *a: None
        conn.cursor.return_value = cursor
        return conn

    with patch("api.routers.verify.get_sync_connection", mock_get_sync_connection):
        response = client.post(f"/v1/services/{fake_id}/verify", headers=api_key_headers)

    assert response.status_code == 404


def test_verify_requires_api_key(client):
    """Verification endpoint should require auth."""
    fake_id = uuid4()
    response = client.post(f"/v1/services/{fake_id}/verify")
    assert response.status_code in (401, 403)
