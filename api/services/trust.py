"""Layer 3 trust recomputation helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import ranker


async def recompute_service_trust(
    db: AsyncSession,
    service_id: str,
) -> dict[str, float | int | bool]:
    """Recompute one service's trust score and upgrade tier 4 when eligible."""
    service_result = await db.execute(
        text(
            """
            SELECT
                id,
                trust_tier,
                last_verified_at,
                is_banned
            FROM services
            WHERE id = :service_id
            """
        ),
        {"service_id": service_id},
    )
    service_row = service_result.mappings().first()
    if service_row is None:
        raise ValueError("service not found")

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
    local_success_count = int(reputation_row["success_count"] or 0)
    local_failure_count = int(reputation_row["failure_count"] or 0)

    federated_result = await db.execute(
        text(
            """
            SELECT AVG((details->>'score')::float) AS federated_score
            FROM crawl_events
            WHERE service_id = :service_id
              AND event_type = 'federated_reputation_signal'
              AND created_at >= NOW() - INTERVAL '30 days'
            """
        ),
        {"service_id": service_id},
    )
    federated_row = federated_result.mappings().first()
    federated_score = (
        None
        if federated_row is None or federated_row["federated_score"] is None
        else float(federated_row["federated_score"])
    )

    revocation_result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS total_count
            FROM chain_events
            WHERE service_id = :service_id
              AND event_type = 'revocation'
              AND is_confirmed = true
            """
        ),
        {"service_id": service_id},
    )
    revocation_row = revocation_result.mappings().first()
    is_globally_revoked = bool(int(revocation_row["total_count"] or 0))

    attestation_result = await db.execute(
        text(
            """
            SELECT
                ar.ontology_scope,
                ar.recorded_at,
                ar.expires_at,
                a.did AS auditor_did
            FROM attestation_records ar
            JOIN auditors a
                ON a.id = ar.auditor_id
            WHERE ar.service_id = :service_id
              AND ar.is_active = true
              AND ar.is_confirmed = true
              AND (ar.expires_at IS NULL OR ar.expires_at > NOW())
              AND a.is_active = true
            ORDER BY ar.recorded_at DESC
            """
        ),
        {"service_id": service_id},
    )
    attestations = []
    for row in attestation_result.mappings().all():
        did_value = row["auditor_did"]
        auditor_org_id = did_value.rsplit(":", 1)[-1]
        attestations.append(
            {
                "ontology_scope": row["ontology_scope"],
                "recorded_at": row["recorded_at"],
                "is_expired": (
                    row["expires_at"] is not None
                    and row["expires_at"] <= datetime.now(timezone.utc)
                ),
                "auditor_org_id": auditor_org_id,
            }
        )

    attestation_score = ranker.compute_attestation_score(
        has_active_service_identity=False,
        attestations=attestations,
    )
    reputation_score = ranker.compute_reputation_score(
        local_success_count,
        local_failure_count,
        federated_score=federated_score,
        is_blocklisted=is_globally_revoked,
    )
    trust_score = ranker.compute_trust_score(
        capability_probe_score=capability_probe_score,
        attestation_score=attestation_score,
        operational_score=operational_score,
        reputation_score=reputation_score,
    )

    trust_tier = int(service_row["trust_tier"] or 1)
    if ranker.evaluate_trust_tier_4(attestations, is_globally_revoked):
        trust_tier = 4

    await db.execute(
        text(
            """
            UPDATE services
            SET trust_score = :trust_score,
                trust_tier = :trust_tier,
                is_banned = CASE WHEN :globally_revoked THEN true ELSE is_banned END,
                ban_reason = CASE
                    WHEN :globally_revoked THEN 'globally_revoked_on_chain'
                    ELSE ban_reason
                END,
                updated_at = NOW()
            WHERE id = :service_id
            """
        ),
        {
            "service_id": service_id,
            "trust_score": trust_score,
            "trust_tier": trust_tier,
            "globally_revoked": is_globally_revoked,
        },
    )
    return {
        "trust_score": trust_score,
        "trust_tier": trust_tier,
        "attestation_score": attestation_score,
        "reputation_score": reputation_score,
        "is_globally_revoked": is_globally_revoked,
    }
