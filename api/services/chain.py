"""Hybrid chain abstraction for Layer 3 trust and audit flows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha3_256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.layer3 import ChainEventRecord, ChainEventsResponse, ChainStatusResponse
from api.services.crypto import canonical_json_bytes
from api.services import runtime_cache

try:
    from eth_account import Account
    from web3 import Web3
except ImportError:  # pragma: no cover - optional until web3 is installed
    Account = None
    Web3 = None


_CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "abi"
_CHAIN_STATUS_TTL_SECONDS = 1.0


def canonical_hash(payload: dict[str, Any]) -> str:
    """Hash one JSON payload in a deterministic EVM-style format."""
    payload_bytes = canonical_json_bytes(payload)
    if Web3 is not None:
        return Web3.keccak(payload_bytes).hex()
    return "0x" + sha3_256(payload_bytes).hexdigest()


def hash_identifier(value: str) -> str:
    """Hash one identifier string into a fixed-width digest."""
    payload_bytes = value.encode("utf-8")
    if Web3 is not None:
        return Web3.keccak(payload_bytes).hex()
    return "0x" + sha3_256(payload_bytes).hexdigest()


def _hex_to_bytes32(value: str) -> bytes:
    normalized = value[2:] if value.startswith("0x") else value
    return bytes.fromhex(normalized.rjust(64, "0"))


def _uuid_to_bytes32(value: str) -> bytes:
    return _hex_to_bytes32(hash_identifier(value))


def _chain_mode() -> str:
    mode = settings.chain_mode.lower()
    if mode in {"local", "web3"}:
        return mode
    if (
        Web3 is not None
        and settings.web3_provider_url
        and settings.attestation_ledger_contract_address
        and settings.audit_chain_contract_address
        and settings.chain_signer_private_key
    ):
        return "web3"
    return "local"


def is_web3_enabled() -> bool:
    """Return whether the repo is configured for live chain IO."""
    return _chain_mode() == "web3"


@lru_cache
def _load_contract_abi(name: str) -> list[dict[str, Any]]:
    path = _CONTRACTS_ROOT / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache
def _get_web3():
    if not is_web3_enabled():
        return None
    provider = Web3.HTTPProvider(settings.web3_provider_url)  # type: ignore[union-attr]
    return Web3(provider)  # type: ignore[misc]


def _get_contract(contract_name: str):
    web3 = _get_web3()
    if web3 is None:
        return None

    if contract_name == "AttestationLedger":
        address = settings.attestation_ledger_contract_address
    else:
        address = settings.audit_chain_contract_address
    if not address:
        return None
    return web3.eth.contract(
        address=web3.to_checksum_address(address),
        abi=_load_contract_abi(contract_name),
    )


def _send_contract_transaction(contract_function) -> tuple[str, int]:
    web3 = _get_web3()
    if web3 is None or Account is None:
        raise RuntimeError("web3 mode is not available")
    account = Account.from_key(settings.chain_signer_private_key)
    nonce = web3.eth.get_transaction_count(account.address)
    transaction = contract_function.build_transaction(
        {
            "from": account.address,
            "chainId": settings.chain_id,
            "nonce": nonce,
            "gas": 600000,
            "gasPrice": web3.eth.gas_price,
        }
    )
    signed = Account.sign_transaction(transaction, settings.chain_signer_private_key)
    raw_transaction = getattr(signed, "raw_transaction", None)
    if raw_transaction is None:
        raw_transaction = getattr(signed, "rawTransaction")
    tx_hash = web3.eth.send_raw_transaction(raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt["transactionHash"].hex(), int(receipt["blockNumber"])


async def next_block_number(db: AsyncSession) -> int:
    """Return the next synthetic block number for the local chain view."""
    result = await db.execute(
        text("SELECT COALESCE(MAX(block_number), 0) + 1 AS next_block FROM chain_events")
    )
    row = result.mappings().first()
    return int(row["next_block"] or 1)


async def _persist_indexed_event(
    db: AsyncSession,
    *,
    event_type: str,
    service_id: UUID | str | None,
    tx_hash: str,
    block_number: int,
    event_data: dict[str, Any],
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO chain_events (
                event_type,
                service_id,
                tx_hash,
                block_number,
                chain_id,
                is_confirmed,
                event_data,
                indexed_at
            )
            VALUES (
                :event_type,
                :service_id,
                :tx_hash,
                :block_number,
                :chain_id,
                false,
                CAST(:event_data AS JSONB),
                NOW()
            )
            ON CONFLICT (tx_hash) DO NOTHING
            """
        ),
        {
            "event_type": event_type,
            "service_id": service_id,
            "tx_hash": tx_hash,
            "block_number": block_number,
            "chain_id": settings.chain_id,
            "event_data": json.dumps(event_data),
        },
    )


