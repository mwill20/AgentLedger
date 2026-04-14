"""Direct tests for session replay hardening paths."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException

from api.services import sessions


class _FakeRedis:
    """Async Redis double with NX semantics for replay tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def set(self, key: str, value, ex=None, nx=False):
        async with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True


def test_session_proof_nonce_single_winner_under_concurrency():
    """Concurrent session proof attempts should allow one winner per nonce."""
    redis = _FakeRedis()

    async def _run_once():
        try:
            await sessions._store_proof_nonce(redis, "did:key:z6MkRaceAgent", "same-nonce")
            return "ok"
        except HTTPException as exc:
            return exc.status_code

    async def _exercise():
        return await asyncio.gather(*[_run_once() for _ in range(25)])

    results = asyncio.run(_exercise())

    assert results.count("ok") == 1
    assert results.count(422) == 24
