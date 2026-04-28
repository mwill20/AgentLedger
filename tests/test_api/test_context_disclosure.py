"""Tests for Layer 4 context disclosure commitments."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from api.models.context import DisclosureRequest, DisclosureRevokeRequest
from api.services import context_disclosure


class _FakeMappings:
    """Minimal mappings wrapper for disclosure service tests."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    """Minimal result wrapper for scalar and mapping responses."""

    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def mappings(self):
        return _FakeMappings(self._rows)


class _InspectableSession:
    """Async DB double that records SQL and transaction state."""

    def __init__(self, rows=None, ids=None):
        self._rows = list(rows or [])
        self._ids = list(ids or [])
        self.executed = []
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, statement, params=None):
        sql_text = statement.text if hasattr(statement, "text") else str(statement)
        self.executed.append((sql_text, params or {}))
        if self._ids:
            return _FakeResult(scalar=self._ids.pop(0))
        return _FakeResult(rows=self._rows.pop(0) if self._rows else [])

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


class _FakeRedis:
    """Small Redis double for match-cache tests."""

    def __init__(self, value=None):
        self.value = value
        self.get_calls = []

    async def get(self, key):
        self.get_calls.append(key)
        return self.value


def _commitment_row(
    *,
    match_id,
    commitment_id,
    service_id,
    session_id=None,
    expires_at=None,
):
    """Build one commitment row with embedded match metadata."""
    return {
        "id": commitment_id,
        "match_id": match_id,
        "agent_did": "did:key:z6MkContextHealthAgent",
        "service_id": service_id,
        "session_assertion_id": session_id,
        "field_name": "user.insurance_id",
        "nonce": "nonce-for-insurance",
        "expires_at": expires_at
        or datetime.now(timezone.utc) + timedelta(minutes=4),
        "fields_requested": ["user.name", "user.insurance_id"],
        "fields_permitted": ["user.name"],
        "fields_withheld": [],
        "fields_committed": ["user.insurance_id"],
    }


def _disclosure_request(match_id, service_id, commitment_id) -> DisclosureRequest:
    """Build a valid disclosure request."""
    return DisclosureRequest(
        match_id=match_id,
        agent_did="did:key:z6MkContextHealthAgent",
        service_id=service_id,
        commitment_ids=[commitment_id],
        field_values={"user.name": "Michael Williams"},
    )


def test_generate_and_verify_commitment_round_trips():
    """Generated HMAC commitments should verify with the returned nonce."""
    commitment_hash, nonce = context_disclosure.generate_commitment("field-value")

    assert context_disclosure.verify_commitment(commitment_hash, nonce, "field-value")
    assert not context_disclosure.verify_commitment(commitment_hash, nonce, "tampered")


def test_create_commitments_persists_one_row_per_field():
    """Committed fields should be persisted and return commitment IDs."""
    commitment_ids = [uuid4(), uuid4()]
    db = _InspectableSession(ids=commitment_ids)
    match_id = uuid4()
    service_id = uuid4()
    session_id = uuid4()

    result = asyncio.run(
        context_disclosure.create_commitments(
            db=db,
            match_id=match_id,
            agent_did="did:key:z6MkContextHealthAgent",
            service_id=service_id,
            session_assertion_id=session_id,
            field_names=["user.insurance_id", "user.dob"],
            fields_requested=["user.name", "user.insurance_id", "user.dob"],
            fields_permitted=["user.name"],
            fields_withheld=[],
            fields_committed=["user.insurance_id", "user.dob"],
        )
    )

    assert result == commitment_ids
    assert len(db.executed) == 2
    assert "INSERT INTO context_commitments" in db.executed[0][0]
    assert db.executed[0][1]["match_id"] == match_id
    assert db.executed[0][1]["field_name"] == "user.insurance_id"
    assert db.executed[0][1]["fields_permitted"] == ["user.name"]


def test_disclose_context_releases_nonces_and_writes_audit_record():
    """Disclosure should release committed nonces and append an audit row."""
    match_id = uuid4()
    service_id = uuid4()
    commitment_id = uuid4()
    disclosure_id = uuid4()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=4)
    db = _InspectableSession(
        rows=[
            [
                _commitment_row(
                    match_id=match_id,
                    commitment_id=commitment_id,
                    service_id=service_id,
                    expires_at=expires_at,
                )
            ],
            [{"ontology_tag": "health.pharmacy.order", "trust_tier": 3, "trust_score": 84.0}],
            [{"field_name": "user.insurance_id", "sensitivity": "high"}],
            [{"field_name": "user.insurance_id", "nonce": "nonce-for-insurance"}],
            [{"id": disclosure_id, "created_at": now}],
        ]
    )
    redis = _FakeRedis()

    response = asyncio.run(
        context_disclosure.disclose_context(
            db=db,
            request=_disclosure_request(match_id, service_id, commitment_id),
            redis=redis,
        )
    )

    assert response.disclosure_id == disclosure_id
    assert response.permitted_fields == {"user.name": "Michael Williams"}
    assert response.committed_field_nonces == {
        "user.insurance_id": "nonce-for-insurance"
    }
    assert "user.insurance_id" not in response.permitted_fields
    assert response.expires_at == expires_at
    assert db.commit_count == 1
    assert any("SET nonce_released = true" in sql for sql, _ in db.executed)
    insert_params = db.executed[-1][1]
    assert insert_params["fields_disclosed"] == ["user.name"]
    assert insert_params["fields_committed"] == ["user.insurance_id"]
    assert insert_params["disclosure_method"] == "direct+committed"
    assert redis.get_calls[0] == f"context:match:{match_id}"


def test_disclose_context_blocks_when_trust_dropped_since_match():
    """Disclose-time trust re-verification is a hard block."""
    match_id = uuid4()
    service_id = uuid4()
    commitment_id = uuid4()
    db = _InspectableSession(
        rows=[
            [_commitment_row(match_id=match_id, commitment_id=commitment_id, service_id=service_id)],
            [{"ontology_tag": "health.pharmacy.order", "trust_tier": 2, "trust_score": 54.0}],
            [{"field_name": "user.insurance_id", "sensitivity": "high"}],
        ]
    )

    try:
        asyncio.run(
            context_disclosure.disclose_context(
                db=db,
                request=_disclosure_request(match_id, service_id, commitment_id),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected trust threshold failure")

    assert response.status_code == 403
    assert response.detail["trust_threshold_failed"] is True
    assert response.detail["fields"][0]["field"] == "user.insurance_id"
    assert not any("SET nonce_released = true" in sql for sql, _ in db.executed)
    assert db.rollback_count == 1


def test_disclose_context_returns_410_for_unknown_or_expired_match():
    """Unknown or expired match IDs should return Gone so agents retry matching."""
    db = _InspectableSession(rows=[[]])
    match_id = uuid4()
    service_id = uuid4()
    commitment_id = uuid4()

    try:
        asyncio.run(
            context_disclosure.disclose_context(
                db=db,
                request=_disclosure_request(match_id, service_id, commitment_id),
            )
        )
    except HTTPException as exc:
        response = exc
    else:  # pragma: no cover
        raise AssertionError("expected gone response")

    assert response.status_code == 410
    assert response.detail == "match_id expired or not found"
    assert db.rollback_count == 1


def test_list_disclosures_returns_field_names_only():
    """Audit list responses should expose field names but never field values."""
    disclosure_id = uuid4()
    service_id = uuid4()
    now = datetime.now(timezone.utc)
    db = _InspectableSession(
        rows=[
            [
                {
                    "id": disclosure_id,
                    "agent_did": "did:key:z6MkContextHealthAgent",
                    "service_id": service_id,
                    "ontology_tag": "health.pharmacy.order",
                    "fields_requested": ["user.name", "user.insurance_id"],
                    "fields_disclosed": ["user.name"],
                    "fields_withheld": [],
                    "fields_committed": ["user.insurance_id"],
                    "disclosure_method": "direct+committed",
                    "trust_score_at_disclosure": 84.0,
                    "trust_tier_at_disclosure": 3,
                    "erased": False,
                    "erased_at": None,
                    "created_at": now,
                    "total_count": 1,
                }
            ]
        ]
    )

    response = asyncio.run(
        context_disclosure.list_disclosures(
            db=db,
            agent_did="did:key:z6MkContextHealthAgent",
            limit=25,
            offset=0,
        )
    )

    assert response.total == 1
    record = response.disclosures[0]
    dumped = record.model_dump()
    assert dumped["fields_disclosed"] == ["user.name"]
    assert dumped["fields_committed"] == ["user.insurance_id"]
    assert "Michael Williams" not in str(dumped)
    assert "field_values" not in dumped


def test_revoke_disclosure_marks_erased_without_deleting_record():
    """Revocation should mark a row erased and clear field-name metadata."""
    disclosure_id = uuid4()
    erased_at = datetime.now(timezone.utc)
    db = _InspectableSession(rows=[[[{"id": disclosure_id, "erased_at": erased_at}][0]]])

    response = asyncio.run(
        context_disclosure.revoke_disclosure(
            db=db,
            disclosure_id=disclosure_id,
            request=DisclosureRevokeRequest(
                agent_did="did:key:z6MkContextHealthAgent",
            ),
        )
    )

    assert response.disclosure_id == disclosure_id
    assert response.erased_at == erased_at
    sql_text, params = db.executed[0]
    assert "UPDATE context_disclosures" in sql_text
    assert "DELETE" not in sql_text
    assert "erased = true" in sql_text
    assert "fields_disclosed = '{}'" in sql_text
    assert params["agent_did"] == "did:key:z6MkContextHealthAgent"
    assert db.commit_count == 1
