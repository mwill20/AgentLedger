"""Small helpers for Server-Sent Events payload formatting."""

from __future__ import annotations


def format_sse(event: str, data: str) -> str:
    """Render one SSE frame."""
    return f"event: {event}\ndata: {data}\n\n"
