"""Shared base classes and naming rules of schema elements."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, JsonValue


class Model(BaseModel):
    """Frozen model base. ``content()`` is the JSON payload used for canonical hashing."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    def content(self) -> dict[str, JsonValue]:
        """JSON-compatible payload: field aliases, absent (``None``) fields omitted."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


#: Role names of a steward list (design 10.7): the names grants use, at most this long.
MAX_STEWARD_ROLE_CHARS = 64
#: Upper bound of the roles one steward list names.
MAX_STEWARD_ROLES = 20


def steward_roles(value: object) -> object:
    """Normalize a steward list before validation: an empty list is the same as none (absent,
    so canonical content without stewards is unchanged); duplicates are dropped in order."""
    if isinstance(value, list | tuple):
        if not value:
            return None
        return tuple(dict.fromkeys(value))
    return value


class ElementStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class SchemaElement(Model):
    """A versioned schema element: stable ``id`` plus renamable ``api_name`` (AC-01)."""

    #: Object type used by the canonical hashing scheme (design 6.1).
    object_type: ClassVar[str]

    id: str
    api_name: str
    display_name: str = ""
    description: str = ""


class NameStyle(StrEnum):
    """Api name styles (design 5.1)."""

    SNAKE = "snake_case"
    PASCAL = "PascalCase"
    DOTTED_SNAKE = "dotted.snake_case"


_NAME_PATTERNS: dict[NameStyle, re.Pattern[str]] = {
    NameStyle.SNAKE: re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"),
    NameStyle.PASCAL: re.compile(r"[A-Z][A-Za-z0-9]*"),
    NameStyle.DOTTED_SNAKE: re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"),
}


def is_valid_api_name(name: str, style: NameStyle) -> bool:
    return _NAME_PATTERNS[style].fullmatch(name) is not None


def snake_case(name: str) -> str:
    """``RiskFlag`` -> ``risk_flag``; used to derive built-in Operation names."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
