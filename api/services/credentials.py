"""JWT credential issuance and verification helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

try:
    import jwt
except ImportError:  # pragma: no cover - optional until Layer 2 deps are installed
    jwt = None

from api.config import settings
from api.services.crypto import load_private_key_from_jwk, public_jwk_from_private_jwk
from api.services.did import build_issuer_did_document


def ensure_jwt_available() -> None:
    """Raise a clear runtime error when JWT deps are missing."""
    if jwt is None:
        raise RuntimeError("Layer 2 JWT dependency is unavailable; install PyJWT")


def load_issuer_private_jwk() -> dict[str, str]:
    """Parse the configured issuer private JWK."""
    if not settings.issuer_private_jwk.strip():
        raise RuntimeError("ISSUER_PRIVATE_JWK is not configured")
    try:
        value = json.loads(settings.issuer_private_jwk)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ISSUER_PRIVATE_JWK must be valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("ISSUER_PRIVATE_JWK must decode to a JSON object")
    return value


def load_issuer_public_jwk() -> dict[str, str]:
    """Derive the issuer public JWK from the configured private JWK."""
    return public_jwk_from_private_jwk(load_issuer_private_jwk())


def build_issuer_did_document_payload() -> dict:
    """Build AgentLedger's issuer DID document."""
    return build_issuer_did_document(settings.issuer_did, load_issuer_public_jwk())


def issue_agent_credential(
    subject_did: str,
    agent_name: str,
    issuing_platform: str | None,
    capability_scope: list[str],
    risk_tier: str,
) -> tuple[str, datetime]:
    """Issue a JWT VC for an agent identity."""
    ensure_jwt_available()
    private_jwk = load_issuer_private_jwk()
    private_key = load_private_key_from_jwk(private_jwk)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.credential_ttl_seconds)
    claims = {
        "iss": settings.issuer_did,
        "sub": subject_did,
        "jti": str(uuid4()),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "vc": {
            "type": ["VerifiableCredential", "AgentIdentityCredential"],
            "credentialSubject": {
                "id": subject_did,
                "agent_name": agent_name,
                "issuing_platform": issuing_platform,
                "capability_scope": capability_scope,
                "risk_tier": risk_tier,
            },
        },
    }
    token = jwt.encode(claims, private_key, algorithm="EdDSA")
    return token, expires_at


def verify_agent_credential(token: str) -> dict:
    """Verify a JWT VC and return its claims."""
    ensure_jwt_available()
    private_key = load_private_key_from_jwk(load_issuer_private_jwk())
    claims = jwt.decode(
        token,
        key=private_key.public_key(),
        algorithms=["EdDSA"],
        issuer=settings.issuer_did,
        options={"require": ["exp", "iat", "nbf", "iss", "sub"]},
    )
    subject = claims.get("vc", {}).get("credentialSubject", {}).get("id")
    if subject != claims.get("sub"):
        raise ValueError("credential subject id does not match sub")
    return claims
