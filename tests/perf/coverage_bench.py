"""Overhead of the `coverage` block of `POST /query` (design 8.11) and the cost of counting it.

Method: the Domain of `resolve_bench.py` (`--records` Products, loaded through the commit
pipeline), a tenth of them without a color. The coverage statistics pass
(`lorebase_store.coverage.run_coverage`) is timed once over the default branch head.

- Overhead: the real Keeper application in process (ASGI transport), authentication mode
  `none`, connected as a role subject to row-level security, response cache and rate limits
  off. The same filtered query (`color`, `brand`, ordered by `price`, 20 Records) is sent with
  `coverage: true` and `coverage: false`, alternating, each timed client side.
- Lookup: the statements the block adds (`read_coverage` and `last_runs` for one Source), timed
  alone in a read-only transaction as that role: the overhead without HTTP and JSON noise.

Usage (Docker for a Postgres 16 container, or LOREBASE_TEST_DATABASE_URL for a server):
    uv run python tests/perf/coverage_bench.py [--records 100000] [--iterations 500] \
        [--warmup 50] [--out build/perf/coverage.md]
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from harness import Measurement
from resolve_bench import (
    AUTHOR,
    BATCH,
    DOMAIN,
    ROLE_PASSWORD,
    SCHEMA,
    Clock,
    _commit,
    _execute,
    _server_version,
    products,
)
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_core.model import Record
from lorebase_core.model.yaml_schema import load_schema
from lorebase_core.validate import RecordUpsert
from lorebase_keeper.app import create_app
from lorebase_keeper.settings import KeeperSettings
from lorebase_platform.settings import Settings
from lorebase_store import coverage, migrations, testing
from lorebase_store.commit import SchemaUpsert, create_domain
from lorebase_store.db import create_engine
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import CoverageSettings, DatabaseSettings, StoreSettings

QUERY = {
    "kind": "Product",
    "where": {
        "and": [
            {"attr": "color", "op": "eq", "value": "Rosso"},
            {"attr": "brand", "op": "eq", "value": "Brand C3"},
        ]
    },
    "order_by": [{"attr": "price", "dir": "desc"}],
    "limit": 20,
}


def without_color(changes: list[RecordUpsert], color_id: str) -> list[RecordUpsert]:
    """Every tenth Record without a color."""
    return [
        RecordUpsert(
            record=Record(
                kind=c.record.kind,
                key=c.record.key,
                values={k: v for k, v in c.record.values.items() if k != color_id},
            )
        )
        if index % 10 == 0
        else c
        for index, c in enumerate(changes)
    ]


async def run(args: argparse.Namespace, owner_url: str, app_url: str) -> dict[str, Any]:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app_engine = create_engine(DatabaseSettings(), url=app_url, pooled=False)
    app_sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    clock = Clock()
    schema = load_schema(SCHEMA)
    product = schema.kind_by_api_name("Product")
    assert product is not None
    color = product.attribute_by_api_name("color")
    assert color is not None
    result: dict[str, Any] = {}
    try:
        async with sessions() as session:
            await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock)
        await _commit(
            sessions,
            runtime,
            clock,
            [],
            schema_changes=[SchemaUpsert(e) for e in schema.elements()],
        )
        for start in range(0, args.records, BATCH):
            changes = products(schema, start, min(BATCH, args.records - start))
            await _commit(sessions, runtime, clock, list(without_color(changes, color.id)))
        await _execute(owner_url, "VACUUM ANALYZE")
        started = time.perf_counter()
        report = await coverage.run_coverage(sessions, DOMAIN, settings=CoverageSettings())
        result["pass_ms"] = (time.perf_counter() - started) * 1000
        assert report.counted, report.as_dict()
        result["lookup"] = await _lookup(args, app_sessions, product.id)
        result["query"] = await _queries(args, app_url)
    finally:
        await engine.dispose()
        await app_engine.dispose()
    return result


async def _lookup(
    args: argparse.Namespace, sessions: async_sessionmaker[Any], kind_id: str
) -> Measurement:
    measurement = Measurement("coverage statements (read_coverage + last_runs)")
    for index in range(args.warmup + args.iterations):
        async with sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await scope_to_domain(session, DOMAIN)
            started = time.perf_counter()
            await coverage.read_coverage(session, DOMAIN, "main", [kind_id])
            await coverage.last_runs(session, DOMAIN, ["src_perf"])
            elapsed = (time.perf_counter() - started) * 1000
        if index >= args.warmup:
            measurement.samples_ms.append(elapsed)
    return measurement


async def _queries(args: argparse.Namespace, app_url: str) -> dict[str, Measurement]:
    keeper = KeeperSettings()
    settings = Settings(
        keeper={"reads": {"cache": {"enabled": False}}, "limits": {"rate": {"enabled": False}}},
        database={"dsn": app_url},
    )
    app = create_app(settings)
    base = f"{keeper.api_prefix}/domains/{DOMAIN}/query"
    found = {flag: Measurement(f"query, coverage {flag}") for flag in ("on", "off")}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        for index in range(args.warmup + args.iterations):
            for flag in ("on", "off") if index % 2 else ("off", "on"):
                started = time.perf_counter()
                response = await http.post(base, json={**QUERY, "coverage": flag == "on"})
                elapsed = (time.perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(f"{response.status_code} {response.text}")
                block = response.json()["coverage"]
                if (flag == "on") != (block is not None):
                    raise RuntimeError(f"coverage {flag}: {block!r}")
                if index >= args.warmup:
                    found[flag].samples_ms.append(elapsed)
    return found


def render(args: argparse.Namespace, result: dict[str, Any], server: str) -> str:
    on, off = result["query"]["on"], result["query"]["off"]
    lookup = result["lookup"]
    lines = [
        f"# Coverage benchmark, {date.today().isoformat()}",
        "",
        f"- {args.records} Products, a tenth without a color; statistics pass "
        f"{result['pass_ms']:.0f} ms.",
        f"- {args.iterations} timed calls each after {args.warmup} warm-up calls; Keeper in "
        "process, role subject to row-level security.",
        f"- {platform.platform()}, Python {platform.python_version()}, Postgres: {server}.",
        "",
        "| Measure | p50 ms | p95 ms | p99 ms |",
        "|---|---:|---:|---:|",
    ]
    for measurement in (off, on, lookup):
        lines.append(
            f"| {measurement.name} | {measurement.p50:.2f} | {measurement.p95:.2f} | "
            f"{measurement.p99:.2f} |"
        )
    lines += [
        "",
        f"Overhead of the block: p50 {on.p50 - off.p50:+.2f} ms, p95 {on.p95 - off.p95:+.2f} ms "
        "(end to end).",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    with testing.postgres_server() as server, testing.scratch_database(server) as url:
        role = f"lorebase_coverage_{uuid.uuid4().hex[:8]}"
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
