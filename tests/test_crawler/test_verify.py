"""Tests for domain verification helpers."""

from __future__ import annotations

from uuid import uuid4

from api.services.verifier import expected_dns_txt_token
from crawler.tasks.probe_capability import calculate_probe_score, promotes_to_trust_tier_three
from crawler.tasks.verify_domain import evaluate_domain_verification


def test_expected_dns_txt_token_matches_spec():
    """TXT records should use the Layer 1 verification prefix."""
    service_id = uuid4()
    token = expected_dns_txt_token(service_id)
    assert token == f"agentledger-verify={service_id}"


def test_evaluate_domain_verification_accepts_matching_record():
    """Domain verification should pass when the TXT record matches."""
    service_id = uuid4()
    assert evaluate_domain_verification(service_id, [expected_dns_txt_token(service_id)]) is True


def test_probe_helpers_require_all_capabilities_to_verify():
    """Tier 3 promotion should require all capability probes to pass."""
    results = [True, True, False]
    assert calculate_probe_score(results) == 2 / 3
    assert promotes_to_trust_tier_three(results) is False
