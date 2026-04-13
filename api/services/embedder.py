"""Embedding generation for semantic search.

Uses sentence-transformers all-MiniLM-L6-v2 when available,
falling back to a deterministic hash-based embedder for tests.
"""

from __future__ import annotations

import logging
import math
import re
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSION = 384
MODEL_NAME = "all-MiniLM-L6-v2"

_model: SentenceTransformer | None = None
_model_load_attempted = False
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize_token(token: str) -> str:
    """Collapse simple inflections so hash mode keeps obvious token overlap."""
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _get_model() -> SentenceTransformer | None:
    """Lazy-load the sentence-transformers model (once).

    Skips model loading when ``EMBEDDING_MODE=hash`` to enable fast
    CPU-only deployments and load testing without GPU.
    """
    global _model, _model_load_attempted
    if _model_load_attempted:
        return _model
    _model_load_attempted = True

    from api.config import settings

    if settings.embedding_mode == "hash":
        logger.info("EMBEDDING_MODE=hash — using deterministic hash-based embedder")
        return _model  # remains None → falls through to _hash_embed

    try:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
        logger.info("Loaded embedding model: %s", MODEL_NAME)
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — using hash-based fallback embedder"
        )
    except Exception:
        logger.exception("Failed to load embedding model — using fallback")
    return _model


# ---------------------------------------------------------------------------
# Hash-based fallback (deterministic, no dependencies)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Normalize free text into lowercase tokens."""
    return [_normalize_token(token) for token in _TOKEN_RE.findall(text.lower())]


def _hash_embed(text: str, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Create a deterministic embedding without external dependencies."""
    vector = [0.0] * dimension
    tokens = _tokenize(text)
    if not tokens:
        return vector

    for token in tokens:
        # Python's built-in hash() is process-randomized, which breaks
        # cross-worker consistency. Use a stable hash for deterministic
        # embeddings in hash mode and tests.
        token_hash = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big")
        vector[token_hash % dimension] += 1.0

    magnitude = math.sqrt(sum(v * v for v in vector))
    if magnitude == 0:
        return vector

    return [v / magnitude for v in vector]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def embed_text(text: str, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Generate a 384-dim embedding for the given text.

    Uses all-MiniLM-L6-v2 when available, otherwise falls back
    to a deterministic hash-based embedder.
    """
    model = _get_model()
    if model is not None:
        embedding = model.encode(text, normalize_embeddings=True)
        return embedding.tolist()
    return _hash_embed(text, dimension)


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in a single call (batched for performance)."""
    model = _get_model()
    if model is not None:
        embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [e.tolist() for e in embeddings]
    return [_hash_embed(t) for t in texts]


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
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
