"""Unit tests for pure helper functions in crawler tasks."""

from unittest.mock import patch, MagicMock

from crawler.tasks.crawl import (
    build_manifest_url,
    compute_manifest_hash,
    should_mark_service_inactive,
    MAX_CONSECUTIVE_FAILURES,
    WELL_KNOWN_MANIFEST_PATH,
)
from crawler.tasks.verify_domain import (
    VERIFICATION_MAX_AGE_DAYS,
    _resolve_txt_records,
    evaluate_domain_verification,
)


# ---------------------------------------------------------------------------
# crawl.py — pure helpers
# ---------------------------------------------------------------------------

class TestBuildManifestUrl:
    def test_builds_correct_url(self):
        url = build_manifest_url("example.com")
        assert url == "https://example.com/.well-known/agent-manifest.json"

    def test_no_double_slash(self):
        url = build_manifest_url("example.com")
        assert "//" not in url.replace("https://", "")


class TestComputeManifestHash:
    def test_deterministic(self):
        payload = {"name": "test", "version": "1.0"}
        assert compute_manifest_hash(payload) == compute_manifest_hash(payload)

    def test_different_payloads(self):
        h1 = compute_manifest_hash({"name": "a"})
        h2 = compute_manifest_hash({"name": "b"})
        assert h1 != h2

    def test_key_order_irrelevant(self):
        h1 = compute_manifest_hash({"b": 2, "a": 1})
        h2 = compute_manifest_hash({"a": 1, "b": 2})
        assert h1 == h2


class TestShouldMarkServiceInactive:
    def test_below_threshold(self):
        assert should_mark_service_inactive(0) is False
        assert should_mark_service_inactive(1) is False
        assert should_mark_service_inactive(2) is False

    def test_at_threshold(self):
        assert should_mark_service_inactive(MAX_CONSECUTIVE_FAILURES) is True

    def test_above_threshold(self):
        assert should_mark_service_inactive(10) is True


# ---------------------------------------------------------------------------
# verify_domain.py — pure helpers
# ---------------------------------------------------------------------------

class TestEvaluateDomainVerification:
    def test_matching_token(self):
        service_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        expected = f"agentledger-verify={service_id}"
        assert evaluate_domain_verification(service_id, [expected]) is True

    def test_no_match(self):
        assert evaluate_domain_verification("some-id", ["unrelated record"]) is False

    def test_empty_records(self):
        assert evaluate_domain_verification("some-id", []) is False


class TestResolveTxtRecords:
    def test_dns_failure_returns_empty(self):
        """DNS failures should return empty list, not raise."""
        with patch("dns.resolver.resolve", side_effect=Exception("NXDOMAIN")):
            result = _resolve_txt_records("nonexistent.example.com")
        assert result == []

    def test_successful_resolution(self):
        """Successful DNS resolution should return decoded strings."""
        mock_rdata = MagicMock()
        mock_rdata.strings = [b"agentledger-verify=abc123"]
        mock_answers = [mock_rdata]

        with patch("dns.resolver.resolve", return_value=mock_answers):
            result = _resolve_txt_records("example.com")
        assert result == ["agentledger-verify=abc123"]


class TestConstants:
    def test_verification_max_age(self):
        assert VERIFICATION_MAX_AGE_DAYS == 30

    def test_well_known_path(self):
        assert WELL_KNOWN_MANIFEST_PATH == "/.well-known/agent-manifest.json"
