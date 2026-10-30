"""A small retail Domain for the resolution tests (design 8.9): schema, deterministic Records
and question -> expected candidate cases measuring precision and recall."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Record, Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import encode_value
from lorebase_core.validate import RecordUpsert
from lorebase_store.commit import CommitResult, SchemaUpsert, commit, create_domain
from lorebase_store.runtime import StoreRuntime

from .supply import Clock

DOMAIN = "shop"
AUTHOR = Author(principal="usr_loader")

SCHEMA = f"""
format: lorebase.schema/v1
domain: {{id: {DOMAIN}, display_name: Shop, description: A small retail catalog.}}
kinds:
  Product:
    description: An article for sale.
    aliases: [article, item]
    primary_key: [sku]
    title_attribute: name
    search: {{embedding_template: "{{name}}"}}
    attributes:
      sku: {{type: string, required: true, description: Stock keeping unit.}}
      name: {{type: string, required: true, description: Product name.}}
      category:
        type: string
        description: Merchandise category.
        value_aliases:
          Calzature: [shoes, footwear]
          Borse: [bags, handbags]
          Abbigliamento: [clothing, apparel]
      color:
        type: string
        description: Main color.
        aliases: [colour]
        value_aliases: {{Rosso: [red, rouge], Nero: [black], Blu: [blue]}}
      size: {{type: {{enum: [S, M, L, XL]}}, description: Size.}}
      price:
        type: decimal(10,2)
        description: Unit selling price in euro.
        aliases: [cost, amount]
      tags: {{type: array<string>, description: Labels., resolvable: true}}
      notes: {{type: text, description: Free text notes.}}
  Store:
    description: A shop selling products.
    aliases: [shop, outlet]
    primary_key: [code]
    title_attribute: city
    attributes:
      code: {{type: string, required: true, description: Store code.}}
      city:
        type: string
        description: Town of the store.
        value_aliases: {{Milano: [milan], Roma: [rome]}}
      region: {{type: {{enum: [North, Center, South]}}, description: Sales region.}}
  Customer:
    description: A buyer.
    primary_key: [number]
    title_attribute: number
    attributes:
      number: {{type: string, required: true, description: Customer number.}}
      segment:
        type: string
        sensitivity: [confidential]
        description: Marketing segment.
      loyalty: {{type: {{enum: [bronze, silver, gold]}}, description: Loyalty tier.}}
