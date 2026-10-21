"""A small `supply` schema and helpers shared by the store tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import (
    Attribute,
    Cardinality,
    Domain,
    Kind,
    Link,
    Record,
    Relation,
)
from lorebase_core.revision import Author, Origin
from lorebase_core.types import Constraints, TypeSpec
from lorebase_core.validate import LinkUpsert, RecordUpsert
from lorebase_store.commit import SchemaUpsert


def ident(prefix: str, number: int) -> str:
    """Deterministic, well-formed prefixed id."""
    return f"{prefix}_01JB8Z{number:020d}"


DOMAIN = "supply"
SUPPLIER = ident("knd", 1)
PART = ident("knd", 2)
SUPPLIER_CODE = ident("att", 101)
LEGAL_NAME = ident("att", 102)
COUNTRY = ident("att", 103)
RATING = ident("att", 104)
VAT = ident("att", 105)
NOTES = ident("att", 106)
PART_NUMBER = ident("att", 201)
PART_NAME = ident("att", 202)
SUPPLIED_BY = ident("rel", 1)
IS_PRIMARY = ident("att", 301)

AUTHOR = Author(principal="usr_test")
ORIGIN = Origin(type="test")
T = TypeSpec.of


class Clock:
    """Deterministic, strictly increasing clock."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def attribute(attribute_id: str, api_name: str, spec: TypeSpec, **extra: Any) -> Attribute:
    extra.setdefault("description", f"The {api_name.replace('_', ' ')}.")
    return Attribute(id=attribute_id, api_name=api_name, type=spec, **extra)


def domain() -> Domain:
    return Domain(id=DOMAIN, display_name="Supply", description="Supply chain.")


def supplier_kind(*, rating: TypeSpec | None = None, **changes: Any) -> Kind:
    attributes = [
        attribute(SUPPLIER_CODE, "supplier_code", T("string"), required=True),
        attribute(LEGAL_NAME, "legal_name", T("string"), required=True, searchable=True),
        attribute(
            COUNTRY,
            "country",
            T("string"),
            constraints=Constraints(min_length=2, max_length=2),
            searchable=True,
            search_weight="C",
        ),
        attribute(RATING, "rating", rating or T("string")),
        attribute(VAT, "vat_number", T("string"), unique=True),
    ]
    values: dict[str, Any] = {
        "id": SUPPLIER,
        "api_name": "Supplier",
        "description": "A company that supplies parts.",
        "primary_key": (SUPPLIER_CODE,),
        "title_attribute": LEGAL_NAME,
        "attributes": {a.id: a for a in attributes},
    }
    values.update(changes)
    return Kind(**values)


def part_kind() -> Kind:
    attributes = [
        attribute(PART_NUMBER, "part_number", T("string"), required=True),
        attribute(PART_NAME, "name", T("string"), required=True, searchable=True),
    ]
    return Kind(
        id=PART,
        api_name="Part",
        description="A part bought from suppliers.",
        primary_key=(PART_NUMBER,),
        title_attribute=PART_NAME,
        attributes={a.id: a for a in attributes},
    )


def supplied_by() -> Relation:
    return Relation(
        id=SUPPLIED_BY,
        api_name="supplied_by",
        inverse_api_name="supplies",
        description="Who supplies a part.",
        from_kind=PART,
        to_kind=SUPPLIER,
        cardinality=Cardinality.MANY_TO_ONE,
        attributes={IS_PRIMARY: attribute(IS_PRIMARY, "is_primary", T("boolean"))},
    )


def schema_changes() -> list[SchemaUpsert]:
    return [SchemaUpsert(supplier_kind()), SchemaUpsert(part_kind()), SchemaUpsert(supplied_by())]


def supplier(code: str, name: str, **values: Any) -> RecordUpsert:
    record_values: dict[str, Any] = {SUPPLIER_CODE: code, LEGAL_NAME: name}
    for api_name, value in values.items():
        record_values[{"country": COUNTRY, "rating": RATING, "vat": VAT}[api_name]] = value
    return RecordUpsert(record=Record(kind=SUPPLIER, key=code, values=record_values))


def part(number: str, name: str) -> RecordUpsert:
    return RecordUpsert(
        record=Record(kind=PART, key=number, values={PART_NUMBER: number, PART_NAME: name})
    )


def supplied(part_number: str, supplier_code: str, *, primary: bool | None = None) -> LinkUpsert:
    values = {} if primary is None else {IS_PRIMARY: primary}
    return LinkUpsert(
        link=Link(relation=SUPPLIED_BY, from_key=part_number, to_key=supplier_code, values=values)
    )


@asynccontextmanager
async def session_scope(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session that is closed afterwards; transactions are managed by the caller."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


LOCK_WAIT_TIMEOUT_S = 30.0
LOCK_POLL_INTERVAL_S = 0.02

# Ungranted locks of one type held by sessions of the current database. Transaction id locks
# carry no database, hence the filter on the session instead of `pg_locks.database`.
_LOCK_WAITERS = text(
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
    "WHERE NOT l.granted AND l.locktype = :locktype AND a.datname = current_database()"
)


async def wait_for_lock_waiter(
    engine: AsyncEngine, waiter: asyncio.Task[Any], *, locktype: str, what: str
) -> None:
    """Return once a session of this database is queued on a lock of `locktype` (`pg_locks`).

    `advisory` is a branch lock; a row lock held by another transaction is waited on as its
    `transactionid`. Each poll runs in its own transaction, so `pg_stat_activity` is read fresh.
    Fails with the waiter's own error when it finished instead of queueing, and after a bounded
    wait.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_WAIT_TIMEOUT_S
    async with engine.connect() as connection:
        while loop.time() < deadline:
            if waiter.done():
                waiter.result()  # raises the waiter's error
                raise AssertionError(f"the waiter finished without waiting on the {what}")
            async with connection.begin():
                waiting = await connection.scalar(_LOCK_WAITERS, {"locktype": locktype})
            if waiting:
                return
            await asyncio.sleep(LOCK_POLL_INTERVAL_S)
    raise AssertionError(f"no session waited on the {what} within {LOCK_WAIT_TIMEOUT_S} s")
