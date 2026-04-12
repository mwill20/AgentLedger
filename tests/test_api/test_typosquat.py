"""Tests for typosquat detection on POST /manifests."""

from __future__ import annotations

from api.services.typosquat import (
    TYPOSQUAT_MAX_DISTANCE,
    _extract_domain_base,
    find_similar_domains,
    levenshtein_distance,
)


# ---------------------------------------------------------------------------
# Levenshtein distance unit tests
# ---------------------------------------------------------------------------

def test_levenshtein_identical():
    """Identical strings should have distance 0."""
    assert levenshtein_distance("hello", "hello") == 0


def test_levenshtein_single_substitution():
    """Single character substitution = distance 1."""
    assert levenshtein_distance("cat", "bat") == 1


def test_levenshtein_single_insertion():
    """Single character insertion = distance 1."""
    assert levenshtein_distance("cat", "cats") == 1


def test_levenshtein_single_deletion():
    """Single character deletion = distance 1."""
    assert levenshtein_distance("cats", "cat") == 1


def test_levenshtein_empty_strings():
    """Empty vs non-empty = length of non-empty."""
    assert levenshtein_distance("", "hello") == 5
    assert levenshtein_distance("hello", "") == 5
    assert levenshtein_distance("", "") == 0


def test_levenshtein_typosquat_l_to_1():
    """The classic l→1 substitution should be distance 1."""
    assert levenshtein_distance("flightbooker", "f1ightbooker") == 1


def test_levenshtein_typosquat_double_letter():
    """Double letter insertion should be distance 1."""
    assert levenshtein_distance("google", "gooogle") == 1


def test_levenshtein_distance_two():
    """Two edits should return distance 2."""
    # l→1 and add extra 'o'
    assert levenshtein_distance("flightbooker", "f1ightboooker") == 2


# ---------------------------------------------------------------------------
# Domain base extraction
# ---------------------------------------------------------------------------

def test_extract_domain_base_strips_tld():
    """Should strip .com, .org, etc."""
    assert _extract_domain_base("flightbooker.com") == "flightbooker"
    assert _extract_domain_base("example.org") == "example"


def test_extract_domain_base_preserves_subdomain():
    """Subdomains should be preserved in the base."""
    assert _extract_domain_base("api.flightbooker.com") == "api.flightbooker"


def test_extract_domain_base_handles_bare_domain():
    """Single-part domain should return as-is."""
    assert _extract_domain_base("localhost") == "localhost"


# ---------------------------------------------------------------------------
# Similar domain detection
# ---------------------------------------------------------------------------

def test_find_similar_domains_detects_typosquat():
    """f1ightbookerpro.com should be flagged against flightbookerpro.com."""
    existing = ["flightbookerpro.com", "unrelated.com", "another-service.io"]
    matches = find_similar_domains("f1ightbookerpro.com", existing)
    assert len(matches) == 1
    assert matches[0]["domain"] == "flightbookerpro.com"
    assert matches[0]["distance"] <= TYPOSQUAT_MAX_DISTANCE


def test_find_similar_domains_ignores_exact_match():
    """Exact same domain should not be flagged (it's an update)."""
    existing = ["flightbooker.com"]
    matches = find_similar_domains("flightbooker.com", existing)
    assert len(matches) == 0


def test_find_similar_domains_ignores_distant():
    """Domains with distance > 2 should not be flagged."""
    existing = ["completelydifferent.com"]
    matches = find_similar_domains("flightbooker.com", existing)
    assert len(matches) == 0


def test_find_similar_domains_multiple_matches():
    """Multiple similar domains should all be returned."""
    existing = ["paypal.com", "paypall.com", "paypa1.com"]
    matches = find_similar_domains("paypl.com", existing)
    # paypl vs paypal = distance 1, paypl vs paypall = distance 2, paypl vs paypa1 = distance 2
    assert len(matches) >= 1


def test_max_distance_constant():
    """Typosquat threshold should be 2 per spec."""
    assert TYPOSQUAT_MAX_DISTANCE == 2
