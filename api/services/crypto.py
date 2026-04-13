"""Layer 2 cryptographic helpers.

This module is dependency-tolerant so the Layer 1 app can still import when
Layer 2 packages are not yet installed.
"""

from __future__ import annotations

import base64
import json
from typing import Any

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover - optional until Layer 2 deps are installed
    serialization = None
    Ed25519PrivateKey = None
    Ed25519PublicKey = None


def ensure_crypto_available() -> None:
    """Raise a clear runtime error when Layer 2 crypto deps are missing."""
    if serialization is None or Ed25519PrivateKey is None or Ed25519PublicKey is None:
        raise RuntimeError(
            "Layer 2 crypto dependencies are unavailable; install cryptography"
        )


def b64url_encode(data: bytes) -> str:
    """Encode bytes with URL-safe base64 and no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    """Decode URL-safe base64 with optional stripped padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a JSON object deterministically for signing."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def public_jwk_from_ed25519_public_key(public_key: Any) -> dict[str, str]:
    """Convert an Ed25519 public key into an OKP JWK."""
    ensure_crypto_available()
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url_encode(raw),
    }


def public_jwk_from_private_jwk(private_jwk: dict[str, str]) -> dict[str, str]:
    """Derive the public OKP JWK from a private OKP JWK."""
    private_key = load_private_key_from_jwk(private_jwk)
    return public_jwk_from_ed25519_public_key(private_key.public_key())


def load_public_key_from_jwk(jwk: dict[str, Any]):
    """Load an Ed25519 public key from an OKP JWK."""
    ensure_crypto_available()
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "x" not in jwk:
        raise ValueError("expected Ed25519 OKP public JWK")
    raw = b64url_decode(str(jwk["x"]))
    return Ed25519PublicKey.from_public_bytes(raw)


def load_private_key_from_jwk(jwk: dict[str, Any]):
    """Load an Ed25519 private key from an OKP JWK."""
    ensure_crypto_available()
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "d" not in jwk:
        raise ValueError("expected Ed25519 OKP private JWK")
    raw = b64url_decode(str(jwk["d"]))
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign_json(payload: dict[str, Any], private_jwk: dict[str, Any]) -> str:
    """Sign a canonical JSON payload and return a base64url signature."""
    private_key = load_private_key_from_jwk(private_jwk)
    signature = private_key.sign(canonical_json_bytes(payload))
    return b64url_encode(signature)


def verify_json_signature(
    payload: dict[str, Any],
    signature: str,
    public_jwk: dict[str, Any],
) -> bool:
    """Verify a canonical JSON payload signature."""
    public_key = load_public_key_from_jwk(public_jwk)
    try:
        public_key.verify(b64url_decode(signature), canonical_json_bytes(payload))
    except Exception:
        return False
    return True
