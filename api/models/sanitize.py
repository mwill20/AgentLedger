"""Shared input sanitization helpers for Pydantic models.

All string inputs pass through these checks before any business
logic or database interaction occurs.
"""

from __future__ import annotations

from typing import Any


def contains_null_bytes(value: str) -> bool:
    """Check if a string contains null bytes."""
    return "\x00" in value


def strip_strings_recursive(data: Any) -> Any:
    """Recursively strip whitespace from all string values in a dict/list.

    Operates on the raw Pydantic input (before validation), so it
    handles nested dicts and lists from the JSON body.
    """
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        return {k: strip_strings_recursive(v) for k, v in data.items()}
    if isinstance(data, list):
        return [strip_strings_recursive(item) for item in data]
    return data


def check_null_bytes_recursive(data: Any, path: str = "") -> list[str]:
    """Find all string fields containing null bytes.

    Returns a list of dotted field paths where null bytes were found.
    """
    violations: list[str] = []
    if isinstance(data, str):
        if contains_null_bytes(data):
            violations.append(path or "value")
    elif isinstance(data, dict):
        for key, val in data.items():
            child_path = f"{path}.{key}" if path else key
            violations.extend(check_null_bytes_recursive(val, child_path))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            child_path = f"{path}[{i}]"
            violations.extend(check_null_bytes_recursive(item, child_path))
    return violations
