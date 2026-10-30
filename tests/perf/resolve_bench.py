"""Latency of `POST /resolve` (design 8.9) and the cost of keeping the value index.

Method: a scratch database migrated to head holds a retail-like Domain of `--records` Products
(category, color and brand strings, a size enum, a tags array, a unique name and a price),
loaded through the commit pipeline in batches. A selection pass (`lorebase_store.values`) then
backfills the value index; its time is the cost of a full rebuild.

- Resolution: the real Keeper application in process (ASGI transport), authentication mode
  `none`, connected as a role subject to row-level security (the read functions granted, as in
  production), response cache and rate limits off. Each timed call sends one random text of a
  set and is timed client side: `exact` texts reach values and names through levels 1-2
  (exact names, values and aliases); `trigram` texts are misspelled, so they reach level 3.
  Each set is measured with only its levels enabled (`levels` in the request).
- Upkeep: commits that update `--update-rows` Products' colors, timed with the color Attribute
  in the index and again with the index switched off (`store.values.enabled: false`, so the
  triggers only probe an empty selection); the difference is what the triggers add.

Usage (Docker for a Postgres 16 container, or LOREBASE_TEST_DATABASE_URL for a server):
    uv run python tests/perf/resolve_bench.py [--records 100000] [--iterations 500] \
        [--warmup 50] [--update-rows 500] [--commits 20] [--out build/perf/resolve.md]
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import random
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from harness import Measurement
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from lorebase_core.model import Record, Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import encode_value
from lorebase_core.validate import RecordUpsert
from lorebase_keeper.app import create_app
from lorebase_keeper.settings import KeeperSettings
from lorebase_platform.settings import Settings
from lorebase_store import migrations, testing, values
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.db import create_engine
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

DOMAIN = "retail"
AUTHOR = Author(principal="usr_perf")
ROLE_PASSWORD = "resolve-bench-role"
BATCH = 5000
SEED = 20261101

SCHEMA = f"""
format: lorebase.schema/v1
domain: {{id: {DOMAIN}, display_name: Retail}}
kinds:
  Product:
    description: An article for sale.
    aliases: [article, item]
    primary_key: [sku]
    title_attribute: name
    attributes:
      sku: {{type: string, required: true, description: Stock keeping unit.}}
      name: {{type: string, required: true, description: Product name.}}
      category:
        type: string
        description: Merchandise category.
        value_aliases: {{Calzature: [shoes, footwear], Borse: [bags]}}
      color:
        type: string
        description: Main color.
        value_aliases: {{Rosso: [red], Nero: [black], Blu: [blue]}}
      brand: {{type: string, description: Maker.}}
      size: {{type: {{enum: [XS, S, M, L, XL]}}, description: Size.}}
      price: {{type: "decimal(10,2)", description: Unit price., aliases: [cost]}}
      tags: {{type: array<string>, description: Labels.}}
