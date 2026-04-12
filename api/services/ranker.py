"""Ranking and trust scoring helpers."""

from __future__ import annotations

PRICING_MODEL_SCORES = {
    "free": 1.0,
    "freemium": 0.8,
    "subscription": 0.6,
    "per_transaction": 0.5,
}


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    """Clamp a value into a bounded range."""
    return max(lower, min(upper, value))


def normalize_trust_score(trust_score: float) -> float:
    """Normalize the 0-100 trust score range into 0-1."""
    return _clamp(trust_score / 100.0)


def compute_latency_score(avg_latency_ms: int | None) -> float:
    """Convert a raw latency number into a rank-friendly score."""
    if avg_latency_ms is None:
        return 0.5
    return _clamp(1.0 - (avg_latency_ms / 10000.0))


def compute_cost_score(pricing_model: str | None) -> float:
    """Approximate cost desirability from pricing model."""
    if pricing_model is None:
        return 0.5
    return PRICING_MODEL_SCORES.get(pricing_model, 0.5)


def compute_reliability_score(success_rate_30d: float | None) -> float:
    """Normalize success-rate inputs into 0-1."""
    if success_rate_30d is None:
        return 0.5
    if success_rate_30d > 1:
        return _clamp(success_rate_30d / 100.0)
    return _clamp(success_rate_30d)


def compute_rank_score(
    capability_match: float,
    trust_score: float,
    latency_score: float,
    cost_score: float,
    reliability_score: float,
    context_fit: float,
) -> float:
    """Ranking algorithm from the Layer 1 spec."""
    score = (
        capability_match * 0.35
        + trust_score * 0.25
        + latency_score * 0.15
        + cost_score * 0.10
        + reliability_score * 0.10
        + context_fit * 0.05
    )
    return round(_clamp(score), 6)


def compute_trust_score(
    capability_probe_score: float,
    attestation_score: float,
    operational_score: float,
    reputation_score: float,
) -> float:
    """Trust score computation from the Layer 1 spec."""
    raw = (
        capability_probe_score * 0.35
        + attestation_score * 0.30
        + operational_score * 0.20
        + reputation_score * 0.15
    )
    return round(_clamp(raw) * 100.0, 2)
