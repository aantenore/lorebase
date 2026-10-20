"""Bounds of compiling and evaluating one expression (the sandbox budget).

Every bound is a field with a documented default; services expose the object in their
settings (``keeper.operations.expression_limits``) so deployments can tune it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExprLimits(BaseModel):
    """Budget of one expression. Exceeding a bound is an ``expression.limit_exceeded`` error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_source_length: int = Field(
        default=4096, ge=1, description="Characters of expression source accepted by compile."
    )
    max_depth: int = Field(
        default=48,
        ge=1,
        description="Nesting depth of the expression tree (operators, calls, literals).",
    )
    max_steps: int = Field(
        default=10_000,
        ge=1,
        description="Evaluation steps (one per node visited, comprehension iterations "
        "included) of one evaluation.",
    )
    max_duration_ms: int = Field(
        default=250,
        ge=1,
        description="Wall-clock budget of one evaluation; a safety net behind max_steps.",
    )
    max_list_size: int = Field(
        default=10_000, ge=1, description="Elements of a list or map built during evaluation."
    )
    max_string_length: int = Field(
        default=65_536, ge=1, description="Characters of a string built during evaluation."
    )
    max_record_lookups: int = Field(
        default=100,
        ge=0,
        description="Records and link lists read through record(), links() and links.<name> "
        "in one evaluation.",
    )
    decimal_precision: int = Field(
        default=34,
        ge=1,
        description="Significant digits of decimal arithmetic (34 is IEEE 754 decimal128).",
    )