def _remote_write(
    event_type: str,
    event_data: dict[str, Any],
) -> tuple[str, int] | None:
    if not is_web3_enabled():
        return None

    if event_type == "attestation":
        contract = _get_contract("AttestationLedger")
        if contract is None:
            return None
        tx_hash, block_number = _send_contract_transaction(
            contract.functions.recordAttestation(
                _hex_to_bytes32(event_data["service_chain_id"]),
                event_data["ontology_scope"],
                event_data.get("certification_ref") or "",
                int(event_data.get("expires_at_unix") or 0),
                _hex_to_bytes32(event_data["evidence_hash"]),
            )
        )
        return tx_hash, block_number

    if event_type == "revocation":
        contract = _get_contract("AttestationLedger")
        if contract is None:
            return None
        tx_hash, block_number = _send_contract_transaction(
            contract.functions.recordRevocation(
                _hex_to_bytes32(event_data["service_chain_id"]),
                event_data["reason_code"],
                _hex_to_bytes32(event_data["evidence_hash"]),
            )
        )
        return tx_hash, block_number

    if event_type == "version":
        contract = _get_contract("AttestationLedger")
        if contract is None:
            return None
        tx_hash, block_number = _send_contract_transaction(
            contract.functions.recordVersion(
                _hex_to_bytes32(event_data["service_chain_id"]),
                _hex_to_bytes32(event_data["manifest_hash"]),
            )
        )
        return tx_hash, block_number

    if event_type == "audit_batch":
        contract = _get_contract("AuditChain")
        if contract is None:
            return None
        tx_hash, block_number = _send_contract_transaction(
            contract.functions.commitBatch(
                _uuid_to_bytes32(event_data["batch_id"]),
                _hex_to_bytes32(event_data["merkle_root"]),
                int(event_data["record_count"]),
            )
        )
        return tx_hash, block_number

    return None


async def record_chain_event(
    db: AsyncSession,
    event_type: str,
    event_data: dict[str, Any],
    service_id: UUID | str | None = None,
) -> tuple[str, int]:
    """Persist one Layer 3 chain event into the indexed event log."""
    remote_result = _remote_write(event_type, event_data)
    if remote_result is None:
        block_number = await next_block_number(db)
        tx_hash = canonical_hash(
            {
                "event_type": event_type,
                "block_number": block_number,
                "nonce": str(uuid4()),
                "event_data": event_data,
            }
        )
    else:
        tx_hash, block_number = remote_result

    await _persist_indexed_event(
        db=db,
        event_type=event_type,
        service_id=service_id,
        tx_hash=tx_hash,
        block_number=block_number,
        event_data=event_data,
    )
    return tx_hash, block_number


def _latest_chain_block_from_provider() -> int | None:
    if not is_web3_enabled():
        return None
    web3 = _get_web3()
    if web3 is None:
        return None
    return int(web3.eth.block_number)


async def get_chain_status(db: AsyncSession) -> ChainStatusResponse:
    """Return current chain status metadata."""
    return await get_chain_status_for_tx(db=db, tx_hash=None)


