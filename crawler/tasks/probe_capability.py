"""Vector C capability probing helpers."""

from __future__ import annotations


def calculate_probe_score(results: list[bool]) -> float:
    """Return the fraction of successful probe results."""
    if not results:
        return 0.0
    return sum(1 for result in results if result) / len(results)


def promotes_to_trust_tier_three(results: list[bool]) -> bool:
    """Layer 1 only promotes when every declared capability verifies."""
    return bool(results) and all(results)
