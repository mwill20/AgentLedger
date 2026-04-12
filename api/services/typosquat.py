"""Typosquat detection for domain similarity scoring.

Flags manifest submissions where the domain is within Levenshtein
distance 2 of an existing registered domain. This catches common
typosquatting patterns:
  - Character substitution: flightbooker → f1ightbooker (l→1)
  - Character insertion:    paypal → paypall
  - Character deletion:     google → gogle
  - Adjacent transposition: amazon → amzaon
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Maximum edit distance to flag as a potential typosquat
TYPOSQUAT_MAX_DISTANCE = 2


def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings.

    Uses the standard dynamic programming approach with O(min(n,m))
    space via a single-row optimization.
    """
    if s1 == s2:
        return 0
    if not s1:
        return len(s2)
    if not s2:
        return len(s1)

    # Ensure s1 is the shorter string for space optimization
    if len(s1) > len(s2):
        s1, s2 = s2, s1

    previous_row = list(range(len(s1) + 1))

    for j, c2 in enumerate(s2):
        current_row = [j + 1]
        for i, c1 in enumerate(s1):
            # Cost is 0 if characters match, 1 if they differ
            cost = 0 if c1 == c2 else 1
            current_row.append(
                min(
                    current_row[i] + 1,       # insertion
                    previous_row[i + 1] + 1,  # deletion
                    previous_row[i] + cost,   # substitution
                )
            )
        previous_row = current_row

    return previous_row[-1]


def _extract_domain_base(domain: str) -> str:
    """Extract the registrable base from a domain for comparison.

    Strips common TLDs/suffixes so that 'flightbooker.com' and
    'f1ightbooker.com' are compared as 'flightbooker' vs 'f1ightbooker'.
    This prevents 'example.com' vs 'example.org' from being a false
    positive (distance 2 on the full string but different registrations).
    """
    # Remove the TLD (last dot-separated segment)
    parts = domain.lower().split(".")
    if len(parts) >= 2:
        return ".".join(parts[:-1])
    return domain.lower()


def find_similar_domains(
    candidate_domain: str,
    existing_domains: list[str],
    max_distance: int = TYPOSQUAT_MAX_DISTANCE,
) -> list[dict[str, str | int]]:
    """Find existing domains within edit distance of the candidate.

    Args:
        candidate_domain: The domain being registered.
        existing_domains: All currently registered domains.
        max_distance: Maximum Levenshtein distance to flag.

    Returns:
        List of dicts with 'domain' and 'distance' for each match.
    """
    candidate_base = _extract_domain_base(candidate_domain)
    matches: list[dict[str, str | int]] = []

    for existing in existing_domains:
        if existing.lower() == candidate_domain.lower():
            # Exact match — this is an update, not a typosquat
            continue

        existing_base = _extract_domain_base(existing)

        # Quick length check — if length difference exceeds max_distance,
        # Levenshtein distance must be at least that large
        if abs(len(candidate_base) - len(existing_base)) > max_distance:
            continue

        distance = levenshtein_distance(candidate_base, existing_base)
        if distance <= max_distance:
            matches.append({
                "domain": existing,
                "distance": distance,
            })

    return matches
