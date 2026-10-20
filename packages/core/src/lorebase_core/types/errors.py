"""Typed errors of the type system."""

from __future__ import annotations

from enum import StrEnum


class EncodingReason(StrEnum):
    """Why a value was rejected by a type."""

    WRONG_TYPE = "wrong_type"
    FORMAT = "format"
    OUT_OF_RANGE = "out_of_range"
    PRECISION = "precision"
    NOT_A_MEMBER = "not_a_member"
    INVALID_TEXT = "invalid_text"
    NOT_CANONICAL = "not_canonical"


class TypeSystemError(ValueError):
    """Base class of all type system errors."""


class EncodingError(TypeSystemError):
    """A value is not valid for a type."""

    def __init__(self, type_name: str, reason: EncodingReason, message: str) -> None:
        super().__init__(f"{type_name}: {message}")
        self.type_name = type_name
        self.reason = reason
        self.message = message


class CastError(TypeSystemError):
    """A value cannot be cast between two types (forbidden rule or failed checked cast)."""

    def __init__(self, from_type: str, to_type: str, message: str) -> None:
        super().__init__(f"cannot cast {from_type} to {to_type}: {message}")
        self.from_type = from_type
        self.to_type = to_type
        self.message = message