async def get_chain_status_for_tx(
    db: AsyncSession,
    tx_hash: str | None,
) -> ChainStatusResponse:
    """Return chain status and optional confirmation depth for one tx."""
    cache_key = f"chain-status:{tx_hash or 'latest'}"
    cached = runtime_cache.get(cache_key)
    if cached is not None:
        return cached

    latest_block = _latest_chain_block_from_provider()
    if latest_block is None:
        result = await db.execute(
            text("SELECT COALESCE(MAX(block_number), 0) AS latest_block FROM chain_events")
        )
        row = result.mappings().first()
        latest_block = int(row["latest_block"] or 0)

    tracked_tx_hash = None
    tracked_block_number = None
    confirmation_depth = None
    is_confirmed = None
    if tx_hash:
        tracked_result = await db.execute(
            text(
                """
                SELECT tx_hash, block_number, is_confirmed
                FROM chain_events
                WHERE tx_hash = :tx_hash
                LIMIT 1
                """
            ),
            {"tx_hash": tx_hash},
        )
        tracked_row = tracked_result.mappings().first()
        if tracked_row is not None:
            tracked_tx_hash = tracked_row["tx_hash"]
            tracked_block_number = int(tracked_row["block_number"])
            confirmation_depth = max(0, latest_block - tracked_block_number)
            is_confirmed = bool(tracked_row["is_confirmed"])

    response = ChainStatusResponse(
        chain_id=settings.chain_id,
        network=settings.chain_network,
        latest_block=latest_block,
        contracts={
            "attestation_ledger": settings.attestation_ledger_contract_address,
            "audit_chain": settings.audit_chain_contract_address,
        },
        tracked_tx_hash=tracked_tx_hash,
        tracked_block_number=tracked_block_number,
        confirmation_depth=confirmation_depth,
        is_confirmed=is_confirmed,
    )
    runtime_cache.set(cache_key, response, ttl_seconds=_CHAIN_STATUS_TTL_SECONDS)
    return response


