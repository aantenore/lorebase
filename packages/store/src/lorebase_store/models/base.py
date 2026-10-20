"""Declarative base, constraint naming convention and shared column helpers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

HASH_PARTITIONED = {"postgresql_partition_by": "HASH (domain_id)"}
"""Table option for tables hash-partitioned on `domain_id` (partitions come from migrations)."""


class Base(DeclarativeBase):
    """Base of every Lorebase table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSONB,
        bytes: LargeBinary,
        str: Text,
        datetime: DateTime(timezone=True),
    }


H32 = bytes
"""A 32-byte SHA-256 digest stored as `bytea`."""

DomainKey = Annotated[str, mapped_column(Text, primary_key=True)]
"""`domain_id` as the leading primary key column."""


def h32_check(column: str) -> CheckConstraint:
    """Check that `column` holds exactly 32 bytes."""
    return CheckConstraint(f"octet_length({column}) = 32", name=f"{column}_h32")


def domain_fk(*, ondelete: str = "CASCADE") -> ForeignKeyConstraint:
    """`domain_id` references `domains.id`; rows go with their Domain."""
    return ForeignKeyConstraint(["domain_id"], ["domains.id"], ondelete=ondelete)


def pg_enum(enum: type[StrEnum], name: str) -> Enum:
    """Native Postgres enum type `name` storing the member values of `enum`."""
    return Enum(enum, name=name, values_callable=lambda members: [m.value for m in members])
