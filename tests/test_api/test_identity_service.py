"""Direct tests for identity service hardening paths."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.services import identity


class _FakeMappings:
    """Minimal SQLAlchemy mappings wrapper for service tests."""

    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    """Minimal SQLAlchemy result wrapper for service tests."""

    def __init__(self, row=None, scalar_value=None):
        self._row = row
        self._scalar_value = scalar_value

    def mappings(self):
        return _FakeMappings(self._row)

    def scalar_one(self):
        return self._scalar_value


class _FakeSession:
    """Async DB session double with queued results."""

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)
        self.execute_calls: list[tuple[tuple, dict]] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        if self._results:
            return self._results.pop(0)
        return _FakeResult()

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeRedis:
    """Async Redis double with NX semantics and simple key-value storage."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value, ex=None, nx=False):
        async with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True


def test_verify_agent_online_returns_cached_revocation(monkeypatch):
    """Cached revocation should short-circuit online verification before DB lookup."""
    redis = _FakeRedis()
    did_value = "did:key:z6MkCachedRevoked"
    redis.values[identity._revocation_cache_key(did_value)] = json.dumps({"did": did_value})
    db = _FakeSession([])

    monkeypatch.setattr(identity, "_require_identity_runtime", lambda: None)
    monkeypatch.setattr(
        identity.credentials,
        "verify_agent_credential",
        lambda token: {"sub": did_value},
    )

    result = asyncio.run(
        identity.verify_agent_online(
            db=db,
            credential_jwt="header.payload.signature.with-enough-length",
            redis=redis,
        )
    )

    assert result.valid is False
    assert result.is_revoked is True
    assert db.execute_calls == []


def test_authenticate_agent_credential_rejects_cached_revocation(monkeypatch):
    """Bearer auth should reject a revoked credential from Redis without DB access."""
    redis = _FakeRedis()
    did_value = "did:key:z6MkCachedRevoked"
    redis.values[identity._revocation_cache_key(did_value)] = json.dumps({"did": did_value})
    db = _FakeSession([])

    monkeypatch.setattr(identity, "_require_identity_runtime", lambda: None)
    monkeypatch.setattr(
        identity.credentials,
        "verify_agent_credential",
        lambda token: {"sub": did_value},
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            identity.authenticate_agent_credential(
                db=db,
                credential_jwt="header.payload.signature.with-enough-length",
                redis=redis,
            )
        )

    assert exc_info.value.status_code == 403
    assert db.execute_calls == []


def test_revoke_agent_writes_revocation_cache(monkeypatch):
    """Revoking an agent should write a revocation marker into Redis."""
    revoked_at = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    redis = _FakeRedis()
    db = _FakeSession(
        [
            _FakeResult(
                row={
                    "did": "did:key:z6MkRevokeMe",
                    "is_revoked": False,
                    "revoked_at": None,
                }
            ),
            _FakeResult(),
            _FakeResult(),
            _FakeResult(scalar_value=revoked_at),
        ]
    )

    request = type(
        "Req",
        (),
        {"reason_code": "key_compromised", "evidence": {"source": "test"}},
    )()

    result = asyncio.run(
        identity.revoke_agent(
            db=db,
            did_value="did:key:z6MkRevokeMe",
            request=request,
            revoked_by="test-admin-key",
            redis=redis,
        )
    )

    cached = json.loads(redis.values[identity._revocation_cache_key("did:key:z6MkRevokeMe")])
    assert result.reason_code == "key_compromised"
    assert cached["did"] == "did:key:z6MkRevokeMe"
    assert cached["reason_code"] == "key_compromised"
    assert db.committed is True


def test_identity_proof_nonce_single_winner_under_concurrency():
    """Concurrent registration proof attempts should allow one winner per nonce."""
    redis = _FakeRedis()

    async def _run_once():
        try:
            await identity._store_proof_nonce(redis, "did:key:z6MkRaceAgent", "same-nonce")
            return "ok"
        except HTTPException as exc:
            return exc.status_code

    async def _exercise():
        return await asyncio.gather(*[_run_once() for _ in range(25)])

    results = asyncio.run(_exercise())

    assert results.count("ok") == 1
    assert results.count(422) == 24
