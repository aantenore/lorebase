"""CSV and Postgres profilers of schema inference."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    Integer,
    Interval,
    LargeBinary,
    Numeric,
    String,
    Text,
    TypeEngine,
)

from lorebase_core.infer import TableProfile, infer_schema
from lorebase_core.model import Cardinality
from lorebase_core.types import AttributeType, TypeSpec
from lorebase_store.db import create_engine
from lorebase_store.profiling import (
    CsvProfileError,
    profile_bytes,
    profile_file,
    profile_postgres,
    type_spec,
)
from lorebase_store.settings import (
    CsvProfilingSettings,
    DatabaseSettings,
    PostgresProfilingSettings,
)

SUPPLY_DATA = Path(__file__).resolve().parents[3] / "examples" / "supply" / "data"
CSV = CsvProfilingSettings()


def values(profile: TableProfile, column: str) -> list[object]:
    found = next(c for c in profile.columns if c.name == column)
    return list(found.values)


# CSV -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "name"),
    [
        (b"id,name\n1,a\n2,b\n", "t.csv"),
        (b"id;name\n1;a\n2;b\n", "t.csv"),
        (b"id|name\n1|a\n2|b\n", "t.csv"),
        (b"id\tname\n1\ta\n2\tb\n", "t.tsv"),
        (b"id\tname\n1\ta\n2\tb\n", "t.txt"),
        (b'id,name\r\n1,"a, quoted"\r\n2,b\r\n', "t.csv"),
    ],
)
def test_delimiters_are_detected(content: bytes, name: str) -> None:
    profile = profile_bytes(content, name, CSV)
    assert profile.name == "t"
    assert [c.name for c in profile.columns] == ["id", "name"]
    assert values(profile, "id") == ["1", "2"]
    assert profile.row_count == 2


def test_header_and_ragged_rows() -> None:
    profile = profile_bytes(b"id,,name\n1,x\n2,y,b,extra\n\n", "dir/raw.csv", CSV)
    assert profile.name == "raw"
    assert [c.name for c in profile.columns] == ["id", "column_2", "name"]
    assert values(profile, "name") == [None, "b"]
    assert profile.row_count == 2


def test_encoding_fallback() -> None:
    profile = profile_bytes("city\nMünchen\n".encode("cp1252"), "t.csv", CSV)
    assert values(profile, "city") == ["München"]
    bom = profile_bytes("﻿city\nLyon\n".encode(), "t.csv", CSV)
    assert [c.name for c in bom.columns] == ["city"]
    with pytest.raises(CsvProfileError):
        profile_bytes(b"\xff\xfe", "t.csv", CsvProfilingSettings(encodings=("ascii",)))


@pytest.mark.parametrize(
    ("content", "settings"),
    [
        (b"", CSV),
        (b"a,b,c\n1,2,3\n", CsvProfilingSettings(max_columns=2)),
    ],
)
def test_unreadable_files(content: bytes, settings: CsvProfilingSettings) -> None:
    with pytest.raises(CsvProfileError):
        profile_bytes(content, "t.csv", settings)


def _numbers(count: int) -> bytes:
    return ("n\n" + "".join(f"{i}\n" for i in range(count))).encode()


def test_head_sampling() -> None:
    profile = profile_bytes(
        _numbers(100), "t.csv", CsvProfilingSettings(sample_rows=5, sampling="head")
    )
    assert values(profile, "n") == ["0", "1", "2", "3", "4"]
    assert profile.row_count == 100


def test_reservoir_sampling_is_seeded_and_in_file_order() -> None:
    settings = CsvProfilingSettings(sample_rows=10)
    first = values(profile_bytes(_numbers(1000), "t.csv", settings), "n")
    again = values(profile_bytes(_numbers(1000), "t.csv", settings), "n")
    other = values(
        profile_bytes(_numbers(1000), "t.csv", settings.model_copy(update={"seed": 7})), "n"
    )
    assert first == again != other
    assert len(first) == 10
    assert [int(str(v)) for v in first] == sorted(int(str(v)) for v in first)


def test_supply_files_feed_inference() -> None:
    profiles = [profile_file(path, CSV) for path in sorted(SUPPLY_DATA.glob("*.csv"))]
    proposal = infer_schema(profiles, "supply")
    kinds = {k.api_name for k in proposal.schema.kinds.values()}
    assert kinds == {"Contract", "Part", "Plant", "PurchaseOrder", "RiskFlag", "Supplier"}
    many_to_one = [
        r for r in proposal.schema.relations.values() if r.cardinality is Cardinality.MANY_TO_ONE
    ]
    assert len(many_to_one) == 5


# Postgres types --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql_type", "expected", "max_length"),
    [
        (Boolean(), TypeSpec.of(AttributeType.BOOLEAN), None),
        (Integer(), TypeSpec.of(AttributeType.INTEGER), None),
        (BigInteger(), TypeSpec.of(AttributeType.INTEGER), None),
        (Numeric(12, 2), TypeSpec.decimal(12, 2), None),
        (Numeric(), None, None),
        (Float(), TypeSpec.of(AttributeType.FLOAT), None),
        (postgresql.DOUBLE_PRECISION(), TypeSpec.of(AttributeType.FLOAT), None),
        (Date(), TypeSpec.of(AttributeType.DATE), None),
        (DateTime(timezone=True), TypeSpec.of(AttributeType.TIMESTAMP), None),
        (Interval(), TypeSpec.of(AttributeType.DURATION), None),
        (JSON(), TypeSpec.of(AttributeType.JSON), None),
        (postgresql.JSONB(), TypeSpec.of(AttributeType.JSON), None),
        (postgresql.UUID(), TypeSpec.of(AttributeType.STRING), None),
        (Enum("low", "high", name="level"), TypeSpec.enum("low", "high"), None),
        (Text(), TypeSpec.of(AttributeType.TEXT), None),
        (String(40), TypeSpec.of(AttributeType.STRING), 40),
        (String(), TypeSpec.of(AttributeType.STRING), None),
        (ARRAY(Integer()), TypeSpec.array(TypeSpec.of(AttributeType.INTEGER)), None),
        (LargeBinary(), None, None),
    ],
)
def test_catalog_type_mapping(
    sql_type: TypeEngine[object], expected: TypeSpec | None, max_length: int | None
) -> None:
    assert type_spec(sql_type) == (expected, max_length)


# Postgres tables (integration) -----------------------------------------------------------------

_DDL = [
    "CREATE TABLE supplier (supplier_code varchar(16) PRIMARY KEY, legal_name text NOT NULL,"
    " vat_number varchar(20) UNIQUE)",
    "COMMENT ON TABLE supplier IS 'Approved suppliers'",
    "CREATE TABLE purchase_order (po_number varchar(16) PRIMARY KEY,"
    " vendor varchar(16) NOT NULL REFERENCES supplier(supplier_code),"
    " qty integer NOT NULL, price numeric(10,2), ordered date)",
    "INSERT INTO supplier VALUES ('S-1001', 'Kavsen', 'PL1'), ('S-1002', 'Sundello', NULL)",
    "INSERT INTO purchase_order VALUES ('PO-1', 'S-1001', 5, 1.50, '2025-01-02'),"
    " ('PO-2', 'S-1002', 7, NULL, NULL)",
]


@pytest.mark.integration
async def test_postgres_tables_are_profiled(empty_database_url: str) -> None:
    engine = create_engine(DatabaseSettings(), url=empty_database_url, pooled=False)
    try:
        async with engine.begin() as connection:
            for statement in _DDL:
                await connection.execute(text(statement))
        profiles = await profile_postgres(engine, PostgresProfilingSettings(exact_row_count=True))
    finally:
        await engine.dispose()
    by_name = {p.name: p for p in profiles}
    assert set(by_name) == {"purchase_order", "supplier"}
    supplier = by_name["supplier"]
    assert supplier.primary_key == ("supplier_code",)
    assert supplier.comment == "Approved suppliers"
    assert supplier.row_count == 2
    vat = next(c for c in supplier.columns if c.name == "vat_number")
    assert vat.unique is True
    assert vat.declared_max_length == 20
    proposal = infer_schema(profiles, "erp")
    relation = proposal.schema.relation_by_api_name("vendor")
    assert relation is not None
    assert relation[0].cardinality is Cardinality.MANY_TO_ONE
    order = proposal.schema.kind_by_api_name("PurchaseOrder")
    assert order is not None
    price = order.attribute_by_api_name("price")
    assert price is not None
    assert price.type == TypeSpec.decimal(10, 2)