async def list_chain_events(
    db: AsyncSession,
    service_id: UUID | None = None,
    event_type: str | None = None,
    from_block: int | None = None,
    to_block: int | None = None,
    limit: int = 50,
) -> ChainEventsResponse:
    """Query indexed chain events."""
    conditions: list[str] = []
    params: dict[str, object] = {"limit": limit}
    if service_id is not None:
        conditions.append("service_id = :service_id")
        params["service_id"] = service_id
    if event_type is not None:
        conditions.append("event_type = :event_type")
        params["event_type"] = event_type
    if from_block is not None:
        conditions.append("block_number >= :from_block")
        params["from_block"] = from_block
    if to_block is not None:
        conditions.append("block_number <= :to_block")
        params["to_block"] = to_block
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                event_type,
                service_id,
                tx_hash,
                block_number,
                chain_id,
                is_confirmed,
                event_data,
                indexed_at,
                confirmed_at
            FROM chain_events
            {where_clause}
            ORDER BY block_number DESC, indexed_at DESC
            LIMIT :limit
            """
        ),
        params,
    )
    rows = result.mappings().all()
    return ChainEventsResponse(
        events=[ChainEventRecord.model_validate(row) for row in rows],
        total=len(rows),
    )


async def _service_hash_map(db: AsyncSession) -> dict[str, str]:
    result = await db.execute(text("SELECT id, domain FROM services"))
    return {hash_identifier(row["domain"]): str(row["id"]) for row in result.mappings().all()}


async def poll_remote_chain_events(db: AsyncSession) -> dict[str, int | str]:
    """Poll remote chain logs and mirror them into chain_events."""
    if not is_web3_enabled():
        return {"indexed_events": 0, "status": "noop"}

    web3 = _get_web3()
    if web3 is None:
        return {"indexed_events": 0, "status": "unavailable"}
    attestation_contract = _get_contract("AttestationLedger")
    audit_contract = _get_contract("AuditChain")
    if attestation_contract is None or audit_contract is None:
        return {"indexed_events": 0, "status": "unconfigured"}

    latest_block = int(web3.eth.block_number)
    result = await db.execute(
        text("SELECT COALESCE(MAX(block_number), :start_block - 1) AS max_block FROM chain_events"),
        {"start_block": settings.chain_start_block},
    )
    row = result.mappings().first()
    from_block = int(row["max_block"] or (settings.chain_start_block - 1)) + 1
    to_block = min(latest_block, from_block + settings.chain_index_window - 1)
    if from_block > to_block:
        return {"indexed_events": 0, "status": "up_to_date"}

    service_hashes = await _service_hash_map(db)
    indexed_events = 0

    event_specs = [
        ("attestation", attestation_contract.events.AttestationRecorded, "serviceId"),
        ("revocation", attestation_contract.events.RevocationRecorded, "serviceId"),
        ("version", attestation_contract.events.VersionRecorded, "serviceId"),
        ("audit_batch", audit_contract.events.BatchAnchorCommitted, None),
    ]
    for event_type, event_factory, service_key in event_specs:
        try:
            logs = event_factory.get_logs(fromBlock=from_block, toBlock=to_block)
        except Exception:
            continue
        for event in logs:
            args = dict(event["args"])
            service_id = None
            if service_key is not None:
                service_hash = "0x" + args[service_key].hex()
                service_id = service_hashes.get(service_hash)
            tx_hash = event["transactionHash"].hex()
            event_data = {key: _jsonable_event_value(value) for key, value in args.items()}
            await _persist_indexed_event(
                db=db,
                event_type=event_type,
                service_id=service_id,
                tx_hash=tx_hash,
                block_number=int(event["blockNumber"]),
                event_data=event_data,
            )
            indexed_events += 1

    await db.commit()
    return {"indexed_events": indexed_events, "status": "indexed"}


def _jsonable_event_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable_event_value(item) for item in value]
    return value


async def confirm_pending_events(db: AsyncSession) -> dict[str, int]:
    """Promote indexed chain events past the confirmation window."""
    latest_block = _latest_chain_block_from_provider()
    if latest_block is None:
        status_snapshot = await get_chain_status(db)
        latest_block = status_snapshot.latest_block
    confirm_before = latest_block - settings.chain_confirmation_blocks
    if confirm_before < 1:
        return {"confirmed_events": 0}

    result = await db.execute(
        text(
            """
            UPDATE chain_events
            SET is_confirmed = true,
                confirmed_at = NOW()
            WHERE is_confirmed = false
              AND block_number <= :confirm_before
            RETURNING event_type, service_id, tx_hash
            """
        ),
        {"confirm_before": confirm_before},
    )
    rows = result.mappings().all()
    now = datetime.now(timezone.utc)
    affected_service_ids: set[str] = set()
    for row in rows:
        if row["event_type"] == "attestation":
            await db.execute(
                text(
                    """
                    UPDATE attestation_records
                    SET is_confirmed = true,
                        confirmed_at = :confirmed_at
                    WHERE tx_hash = :tx_hash
                    """
                ),
                {"tx_hash": row["tx_hash"], "confirmed_at": now},
            )
            if row["service_id"] is not None:
                affected_service_ids.add(str(row["service_id"]))
        elif row["event_type"] == "audit_batch":
            await db.execute(
                text(
                    """
                    UPDATE audit_batches
                    SET status = 'confirmed',
                        confirmed_at = :confirmed_at
                    WHERE tx_hash = :tx_hash
                    """
                ),
                {"tx_hash": row["tx_hash"], "confirmed_at": now},
            )
        elif row["event_type"] == "revocation" and row["service_id"] is not None:
            affected_service_ids.add(str(row["service_id"]))

    await db.execute(
        text(
            """
            UPDATE audit_batches AS ab
            SET status = 'confirmed',
                confirmed_at = COALESCE(ab.confirmed_at, ce.confirmed_at, :confirmed_at)
            FROM chain_events AS ce
            WHERE ce.event_type = 'audit_batch'
              AND ce.is_confirmed = true
              AND ab.tx_hash = ce.tx_hash
              AND (ab.status <> 'confirmed' OR ab.confirmed_at IS NULL)
            """
        ),
        {"confirmed_at": now},
    )

    if affected_service_ids:
        from api.services import trust

        for service_id in affected_service_ids:
            await trust.recompute_service_trust(db=db, service_id=service_id)

    await db.commit()
    return {"confirmed_events": len(rows)}
