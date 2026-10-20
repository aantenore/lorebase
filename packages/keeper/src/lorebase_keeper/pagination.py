"""Cursor pagination shared by every list endpoint.

A list request takes `limit` (bounded by settings) and an opaque `cursor`; the response is a
`Page` with `items` and `next_cursor` (null on the last page). Cursors are base64url JSON so
that routes can keep a keyset position in them; clients must treat them as opaque.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel

from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import VALIDATION_FAILED, ProblemError

MAX_CURSOR_LENGTH = 2048


class Page[T](BaseModel):
    """One page of results."""

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(position: Mapping[str, Any]) -> str:
    """Opaque cursor carrying a keyset position."""
    raw = json.dumps(position, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """Position stored in a cursor; `ValueError` when the cursor is not one we issued."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError):
        raise ValueError("malformed cursor") from None
    if not isinstance(decoded, dict):
        raise ValueError("malformed cursor")
    return decoded


@dataclass(frozen=True)
class PageParams:
    """Validated pagination request: effective limit and decoded cursor position."""

    limit: int
    position: dict[str, Any] | None


def _invalid(pointer: str, message: str) -> ProblemError:
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


def get_page_params(
    keeper: KeeperSettingsDep,
    limit: Annotated[int | None, Query(ge=1, description="Page size.")] = None,
    cursor: Annotated[
        str | None, Query(max_length=MAX_CURSOR_LENGTH, description="Opaque page cursor.")
    ] = None,
) -> PageParams:
    """Dependency resolving `limit` and `cursor` against the configured bounds."""
    bounds = keeper.pagination
    effective = bounds.default_limit if limit is None else limit
    if effective > bounds.max_limit:
        raise _invalid("/query/limit", f"must not exceed {bounds.max_limit}")
    position = None
    if cursor:
        try:
            position = decode_cursor(cursor)
        except ValueError:
            raise _invalid("/query/cursor", "is not a valid cursor") from None
    return PageParams(limit=effective, position=position)


PageParamsDep = Annotated[PageParams, Depends(get_page_params)]