"""

CATEGORIES = [
    "Calzature", "Borse", "Abbigliamento", "Accessori", "Gioielli", "Orologi", "Cosmetici",
    "Profumi", "Occhiali", "Cinture", "Cappelli", "Sciarpe", "Guanti", "Valigie", "Ombrelli",
    "Intimo", "Costumi", "Sport", "Casa", "Tessili",
]  # fmt: skip
COLORS = [
    "Rosso", "Nero", "Blu", "Bianco", "Verde", "Giallo", "Grigio", "Marrone", "Beige", "Rosa",
    "Viola", "Arancione",
]  # fmt: skip
BRANDS = [f"Brand {chr(65 + i % 26)}{i // 26}" for i in range(200)]
TAGS = ["new", "sale", "eco", "limited", "online", "outlet"]
EXACT_TEXTS = [
    "average price of red shoes",
    "how many black bags",
    "blue accessori in size XL",
    "verde abbigliamento items",
    "cost of grigio orologi",
    "brand C3 cinture on sale",
    "eco products in size S",
    "nero valigie limited",
]
TRIGRAM_TEXTS = [
    "rossso calzatur",
    "abbigliamnto",
    "orologgi neri",
    "marone cinture",
    "giallo ombreli",
    "profumii",
    "accesori bianchi",
    "valige blu",
]


def products(
    schema: Schema, start: int, count: int, color: str | None = None
) -> list[RecordUpsert]:
    kind = schema.kind_by_api_name("Product")
    assert kind is not None
    ids = {a.api_name: a for a in kind.attributes.values()}
    changes = []
    for i in range(start, start + count):
        raw: dict[str, Any] = {
            "sku": f"P-{i:07d}",
            "name": f"Product {i}",
            "category": CATEGORIES[i % len(CATEGORIES)],
            "color": color or COLORS[(i // 3) % len(COLORS)],
            "brand": BRANDS[(i * 7) % len(BRANDS)],
            "size": ("XS", "S", "M", "L", "XL")[i % 5],
            "price": str(Decimal(5 + i % 500) + Decimal("0.90")),
            "tags": [TAGS[i % len(TAGS)], TAGS[(i // 6) % len(TAGS)]],
        }
        encoded = {ids[k].id: encode_value(ids[k].type, v) for k, v in raw.items()}
        changes.append(RecordUpsert(record=Record(kind=kind.id, key=raw["sku"], values=encoded)))
    return changes


class Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


async def _execute(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _commit(
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    changes: list[Any],
    **options: Any,
) -> float:
    started = time.perf_counter()
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            changes,
            author=AUTHOR,
            message="perf",
            origin=Origin(type="import"),
            runtime=runtime,
            clock=clock,
            **options,
        )
    return (time.perf_counter() - started) * 1000


async def _sync(
    sessions: async_sessionmaker[AsyncSession], settings: StoreSettings
) -> tuple[float, values.SelectionReport]:
    started = time.perf_counter()
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        report = await values.sync_selection(session, DOMAIN, settings=settings, force=True)
    return (time.perf_counter() - started) * 1000, report


async def run(args: argparse.Namespace, owner_url: str, app_url: str) -> dict[str, Any]:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    schema = load_schema(SCHEMA)
    result: dict[str, Any] = {}
    try:
        async with sessions() as session:
            await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock)
        schema_changes = [SchemaUpsert(e) for e in schema.elements()]
        await _commit(sessions, runtime, clock, [], schema_changes=schema_changes)
        started = time.perf_counter()
        for start in range(0, args.records, BATCH):
            count = min(BATCH, args.records - start)
            await _commit(sessions, runtime, clock, products(schema, start, count))
        result["load_s"] = time.perf_counter() - started
        await _execute(owner_url, "VACUUM ANALYZE")
        result["backfill_ms"], report = await _sync(sessions, runtime.settings)
        result["selected"] = len(report.selected)
        result["value_rows"] = report.backfilled
        await _execute(owner_url, "ANALYZE mat_values")
        result["resolve"] = await _resolve(args, app_url)
        result["upkeep"] = await _upkeep(args, sessions, runtime, clock, schema)
    finally:
        await engine.dispose()
    return result


async def _resolve(args: argparse.Namespace, app_url: str) -> dict[str, Measurement]:
    keeper = KeeperSettings()
    settings = Settings(
        keeper={"reads": {"cache": {"enabled": False}}, "limits": {"rate": {"enabled": False}}},
        database={"dsn": app_url},
    )
    app = create_app(settings)
    base = f"{keeper.api_prefix}/domains/{DOMAIN}/resolve"
    sets = {
        "levels 1-2 (exact, synonym)": (EXACT_TEXTS, ["exact", "synonym"]),
        "level 3 (trigram)": (TRIGRAM_TEXTS, ["trigram"]),
        "all levels, mixed text": (EXACT_TEXTS + TRIGRAM_TEXTS, None),
    }
    found: dict[str, Measurement] = {}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        for name, (texts, levels) in sets.items():
            rng = random.Random(SEED)
            measurement = Measurement(name)
            for index in range(args.warmup + args.iterations):
                body: dict[str, Any] = {"text": rng.choice(texts)}
                if levels is not None:
                    body["levels"] = levels
                started = time.perf_counter()
                response = await http.post(base, json=body)
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f"{name}: {response.status_code} {response.text}")
                if not response.json()["candidates"]:
                    raise RuntimeError(f"{name}: no candidate for {body['text']!r}")
                if index >= args.warmup:
                    measurement.samples_ms.append(elapsed)
            found[name] = measurement
    return found


async def _upkeep(
    args: argparse.Namespace,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    clock: Clock,
    schema: Schema,
) -> dict[str, Measurement]:
    rng = random.Random(SEED)

    def repaint() -> list[RecordUpsert]:
        start = rng.randrange(0, args.records - args.update_rows)
        return products(schema, start, args.update_rows, color=rng.choice(COLORS))

    found = {"index on": Measurement("index on"), "index off": Measurement("index off")}
    off = runtime.settings.model_copy(
        update={"values": runtime.settings.values.model_copy(update={"enabled": False})}
    )
    for state in ("index on", "index off", "index on", "index off"):
        await _sync(sessions, off if state == "index off" else runtime.settings)
        for _ in range(args.commits // 2):
            found[state].samples_ms.append(await _commit(sessions, runtime, clock, repaint()))
    return found


def render(args: argparse.Namespace, result: dict[str, Any], server: str) -> str:
    lines = [
        f"# Resolve benchmark, {date.today().isoformat()}",
        "",
        f"- {args.records} Products, loaded in {result['load_s']:.0f} s; "
        f"{result['selected']} Attributes indexed, {result['value_rows']} value rows.",
        f"- Backfill of the value index (selection pass over every Record): "
        f"{result['backfill_ms']:.0f} ms.",
        f"- {args.iterations} timed calls per set after {args.warmup} warm-up calls; Keeper in "
        "process, role subject to row-level security, definer functions on.",
        f"- {platform.platform()}, Python {platform.python_version()}, Postgres: {server}.",
        "",
        "| Resolution | p50 ms | p95 ms | p99 ms |",
        "|---|---:|---:|---:|",
    ]
    for measurement in result["resolve"].values():
        lines.append(
            f"| {measurement.name} | {measurement.p50:.2f} | {measurement.p95:.2f} | "
            f"{measurement.p99:.2f} |"
        )
    on, off = result["upkeep"]["index on"], result["upkeep"]["index off"]
    extra = on.p50 - off.p50
    lines += [
        "",
        f"| Commit of {args.update_rows} color updates | p50 ms | p95 ms |",
        "|---|---:|---:|",
        f"| value index on | {on.p50:.1f} | {on.p95:.1f} |",
        f"| value index off | {off.p50:.1f} | {off.p95:.1f} |",
        "",
        f"Upkeep: {extra:.1f} ms per commit at p50 "
        f"({extra / args.update_rows * 1000:.0f} µs per updated Record).",
    ]
    return "\n".join(lines) + "\n"


async def _server_version(url: str) -> str:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return str(await connection.scalar(text("SHOW server_version")))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--update-rows", type=int, default=500)
    parser.add_argument("--commits", type=int, default=20)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    with testing.postgres_server() as server, testing.scratch_database(server) as url:
        role = f"lorebase_resolve_{uuid.uuid4().hex[:8]}"
        asyncio.run(_execute(url, f"CREATE ROLE {role} LOGIN PASSWORD '{ROLE_PASSWORD}'"))
        try:
            migrations.upgrade(
                database=DatabaseSettings(application_role=role), store=StoreSettings(), url=url
            )
            asyncio.run(
                _execute(
                    url,
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f"TO {role}",
                    f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}",
                )
            )
            app_url = (
                make_url(url)
                .set(username=role, password=ROLE_PASSWORD)
                .render_as_string(hide_password=False)
            )
            result = asyncio.run(run(args, url, app_url))
            report = render(args, result, asyncio.run(_server_version(url)))
        finally:
            asyncio.run(_execute(url, f"DROP OWNED BY {role}", f"DROP ROLE {role}"))
    print(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)


if __name__ == "__main__":
    main()
