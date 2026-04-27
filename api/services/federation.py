"""Layer 3 federation and blocklist helpers."""

from __future__ import annotations

import json
from datetime import datetime
from typing import AsyncIterator

import httpx
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.layer3 import (
    FederationBlocklistEntry,
    FederationBlocklistResponse,
    FederationRegistrySubscribeRequest,
    FederationRegistrySubscribeResponse,
    FederationRevocationSubmitRequest,
    FederationRevocationSubmitResponse,
)
from api.services.crypto import sign_json
from . import runtime_cache
from .sse import format_sse


_BLOCKLIST_TTL_SECONDS = 2.0


async def get_blocklist(
    db: AsyncSession,
    page: int = 1,
    limit: int = 50,
    since: datetime | None = None,
) -> FederationBlocklistResponse:
    """Return the confirmed global revocation list."""
    since_key = since.isoformat() if since is not None else "none"
    cache_key = f"blocklist:{page}:{limit}:{since_key}"
    cached = runtime_cache.get(cache_key)
    if cached is not None:
        return cached

    offset = max(page - 1, 0) * limit
    conditions = [
        "ce.event_type = 'revocation'",
        "ce.is_confirmed = true",
    ]
    params: dict[str, object] = {
        "limit": limit,
        "offset": offset,
    }
    if since is not None:
        conditions.append("COALESCE(ce.confirmed_at, ce.indexed_at) >= :since")
        params["since"] = since

    result = await db.execute(
        text(
            f"""
            SELECT
                COALESCE(s.domain, ce.event_data->>'domain') AS domain,
                ce.event_data->>'reason_code' AS reason,
                COALESCE(ce.confirmed_at, ce.indexed_at) AS revoked_at,
                ce.tx_hash
            FROM chain_events ce
            LEFT JOIN services s
                ON s.id = ce.service_id
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(ce.confirmed_at, ce.indexed_at) DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    )
    rows = result.mappings().all()
    next_page = page + 1 if len(rows) == limit else None
    response = FederationBlocklistResponse(
        revocations=[FederationBlocklistEntry.model_validate(row) for row in rows],
        total=len(rows),
        next_page=next_page,
    )
    runtime_cache.set(cache_key, response, ttl_seconds=_BLOCKLIST_TTL_SECONDS)
    return response


async def stream_blocklist(
    db: AsyncSession,
    since: datetime | None = None,
) -> AsyncIterator[str]:
    """Yield a simple SSE snapshot of the current blocklist state."""
    snapshot = await get_blocklist(db=db, page=1, limit=100, since=since)
    for revocation in snapshot.revocations:
        yield format_sse("revocation", revocation.model_dump_json())
    yield format_sse("end", "{}")


async def subscribe_registry(
    db: AsyncSession,
    request: FederationRegistrySubscribeRequest,
) -> FederationRegistrySubscribeResponse:
    """Register or refresh one downstream federation subscriber."""
    try:
        result = await db.execute(
            text(
                """
                INSERT INTO federated_registries (
                    name,
                    endpoint,
                    webhook_url,
                    public_key_pem,
                    is_active,
                    created_at
                )
                VALUES (
                    :name,
                    :endpoint,
                    :webhook_url,
                    :public_key_pem,
                    true,
                    NOW()
                )
                ON CONFLICT (endpoint) DO UPDATE
                SET name = EXCLUDED.name,
                    webhook_url = EXCLUDED.webhook_url,
                    public_key_pem = EXCLUDED.public_key_pem,
                    is_active = true
                RETURNING id
                """
            ),
            {
                "name": request.name,
                "endpoint": request.endpoint,
                "webhook_url": request.webhook_url,
                "public_key_pem": request.public_key_pem,
            },
        )
        subscriber_id = result.scalar_one()
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to subscribe registry: {exc.__class__.__name__}",
        ) from exc

    return FederationRegistrySubscribeResponse(
        subscriber_id=subscriber_id,
        status="active",
    )


async def submit_federated_revocation(
    db: AsyncSession,
    request: FederationRevocationSubmitRequest,
) -> FederationRevocationSubmitResponse:
    """Accept a federated revocation for later review."""
    try:
        service_result = await db.execute(
            text("SELECT id FROM services WHERE domain = :domain"),
            {"domain": request.domain},
        )
        service_row = service_result.mappings().first()
        result = await db.execute(
            text(
                """
                INSERT INTO crawl_events (service_id, event_type, domain, details, created_at)
                VALUES (
                    :service_id,
                    'federated_revocation_submitted',
                    :domain,
                    CAST(:details AS JSONB),
                    NOW()
                )
                RETURNING id
                """
            ),
            {
                "service_id": None if service_row is None else service_row["id"],
                "domain": request.domain,
                "details": json.dumps(
                    {
                        "reason_code": request.reason_code,
                        "evidence_url": request.evidence_url,
                    }
                ),
            },
        )
        submission_id = result.scalar_one()
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to submit federated revocation: {exc.__class__.__name__}",
        ) from exc

    return FederationRevocationSubmitResponse(
        submission_id=submission_id,
        status="pending_review",
    )


async def dispatch_revocation_pushes(db: AsyncSession) -> dict[str, int]:
    """Push confirmed revocations to active subscriber webhooks."""
    registries_result = await db.execute(
        text(
            """
            SELECT id, webhook_url, last_push_at
            FROM federated_registries
            WHERE is_active = true
              AND webhook_url IS NOT NULL
            """
        )
    )
    registries = registries_result.mappings().all()
    pushed = 0

    for registry in registries:
        payload = await get_blocklist(
            db=db,
            page=1,
            limit=100,
            since=registry["last_push_at"],
        )
        if not payload.revocations:
            continue

        headers = {"Content-Type": "application/json"}
        body = payload.model_dump(mode="json")
        if settings.issuer_private_jwk:
            try:
                private_jwk = json.loads(settings.issuer_private_jwk)
                headers["X-AgentLedger-Signature"] = sign_json(body, private_jwk)
            except Exception:
                headers["X-AgentLedger-Signature"] = ""

        status_name = "success"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    registry["webhook_url"],
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
        except Exception:
            status_name = "failed"

        await db.execute(
            text(
                """
                UPDATE federated_registries
                SET last_push_at = CASE
                        WHEN :status_name = 'success' THEN NOW()
                        ELSE last_push_at
                    END,
                    last_push_status = :status_name,
                    push_failure_count = CASE
                        WHEN :status_name = 'failed' THEN push_failure_count + 1
                        ELSE push_failure_count
                    END
                WHERE id = :registry_id
                """
            ),
            {
                "registry_id": registry["id"],
                "status_name": status_name,
            },
        )
        if status_name == "success":
            pushed += len(payload.revocations)

    await db.commit()
    return {"pushed": pushed}
