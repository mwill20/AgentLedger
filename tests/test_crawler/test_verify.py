"""Tests for domain verification helpers and DNS logic."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from api.services.verifier import expected_dns_txt_token
from crawler.tasks.probe_capability import calculate_probe_score, promotes_to_trust_tier_three
from crawler.tasks.verify_domain import (
    evaluate_domain_verification,
    _resolve_txt_records,
    VERIFICATION_MAX_AGE_DAYS,
)


def test_expected_dns_txt_token_matches_spec():
    """TXT records should use the Layer 1 verification prefix."""
    service_id = uuid4()
    token = expected_dns_txt_token(service_id)
    assert token == f"agentledger-verify={service_id}"


def test_evaluate_domain_verification_accepts_matching_record():
    """Domain verification should pass when the TXT record matches."""
    service_id = uuid4()
    assert evaluate_domain_verification(service_id, [expected_dns_txt_token(service_id)]) is True


def test_evaluate_domain_verification_rejects_wrong_id():
    """Domain verification should fail when the service_id doesn't match."""
    service_id = uuid4()
    wrong_id = uuid4()
    assert evaluate_domain_verification(service_id, [expected_dns_txt_token(wrong_id)]) is False


def test_evaluate_domain_verification_handles_empty_records():
    """Domain verification should fail with no TXT records."""
    assert evaluate_domain_verification(uuid4(), []) is False


def test_evaluate_domain_verification_case_insensitive():
    """Domain verification should be case-insensitive."""
    service_id = uuid4()
    token = expected_dns_txt_token(service_id).upper()
    assert evaluate_domain_verification(service_id, [token]) is True


def test_probe_helpers_require_all_capabilities_to_verify():
    """Tier 3 promotion should require all capability probes to pass."""
    results = [True, True, False]
    assert calculate_probe_score(results) == 2 / 3
    assert promotes_to_trust_tier_three(results) is False


def test_probe_all_pass_promotes():
    """All probes passing should allow tier 3 promotion."""
    results = [True, True, True]
    assert calculate_probe_score(results) == 1.0
    assert promotes_to_trust_tier_three(results) is True


def test_probe_empty_does_not_promote():
    """Empty probe results should not promote."""
    assert calculate_probe_score([]) == 0.0
    assert promotes_to_trust_tier_three([]) is False


def test_resolve_txt_records_handles_missing_dns():
    """DNS resolution should return empty list on failure, not raise."""
    import dns.resolver

    with patch.object(dns.resolver, "resolve", side_effect=Exception("NXDOMAIN")):
        records = _resolve_txt_records("nonexistent.example.com")
    assert records == []


def test_verification_max_age():
    """Verification window should be 30 days per spec."""
    assert VERIFICATION_MAX_AGE_DAYS == 30
