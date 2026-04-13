"""DID helpers for Layer 2."""

from __future__ import annotations

from typing import Any

from api.services.crypto import b64url_decode, b64url_encode

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_MULTICODEC_PREFIX = bytes.fromhex("ed01")


def _base58_encode(data: bytes) -> str:
    """Encode bytes using the Bitcoin base58 alphabet."""
    if not data:
        return ""

    zero_count = len(data) - len(data.lstrip(b"\x00"))
    num = int.from_bytes(data, "big")
    encoded = ""
    while num:
        num, remainder = divmod(num, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    return ("1" * zero_count) + (encoded or "1")


def _base58_decode(data: str) -> bytes:
    """Decode a Bitcoin base58 string."""
    if not data:
        return b""

    num = 0
    for char in data:
        if char not in _BASE58_ALPHABET:
            raise ValueError("invalid base58 character")
        num = (num * 58) + _BASE58_ALPHABET.index(char)

    decoded = b"" if num == 0 else num.to_bytes((num.bit_length() + 7) // 8, "big")
    zero_count = len(data) - len(data.lstrip("1"))
    return (b"\x00" * zero_count) + decoded


def did_key_from_public_jwk(public_jwk: dict[str, Any]) -> str:
    """Derive a did:key identifier from an Ed25519 public JWK."""
    if public_jwk.get("kty") != "OKP" or public_jwk.get("crv") != "Ed25519" or "x" not in public_jwk:
        raise ValueError("expected Ed25519 OKP public JWK")
    fingerprint_bytes = _ED25519_MULTICODEC_PREFIX + b64url_decode(str(public_jwk["x"]))
    return f"did:key:z{_base58_encode(fingerprint_bytes)}"


def public_jwk_from_did_key(did: str) -> dict[str, str]:
    """Reconstruct a public Ed25519 JWK from a did:key identifier."""
    prefix = "did:key:z"
    if not did.startswith(prefix):
        raise ValueError("expected did:key identifier")
    payload = _base58_decode(did[len(prefix) :])
    if not payload.startswith(_ED25519_MULTICODEC_PREFIX):
        raise ValueError("unsupported did:key multicodec prefix")
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(payload[len(_ED25519_MULTICODEC_PREFIX) :]),
    }


def _verification_method_id(did: str) -> str:
    """Return the stable verification method fragment for a DID."""
    return f"{did}#{did.split(':')[-1]}"


def build_did_document(did: str, public_jwk: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal DID document backed by an Ed25519 public JWK."""
    method_id = _verification_method_id(did)
    return {
        "id": did,
        "verificationMethod": [
            {
                "id": method_id,
                "type": "JsonWebKey2020",
                "controller": did,
                "publicKeyJwk": public_jwk,
            }
        ],
        "authentication": [method_id],
        "assertionMethod": [method_id],
    }


def build_did_key_document(public_jwk: dict[str, Any]) -> dict[str, Any]:
    """Build a did:key DID document from a public JWK."""
    did = did_key_from_public_jwk(public_jwk)
    return build_did_document(did=did, public_jwk=public_jwk)


def build_issuer_did_document(issuer_did: str, issuer_public_jwk: dict[str, Any]) -> dict[str, Any]:
    """Build AgentLedger's issuer DID document."""
    return build_did_document(did=issuer_did, public_jwk=issuer_public_jwk)


def extract_public_jwk_from_did_document(
    did_document: dict[str, Any],
    expected_did: str | None = None,
) -> dict[str, Any]:
    """Extract the first public JWK verification method from a DID document."""
    if expected_did is not None and did_document.get("id") != expected_did:
        raise ValueError("DID document id does not match expected DID")

    verification_methods = did_document.get("verificationMethod")
    if not isinstance(verification_methods, list) or not verification_methods:
        raise ValueError("DID document must include verificationMethod entries")

    for method in verification_methods:
        if isinstance(method, dict) and isinstance(method.get("publicKeyJwk"), dict):
            public_jwk = method["publicKeyJwk"]
            if public_jwk.get("kty") == "OKP" and public_jwk.get("crv") == "Ed25519":
                return public_jwk

    raise ValueError("DID document does not contain an Ed25519 publicKeyJwk")
