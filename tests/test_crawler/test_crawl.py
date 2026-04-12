"""Tests for crawl helpers and task logic."""

from __future__ import annotations

from crawler.tasks.crawl import (
    build_manifest_url,
    compute_manifest_hash,
    should_mark_service_inactive,
    MAX_CONSECUTIVE_FAILURES,
    WELL_KNOWN_MANIFEST_PATH,
)


def test_build_manifest_url_uses_well_known_path():
    """Crawl helper should use the Layer 1 well-known path."""
    assert build_manifest_url("example.com") == "https://example.com/.well-known/agent-manifest.json"


def test_build_manifest_url_preserves_domain():
    """Manifest URL should use the exact domain provided."""
    url = build_manifest_url("api.skybridge.travel")
    assert url == f"https://api.skybridge.travel{WELL_KNOWN_MANIFEST_PATH}"


def test_compute_manifest_hash_is_stable():
    """Manifest hashing should be deterministic."""
    payload = {"name": "SkyBridge", "version": "1.0"}
    assert compute_manifest_hash(payload) == compute_manifest_hash(dict(reversed(payload.items())))


def test_compute_manifest_hash_changes_on_content_change():
    """Different payloads should produce different hashes."""
    hash_a = compute_manifest_hash({"name": "A"})
    hash_b = compute_manifest_hash({"name": "B"})
    assert hash_a != hash_b


def test_three_failures_marks_service_inactive():
    """Layer 1 deactivates services after three consecutive crawl failures."""
    assert should_mark_service_inactive(0) is False
    assert should_mark_service_inactive(1) is False
    assert should_mark_service_inactive(2) is False
    assert should_mark_service_inactive(3) is True
    assert should_mark_service_inactive(5) is True


def test_max_consecutive_failures_constant():
    """The threshold should match the Layer 1 spec (3 failures)."""
    assert MAX_CONSECUTIVE_FAILURES == 3
