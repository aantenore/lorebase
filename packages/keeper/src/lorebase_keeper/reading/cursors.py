"""Opaque cursors of read endpoints, bound to the request they were issued for.

A cursor carries a keyset position and a fingerprint of the request shape (query, filters,
ordering), so a cursor replayed against a different request is rejected instead of silently
returning a wrong page.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError
from lorebase_keeper.pagination import decode_cursor, encode_cursor


def fingerprint(*parts: Any) -> str:
    """Short stable digest of the request parts a cursor depends on."""
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def issue(shape: str, position: Sequence[Any]) -> str:
    return encode_cursor({"q": shape, "p": list(position)})


def position_of(cursor: str | None, shape: str, pointer: str) -> list[Any] | None:
    """Position of a cursor issued for `shape`; 422 naming `pointer` otherwise."""
    if not cursor:
        return None
    try:
        decoded = decode_cursor(cursor)
    except ValueError:
        decoded = {}
    position = decoded.get("p")
    if decoded.get("q") != shape or not isinstance(position, list):
        raise invalid_cursor(pointer)
    return position


def invalid_cursor(pointer: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={
            "errors": [
                {
                    "pointer": pointer,
                    "message": "is not a cursor issued for this request",
                    "code": "invalid_cursor",
                }
            ]
        },
    )