"""

CATEGORIES = ("Calzature", "Borse", "Abbigliamento")
COLORS = ("Rosso", "red", "Nero", "Blu", "Verde")
SIZES = ("S", "M", "L", "XL")
CITIES = (
    ("Milano", "North"),
    ("Roma", "Center"),
    ("Napoli", "South"),
    ("Torino", "North"),
    # Non-Latin scripts and punctuation inside values.
    ("".join(chr(c) for c in (0x92E, 0x941, 0x902, 0x92C, 0x908)), "South"),
    ("Rock & Roll", "Center"),
    ("S/M", "North"),
    ("50%", "South"),
)
NOUNS = ("Runner", "Tote", "Jacket", "Loafer", "Clutch", "Shirt", "Boot", "Satchel", "Coat")


def shop_schema() -> Schema:
    return load_schema(SCHEMA)


def _record(schema: Schema, kind_name: str, key: str, values: dict[str, Any]) -> RecordUpsert:
    kind = schema.kind_by_api_name(kind_name)
    assert kind is not None
    encoded: dict[str, Any] = {}
    for name, value in values.items():
        attribute = kind.attribute_by_api_name(name)
        assert attribute is not None, name
        encoded[attribute.id] = encode_value(attribute.type, value)
    return RecordUpsert(record=Record(kind=kind.id, key=key, values=encoded))


def products(
    schema: Schema, count: int, *, start: int = 0, color: str | None = None
) -> list[RecordUpsert]:
    return [
        _record(
            schema,
            "Product",
            f"P-{i:06d}",
            {
                "sku": f"P-{i:06d}",
                "name": f"{NOUNS[i % len(NOUNS)]} {i}",
                "category": CATEGORIES[i % len(CATEGORIES)],
                "color": color or COLORS[i % len(COLORS)],
                "size": SIZES[i % len(SIZES)],
                "price": str(Decimal(10 + i % 190) + Decimal("0.99")),
                "tags": ["new", "sale"] if i % 4 == 0 else ["new"],
                "notes": f"Generated product {i} with a longer free text description.",
            },
        )
        for i in range(start, start + count)
    ]


def stores(schema: Schema) -> list[RecordUpsert]:
    return [
        _record(schema, "Store", f"ST-{i}", {"code": f"ST-{i}", "city": city, "region": region})
        for i, (city, region) in enumerate(CITIES)
    ]


def customers(schema: Schema, count: int) -> list[RecordUpsert]:
    return [
        _record(
            schema,
            "Customer",
            f"C-{i:04d}",
            {
                "number": f"C-{i:04d}",
                "segment": ("students", "families", "seniors")[i % 3],
                "loyalty": ("bronze", "silver", "gold")[i % 3],
            },
        )
        for i in range(count)
    ]


async def load_shop(
    session: AsyncSession,
    runtime: StoreRuntime,
    clock: Clock,
    *,
    product_count: int = 300,
    batch: int = 5000,
) -> tuple[Schema, list[CommitResult]]:
    """Create the Domain, commit the schema, then the Records in batches."""
    schema = shop_schema()
    results = [
        await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock)
    ]
    results.append(
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(e) for e in schema.elements()],  # type: ignore[arg-type]
            author=AUTHOR,
            message="Load the shop schema",
            origin=Origin(type="import"),
            runtime=runtime,
            clock=clock,
        )
    )
    changes: list[Any] = [*stores(schema), *customers(schema, 60)]
    for start in range(0, product_count, batch):
        changes += products(schema, min(batch, product_count - start), start=start)
        results.append(await commit_changes(session, runtime, clock, changes))
        changes = []
    return schema, results


async def commit_changes(
    session: AsyncSession,
    runtime: StoreRuntime,
    clock: Clock,
    changes: Sequence[Any],
    branch: str = "main",
) -> CommitResult:
    return await commit(
        session,
        DOMAIN,
        branch,
        list(changes),
        author=AUTHOR,
        message="Import shop data",
        origin=Origin(type="import"),
        runtime=runtime,
        clock=clock,
    )


# Evaluation -------------------------------------------------------------------------------

Expected = tuple[str, str, str | None, frozenset[str] | None]
"""(target, Kind, Attribute or None, values or None)."""


@dataclass(frozen=True)
class Case:
    text: str
    expected: frozenset[Expected]


def _v(kind: str, attribute: str, *values: str) -> Expected:
    return ("value", kind, attribute, frozenset(values))


def _a(kind: str, attribute: str) -> Expected:
    return ("attribute", kind, attribute, None)


def _k(kind: str) -> Expected:
    return ("kind", kind, None, None)


CASES = (
    Case(
        "average price of red shoes",
        frozenset(
            {
                _a("Product", "price"),
                _v("Product", "color", "Rosso", "red"),
                _v("Product", "category", "Calzature"),
            }
        ),
    ),
    Case(
        "how many black bags",
        frozenset({_v("Product", "color", "Nero"), _v("Product", "category", "Borse")}),
    ),
    Case("stores in milan", frozenset({_k("Store"), _v("Store", "city", "Milano")})),
    Case(
        "products of size XL",
        frozenset({_k("Product"), _a("Product", "size"), _v("Product", "size", "XL")}),
    ),
    Case("gold customers", frozenset({_k("Customer"), _v("Customer", "loyalty", "gold")})),
    Case("items on sale", frozenset({_k("Product"), _v("Product", "tags", "sale")})),
    Case(
        "cost of apparel in rome",
        frozenset(
            {
                _a("Product", "price"),
                _v("Product", "category", "Abbigliamento"),
                _v("Store", "city", "Roma"),
            }
        ),
    ),
    Case(
        "rossso handbags",
        frozenset({_v("Product", "color", "Rosso"), _v("Product", "category", "Borse")}),
    ),
    Case(
        "colour of footwear",
        frozenset({_a("Product", "color"), _v("Product", "category", "Calzature")}),
    ),
    Case(
        "north region outlets",
        frozenset({_k("Store"), _a("Store", "region"), _v("Store", "region", "North")}),
    ),
    Case("torrino shops", frozenset({_k("Store"), _v("Store", "city", "Torino")})),
)
"""Expected candidates per text. "rossso" and "torrino" are misspelled: only the trigram level
reaches them."""


def found(candidates: Sequence[dict[str, Any]]) -> set[Expected]:
    return {
        (
            c["target"],
            c["kind"],
            c.get("attribute"),
            None if c.get("values") is None else frozenset(c["values"]),
        )
        for c in candidates
    }


def precision_recall(
    results: Sequence[tuple[Case, Sequence[dict[str, Any]]]],
) -> tuple[float, float]:
    """Micro-averaged over the cases: returned candidates that were expected, and expected
    candidates that were returned."""
    hits = returned = wanted = 0
    for case, candidates in results:
        got = found(candidates)
        hits += len(got & case.expected)
        returned += len(got)
        wanted += len(case.expected)
    return hits / max(returned, 1), hits / max(wanted, 1)
