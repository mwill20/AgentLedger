"""Vector A crawl helpers."""

from __future__ import annotations

import json
from hashlib import sha256

WELL_KNOWN_MANIFEST_PATH = "/.well-known/agent-manifest.json"


def build_manifest_url(domain: str) -> str:
    """Build the well-known manifest URL for a domain."""
    return f"https://{domain}{WELL_KNOWN_MANIFEST_PATH}"


def compute_manifest_hash(payload: dict) -> str:
    """Hash a manifest payload deterministically."""
    serialized = json.dumps(payload, sort_keys=True)
    return sha256(serialized.encode("utf-8")).hexdigest()


def should_mark_service_inactive(consecutive_failures: int) -> bool:
    """Layer 1 marks a service inactive after three consecutive crawl failures."""
    return consecutive_failures >= 3
