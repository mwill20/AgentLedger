"""Domain verification logic."""

from __future__ import annotations

from uuid import UUID


def expected_dns_txt_token(service_id: UUID | str) -> str:
    """Build the expected TXT verification record."""
    return f"agentledger-verify={service_id}"


def verify_txt_records(service_id: UUID | str, txt_records: list[str]) -> bool:
    """Match TXT records against the expected verification token."""
    expected = expected_dns_txt_token(service_id).lower()
    normalized = {record.strip().strip('"').lower() for record in txt_records}
    return expected in normalized
