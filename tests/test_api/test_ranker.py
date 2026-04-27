"""Unit tests for api.services.ranker — pure scoring functions."""

from datetime import datetime, timedelta, timezone

from api.services.ranker import (
    compute_attestation_score,
    compute_cost_score,
    compute_latency_score,
    compute_rank_score,
    compute_reputation_score,
    compute_reliability_score,
    compute_trust_score,
    evaluate_trust_tier_4,
    normalize_trust_score,
)


class TestNormalizeTrustScore:
    def test_zero(self):
        assert normalize_trust_score(0.0) == 0.0

    def test_hundred(self):
        assert normalize_trust_score(100.0) == 1.0

    def test_fifty(self):
        assert normalize_trust_score(50.0) == 0.5

    def test_negative_clamped(self):
        assert normalize_trust_score(-10.0) == 0.0

    def test_over_hundred_clamped(self):
        assert normalize_trust_score(200.0) == 1.0


class TestComputeLatencyScore:
    def test_none_returns_default(self):
        assert compute_latency_score(None) == 0.5

    def test_zero_latency_perfect(self):
        assert compute_latency_score(0) == 1.0

    def test_high_latency_low_score(self):
        score = compute_latency_score(9000)
        assert 0.0 < score < 0.2

    def test_very_high_latency_clamped(self):
        assert compute_latency_score(20000) == 0.0


class TestComputeCostScore:
    def test_none_returns_default(self):
        assert compute_cost_score(None) == 0.5

    def test_free(self):
        assert compute_cost_score("free") == 1.0

    def test_per_transaction(self):
        assert compute_cost_score("per_transaction") == 0.5

    def test_unknown_model_returns_default(self):
        assert compute_cost_score("barter") == 0.5


class TestComputeReliabilityScore:
    def test_none_returns_default(self):
        assert compute_reliability_score(None) == 0.5

    def test_percentage(self):
        assert compute_reliability_score(95.0) == 0.95

    def test_fraction(self):
        assert compute_reliability_score(0.95) == 0.95

    def test_negative_clamped(self):
        assert compute_reliability_score(-1.0) == 0.0


class TestComputeAttestationScore:
    def test_active_identity_returns_one(self):
        assert compute_attestation_score(True) == 1.0

    def test_inactive_identity_returns_zero(self):
        assert compute_attestation_score(False) == 0.0

    def test_confirmed_attestations_take_precedence(self):
        now = datetime(2026, 4, 14, tzinfo=timezone.utc)
        score = compute_attestation_score(
            True,
            attestations=[
                {
                    "ontology_scope": "travel.*",
                    "recorded_at": now - timedelta(days=30),
                    "auditor_org_id": "auditfirm-one.example",
                },
                {
                    "ontology_scope": "travel.air.book",
                    "recorded_at": now - timedelta(days=60),
                    "auditor_org_id": "auditfirm-two.example",
                },
            ],
            now=now,
        )
        assert 0.7 <= score <= 1.0


class TestComputeReputationScore:
    def test_zero_total_returns_zero(self):
        assert compute_reputation_score(0, 0) == 0.0

    def test_success_over_total(self):
        assert compute_reputation_score(8, 2) == 0.8

    def test_blocklisted_forces_zero(self):
        assert compute_reputation_score(8, 2, federated_score=1.0, is_blocklisted=True) == 0.0


class TestEvaluateTrustTier4:
    def test_requires_two_independent_auditors(self):
        now = datetime(2026, 4, 14, tzinfo=timezone.utc)
        assert evaluate_trust_tier_4(
            [
                {
                    "ontology_scope": "travel.*",
                    "recorded_at": now,
                    "auditor_org_id": "auditfirm-one.example",
                    "is_expired": False,
                },
                {
                    "ontology_scope": "travel.air.book",
                    "recorded_at": now,
                    "auditor_org_id": "auditfirm-two.example",
                    "is_expired": False,
                },
            ],
            is_globally_revoked=False,
        ) is True

    def test_revoked_service_cannot_reach_tier_four(self):
        now = datetime(2026, 4, 14, tzinfo=timezone.utc)
        assert evaluate_trust_tier_4(
            [
                {
                    "ontology_scope": "travel.*",
                    "recorded_at": now,
                    "auditor_org_id": "auditfirm-one.example",
                    "is_expired": False,
                },
                {
                    "ontology_scope": "travel.air.book",
                    "recorded_at": now,
                    "auditor_org_id": "auditfirm-two.example",
                    "is_expired": False,
                },
            ],
            is_globally_revoked=True,
        ) is False


class TestComputeRankScore:
    def test_all_ones(self):
        score = compute_rank_score(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
        assert score == 1.0

    def test_all_zeros(self):
        score = compute_rank_score(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        assert score == 0.0

    def test_weighted_contribution(self):
        # Only capability_match=1.0, rest zero → should be 0.35
        score = compute_rank_score(1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        assert score == 0.35


class TestComputeTrustScore:
    def test_all_ones(self):
        score = compute_trust_score(1.0, 1.0, 1.0, 1.0)
        assert score == 100.0

    def test_all_zeros(self):
        score = compute_trust_score(0.0, 0.0, 0.0, 0.0)
        assert score == 0.0

    def test_partial(self):
        score = compute_trust_score(0.5, 0.5, 0.5, 0.5)
        assert score == 50.0
