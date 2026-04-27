"""Layer 2 service identity helpers and activation flow."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.identity import (
    ServiceDidResolutionResponse,
    ServiceIdentityActivationResponse,
)
from api.models.manifest import ServiceManifest
from api.services.crypto import verify_json_signature
from api.services.ranker import (
    compute_attestation_score,
    compute_reputation_score,
    compute_trust_score,
)
from . import trust

SERVICE_DID_CACHE_TTL_SECONDS = 600


def service_did_from_domain(domain: str) -> str:
    """Derive the v0.1 did:web identifier for one service domain."""
    return f"did:web:{domain}"


def _did_document_cache_key(domain: str) -> str:
    """Build the Redis cache key for one did:web document."""
    return f"service-did:{domain}"


async def _cache_get(redis, key: str) -> str | None:
    """Best-effort cache read for did:web documents."""
    if redis is None:
        return None
    try:
        return await redis.get(key)
    except Exception:
        return None


async def _cache_set(redis, key: str, value: str, ttl: int = SERVICE_DID_CACHE_TTL_SECONDS) -> None:
    """Best-effort cache write for did:web documents."""
    if redis is None:
        return
    try:
        await redis.set(key, value, ex=ttl)
    except Exception:
        pass


def build_manifest_signing_payload(manifest: ServiceManifest) -> dict[str, Any]:
    """Build the canonical payload signed by a service manifest signature."""
    return manifest.model_dump(mode="json", exclude_none=True, exclude={"signature"})


def parse_manifest_public_key_jwk(manifest: ServiceManifest) -> dict[str, Any]:
    """Parse the manifest public key field as a JWK object."""
    if not manifest.public_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest public_key is required for signed service identity",
        )
    try:
        value = json.loads(manifest.public_key)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest public_key must be a valid JWK JSON string",
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest public_key must decode to a JWK object",
        )
    return value


def _extract_verification_method(
    did_document: dict[str, Any],
    verification_method_id: str,
) -> dict[str, Any]:
    """Return one verification method from a DID document by id."""
    methods = did_document.get("verificationMethod")
    if not isinstance(methods, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="did document is missing verificationMethod entries",
        )
    for method in methods:
        if isinstance(method, dict) and method.get("id") == verification_method_id:
            if not isinstance(method.get("publicKeyJwk"), dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="verification method is missing publicKeyJwk",
                )
            return method
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="verification method not found in did document",
    )


def _ensure_method_is_authorized(
    did_document: dict[str, Any],
    verification_method_id: str,
) -> None:
    """Require the verification method in authentication and assertionMethod."""
    authentication = did_document.get("authentication") or []
    assertion_method = did_document.get("assertionMethod") or []
    if verification_method_id not in authentication or verification_method_id not in assertion_method:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="verification method must appear in authentication and assertionMethod",
        )


async def _fetch_did_web_document(domain: str) -> dict[str, Any]:
    """Fetch the did:web document for one domain over HTTPS."""
    url = f"https://{domain}/.well-known/did.json"
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unable to resolve did:web document for {domain}",
        ) from exc

    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"did:web document not found for {domain}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="did:web document must be valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="did:web document must decode to an object",
        )
    return payload


async def resolve_service_did_document(
    domain: str,
    redis=None,
    force_refresh: bool = False,
) -> ServiceDidResolutionResponse:
    """Resolve and optionally cache one service did:web document."""
    key = _did_document_cache_key(domain)
    if not force_refresh:
        cached = await _cache_get(redis, key)
        if cached is not None:
            try:
                cached_payload = json.loads(cached)
            except json.JSONDecodeError:
                cached_payload = None
            if isinstance(cached_payload, dict):
                return ServiceDidResolutionResponse(
                    did=service_did_from_domain(domain),
                    did_document=cached_payload["did_document"],
                    cache_status="hit",
                    validated_at=datetime.fromisoformat(cached_payload["validated_at"]),
                )

    did_document = await _fetch_did_web_document(domain)
    expected_did = service_did_from_domain(domain)
    if did_document.get("id") != expected_did:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="did:web document id does not match the service domain",
        )

    validated_at = datetime.now(timezone.utc)
    await _cache_set(
        redis,
        key,
        json.dumps(
            {
                "did_document": did_document,
                "validated_at": validated_at.isoformat(),
            }
        ),
    )
    return ServiceDidResolutionResponse(
        did=expected_did,
        did_document=did_document,
        cache_status="miss",
        validated_at=validated_at,
    )


async def validate_signed_manifest(
    manifest: ServiceManifest,
    redis=None,
    force_refresh: bool = False,
) -> ServiceDidResolutionResponse:
    """Validate the identity and signature blocks of a signed manifest."""
    if manifest.identity is None or manifest.signature is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest identity and signature blocks are required",
        )

    expected_did = service_did_from_domain(manifest.domain)
    if manifest.identity.did != expected_did:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest identity.did must match the service did:web identifier",
        )

    resolution = await resolve_service_did_document(
        domain=manifest.domain,
        redis=redis,
        force_refresh=force_refresh,
    )
    method = _extract_verification_method(
        resolution.did_document,
        manifest.identity.verification_method,
    )
    _ensure_method_is_authorized(
        resolution.did_document,
        manifest.identity.verification_method,
    )

    manifest_public_jwk = parse_manifest_public_key_jwk(manifest)
    verification_jwk = method["publicKeyJwk"]
    if verification_jwk != manifest_public_jwk:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest public_key does not match the did:web verification method",
        )

    if not verify_json_signature(
        payload=build_manifest_signing_payload(manifest),
        signature=manifest.signature.value,
        public_jwk=verification_jwk,
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest signature verification failed",
        )

    return resolution


async def _compute_service_trust_components(
    db: AsyncSession,
    service_id: str,
    has_active_service_identity: bool,
) -> tuple[float, float, float, float]:
    """Compute the trust score inputs for one service."""
    verified_result = await db.execute(
        text(
            """
            SELECT
                COUNT(*) AS total_count,
                COUNT(*) FILTER (WHERE is_verified = true) AS verified_count
            FROM service_capabilities
            WHERE service_id = :service_id
            """
        ),
        {"service_id": service_id},
    )
    verified_row = verified_result.mappings().first()
    total_count = int(verified_row["total_count"] or 0)
    verified_count = int(verified_row["verified_count"] or 0)
    capability_probe_score = 0.0 if total_count == 0 else verified_count / total_count

    operations_result = await db.execute(
        text(
            """
            SELECT uptime_sla_percent
            FROM service_operations
            WHERE service_id = :service_id
            """
        ),
        {"service_id": service_id},
    )
    operations_row = operations_result.mappings().first()
    uptime = None if operations_row is None else operations_row["uptime_sla_percent"]
    operational_score = 0.5 if uptime is None else max(0.0, min(float(uptime) / 100.0, 1.0))

    reputation_result = await db.execute(
        text(
            """
            SELECT
                COUNT(*) FILTER (WHERE event_type = 'session_redeemed') AS success_count,
                COUNT(*) FILTER (WHERE event_type = 'session_redeem_rejected') AS failure_count
            FROM crawl_events
            WHERE service_id = :service_id
              AND created_at >= NOW() - INTERVAL '30 days'
            """
        ),
        {"service_id": service_id},
    )
    reputation_row = reputation_result.mappings().first()
    success_count = int(reputation_row["success_count"] or 0)
    failure_count = int(reputation_row["failure_count"] or 0)
    reputation_score = compute_reputation_score(success_count, failure_count)
    attestation_score = compute_attestation_score(has_active_service_identity)
    return capability_probe_score, attestation_score, operational_score, reputation_score


async def activate_service_identity(
    db: AsyncSession,
    domain: str,
    redis=None,
    force_refresh: bool = False,
) -> ServiceIdentityActivationResponse:
    """Validate and activate the current service did:web identity."""
    try:
        service_result = await db.execute(
            text(
                """
                SELECT id, trust_tier, public_key
                FROM services
                WHERE domain = :domain
                """
            ),
            {"domain": domain},
        )
        service_row = service_result.mappings().first()
        if not service_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="service not found",
            )
        if int(service_row["trust_tier"]) < 2:
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail="service must be domain-verified before identity activation",
            )

        manifest_result = await db.execute(
            text(
                """
                SELECT raw_json
                FROM manifests
                WHERE service_id = :service_id
                  AND is_current = true
                ORDER BY crawled_at DESC
                LIMIT 1
                """
            ),
            {"service_id": service_row["id"]},
        )
        manifest_row = manifest_result.mappings().first()
        if not manifest_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="current manifest not found",
            )

        manifest = ServiceManifest.model_validate(manifest_row["raw_json"])
        resolution = await validate_signed_manifest(
            manifest=manifest,
            redis=redis,
            force_refresh=force_refresh,
        )

        capability_probe_score, attestation_score, operational_score, reputation_score = (
            await _compute_service_trust_components(
                db=db,
                service_id=str(service_row["id"]),
                has_active_service_identity=True,
            )
        )

        verified_at = datetime.now(timezone.utc)
        await db.execute(
            text(
                """
                UPDATE services
                SET public_key = :public_key,
                    last_verified_at = :verified_at,
                    updated_at = NOW()
                WHERE id = :service_id
                """
            ),
            {
                "service_id": service_row["id"],
                "public_key": manifest.public_key,
                "verified_at": verified_at,
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                VALUES (
                    :service_id,
                    'service_identity_activated',
                    :domain,
                    CAST(:details AS JSONB),
                    NOW()
                )
                """
            ),
            {
                "service_id": service_row["id"],
                "domain": domain,
                "details": json.dumps(
                    {
                        "did": resolution.did,
                        "cache_status": resolution.cache_status,
                        "verification_method": manifest.identity.verification_method,
                    }
                ),
            },
        )
        trust_snapshot = await trust.recompute_service_trust(
            db=db,
            service_id=str(service_row["id"]),
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to activate service identity: {exc.__class__.__name__}",
        ) from exc

    return ServiceIdentityActivationResponse(
        domain=domain,
        did=resolution.did,
        identity_status="active",
        attestation_score=attestation_score,
        trust_score=float(trust_snapshot["trust_score"]),
        verified_at=verified_at,
    )
