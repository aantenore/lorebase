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
