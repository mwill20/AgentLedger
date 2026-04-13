"""Tests for Layer 2 expiry task helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from crawler.tasks.expire_identity_records import _expire_identity_records_impl


def test_expire_identity_records_updates_authorizations_and_sessions(monkeypatch):
    """The expiry task should expire pending approvals and prune expired sessions."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = lambda s, *a: None
    cursor.rowcount = 0
    conn.cursor.return_value = cursor

    rowcounts = iter([2, 5])

    def execute_side_effect(*args, **kwargs):
        cursor.rowcount = next(rowcounts)

    cursor.execute.side_effect = execute_side_effect

    monkeypatch.setattr(
        "crawler.tasks.expire_identity_records.get_sync_connection",
        lambda: conn,
    )

    result = _expire_identity_records_impl()

    assert result == {"expired_authorizations": 2, "pruned_sessions": 5}
    conn.commit.assert_called_once()
