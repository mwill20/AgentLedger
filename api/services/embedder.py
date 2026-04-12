"""Embedding generation for semantic search."""

from __future__ import annotations

import math
import re

EMBEDDING_DIMENSION = 384
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Normalize free text into lowercase tokens."""
    return _TOKEN_RE.findall(text.lower())


def embed_text(text: str, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Create a deterministic local embedding without external dependencies."""
    vector = [0.0] * dimension
    tokens = tokenize(text)
    if not tokens:
        return vector

    for token in tokens:
        vector[hash(token) % dimension] += 1.0

    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector

    return [value / magnitude for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Compute cosine similarity for two vectors."""
    if not left or not right:
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=False))))


def semantic_similarity(query: str, candidate: str) -> float:
    """Compute semantic similarity between two free-text strings."""
    return cosine_similarity(embed_text(query), embed_text(candidate))


def serialize_embedding(vector: list[float]) -> str:
    """Serialize an embedding into pgvector's text format."""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
