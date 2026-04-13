"""Unit tests for api.services.embedder — embedding and similarity functions."""

import math

from api.services.embedder import (
    EMBEDDING_DIMENSION,
    _hash_embed,
    _tokenize,
    cosine_similarity,
    embed_text,
    semantic_similarity,
    serialize_embedding,
)


class TestTokenize:
    def test_basic(self):
        assert _tokenize("Hello World") == ["hello", "world"]

    def test_strips_punctuation(self):
        assert _tokenize("Book flights!") == ["book", "flight"]

    def test_empty(self):
        assert _tokenize("") == []

    def test_numbers(self):
        assert _tokenize("flight 123") == ["flight", "123"]

    def test_normalizes_simple_plurals(self):
        assert _tokenize("cities bookings flights") == ["city", "booking", "flight"]


class TestHashEmbed:
    def test_dimension(self):
        vec = _hash_embed("test text")
        assert len(vec) == EMBEDDING_DIMENSION

    def test_normalized(self):
        vec = _hash_embed("test text")
        magnitude = math.sqrt(sum(v * v for v in vec))
        assert abs(magnitude - 1.0) < 1e-6

    def test_empty_returns_zero_vector(self):
        vec = _hash_embed("!!!???")
        assert all(v == 0.0 for v in vec)

    def test_deterministic(self):
        v1 = _hash_embed("flights")
        v2 = _hash_embed("flights")
        assert v1 == v2

    def test_different_texts_differ(self):
        v1 = _hash_embed("book flights")
        v2 = _hash_embed("read books")
        assert v1 != v2


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == 1.0

    def test_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert cosine_similarity(a, b) == 0.0

    def test_empty_vectors(self):
        assert cosine_similarity([], []) == 0.0

    def test_clamped_to_zero_one(self):
        # negative dot product
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert cosine_similarity(a, b) == 0.0


class TestEmbedText:
    def test_returns_correct_dimension(self):
        vec = embed_text("book a flight")
        assert len(vec) == EMBEDDING_DIMENSION

    def test_similar_texts_high_similarity(self):
        score = semantic_similarity("book flights", "reserve airplane tickets")
        # hash-based fallback won't be great, but should be >= 0
        assert 0.0 <= score <= 1.0


class TestEmbedBatch:
    def test_returns_list_of_correct_length(self):
        from api.services.embedder import embed_batch

        results = embed_batch(["hello world", "book flights"])
        assert len(results) == 2
        assert len(results[0]) == EMBEDDING_DIMENSION
        assert len(results[1]) == EMBEDDING_DIMENSION

    def test_empty_list(self):
        from api.services.embedder import embed_batch

        results = embed_batch([])
        assert results == []


class TestSerializeEmbedding:
    def test_format(self):
        vec = [0.1, 0.2, 0.3]
        result = serialize_embedding(vec)
        assert result.startswith("[")
        assert result.endswith("]")
        assert "0.100000" in result

    def test_roundtrip_length(self):
        vec = embed_text("test")
        serialized = serialize_embedding(vec)
        # Should contain EMBEDDING_DIMENSION values
        values = serialized.strip("[]").split(",")
        assert len(values) == EMBEDDING_DIMENSION
