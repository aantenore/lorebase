"""Read-path performance harness: seed the `supply` Domain at scale, then time the hot paths.

The dataset comes from `examples/supply/scripts/make_supply_data.py` (deterministic) and goes
through the real commit pipeline, so materialized rows, search vectors and per-Attribute
indexes are the ones production builds. `extra_links` adds that many `supplied_by` and
`stocked_at` Links per Part to reach a denser graph (about three Links per Record).

Each measured operation is shaped like one agent request: a transaction that scopes RLS to
the Domain and runs the store primitive. Timings are client side over a local socket.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Link, Record, Schema, load_schema
from lorebase_core.revision import Author, Origin
from lorebase_core.types import AttributeType, encode_value
from lorebase_core.validate import DataChange, LinkUpsert, RecordUpsert
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.definer import grant_read_functions
from lorebase_store.indexes import BranchIndexes, run_index_jobs
from lorebase_store.layout import BranchLayout, load_layout
from lorebase_store.materialize import BranchMode, MaterializationMode, create_branch
from lorebase_store.reads import (
    Comparison,
    TitleSimilarity,
    get_record,
    head_schema,
    index_spec,
    links_of_record,
    list_records,
    query_by_attribute,
    search_records,
)
from lorebase_store.repositories import RefRepository, scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.traverse import neighborhood

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "supply"
DOMAIN = "supply"
AUTHOR = Author(principal="usr_perf")
ORIGIN = Origin(type="perf")
READER_ROLE = "lorebase_perf_reader"

RECORD_FILES = {
    "supplier": "Supplier",
    "part": "Part",
    "plant": "Plant",
    "purchase_order": "PurchaseOrder",
    "contract": "Contract",
    "risk_flag": "RiskFlag",
}
LINK_FILES = ("supplied_by", "stocked_at", "for_part", "delivered_to", "with_supplier", "flags")
#: Record files committed together with the Links their Kind requires (`required_from`).
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("supplier", ()),
    ("plant", ()),
    ("part", ("supplied_by", "stocked_at")),
    ("purchase_order", ("for_part", "delivered_to")),
    ("contract", ("with_supplier",)),
    ("risk_flag", ("flags",)),
)

TARGETS_MS = {
    "get by key": 5.0,
    "list page of 50": 20.0,
    "links of a record": 10.0,
    "2-hop neighborhood": 30.0,
    "query by indexed attribute": 25.0,
    "full-text search": 50.0,
    "full-text search with typo-tolerant titles": 50.0,
}
#: Keeper's lexical search defaults (`search.trigram_threshold`, `search.trigram_weight`).
TITLE_SIMILARITY = TitleSimilarity(threshold=0.3, weight=0.5)


@dataclass(frozen=True, slots=True)
class Scale:
    factor: int
    extra_links: int
    commit_batch: int = 10000
    seed: int = 20260115


@dataclass
class SeedReport:
    records: int
    links: int
    seconds: float
    commits: int


@dataclass
class Measurement:
    name: str
    samples_ms: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def p95(self) -> float:
        ordered = sorted(self.samples_ms)
        return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


# Data -------------------------------------------------------------------------------------------


def generator() -> ModuleType:
    path = EXAMPLE / "scripts" / "make_supply_data.py"
    spec = importlib.util.spec_from_file_location("make_supply_data", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def perf_schema() -> Schema:
    """The example schema without Operations and Derivations (CEL is not enabled in commits).

    Attributes computed by a Derivation are dropped with it.
    """
    schema = load_schema((EXAMPLE / "schema.yaml").read_text(encoding="utf-8"))
    kinds = {}
    for kind in schema.kinds.values():
        kept = {a.id: a for a in kind.attributes.values() if a.derivation_id is None}
        kinds[kind.id] = kind.model_copy(update={"attributes": kept})
    return schema.model_copy(update={"kinds": kinds, "operations": {}, "derivations": {}})


def _typed(attribute: Any, raw: str) -> Any:
    if raw == "":
        return None
    if attribute.type.name is AttributeType.BOOLEAN:
        return raw == "true"
    return encode_value(attribute.type, raw)


def build_changes(schema: Schema, scale: Scale) -> list[list[DataChange]]:
    """Batches of changes, each a group of Records with the Links their Kind requires."""
    module = generator()
    tables = module.build_dataset(
        module.Settings(scale.factor, scale.seed, date.fromisoformat(module.DEFAULT_AS_OF))
    )
    _densify(tables, scale, random.Random(scale.seed))
    batches: list[list[DataChange]] = []
    for stem, link_stems in GROUPS:
        kind = schema.kind_by_api_name(RECORD_FILES[stem])
        assert kind is not None
        records: list[RecordUpsert] = []
        for row in tables[stem]:
            values = {}
            for column, raw in row.items():
                if column == "key":
                    continue
                attribute = kind.attribute_by_api_name(column)
                if attribute is None:
                    continue
                value = _typed(attribute, raw)
                if value is not None:
                    values[attribute.id] = value
            key = row["key"] if "key" in row else row[kind.attributes[kind.primary_key[0]].api_name]
            records.append(RecordUpsert(record=Record(kind=kind.id, key=key, values=values)))
        links_by_from: dict[str, list[LinkUpsert]] = {}
        for link_stem in link_stems:
            found = schema.relation_by_api_name(link_stem)
            assert found is not None
            relation = found[0]
            for row in tables[link_stem]:
                values = {}
                for column, raw in row.items():
                    if column in ("from_key", "to_key"):
                        continue
                    attribute = next(
                        a for a in relation.attributes.values() if a.api_name == column
                    )
                    value = _typed(attribute, raw)
                    if value is not None:
                        values[attribute.id] = value
                link = Link.model_validate(
                    {
                        "relation": relation.id,
                        "from": row["from_key"],
                        "to": row["to_key"],
                        "values": values,
                    }
                )
                links_by_from.setdefault(link.from_key, []).append(LinkUpsert(link=link))
        batch: list[DataChange] = []
        for change in records:
            batch.append(change)
            batch.extend(links_by_from.pop(change.record.key, []))
            if len(batch) >= scale.commit_batch:
                batches.append(batch)
                batch = []
        if batch:
            batches.append(batch)
    return batches


def _densify(tables: dict[str, list[dict[str, str]]], scale: Scale, rng: random.Random) -> None:
    """`extra_links` more suppliers and plants per Part, distinct from the existing ones."""
    if scale.extra_links < 1:
        return
    suppliers = [row["supplier_code"] for row in tables["supplier"]]
    plants = [row["plant_code"] for row in tables["plant"]]
    for stem, targets, template in (
        ("supplied_by", suppliers, {"is_primary": "false", "lead_time_days": "30"}),
        ("stocked_at", plants, {"quantity": "10"}),
    ):
        existing: dict[str, set[str]] = {}
        columns = list(tables[stem][0])
        for row in tables[stem]:
            existing.setdefault(row["from_key"], set()).add(row["to_key"])
        for part in tables["part"]:
            number = part["part_number"]
            taken = existing.setdefault(number, set())
            wanted = min(scale.extra_links, len(targets) - len(taken))
            while wanted > 0:
                target = rng.choice(targets)
                if target in taken:
                    continue
                taken.add(target)
                row = {"from_key": number, "to_key": target, **template}
                tables[stem].append({c: row.get(c, "") for c in columns})
                wanted -= 1


async def seed(engine: AsyncEngine, runtime: StoreRuntime, scale: Scale) -> SeedReport:
    """Create the Domain, its schema and the dataset on `main`; refresh statistics."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    schema = perf_schema()
    started = time.perf_counter()
    async with sessions() as session:
        await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime)
    elements = [*schema.kinds.values(), *schema.relations.values(), *schema.traits.values()]
    elements.extend(schema.sources.values())
    async with sessions() as session:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            schema_changes=[SchemaUpsert(e) for e in elements],
            author=AUTHOR,
            message="schema",
            origin=ORIGIN,
            runtime=runtime,
        )
    batches = build_changes(schema, scale)
    for batch in batches:
        async with sessions() as session:
            await commit(
                session,
                DOMAIN,
                "main",
                batch,
                author=AUTHOR,
                message="seed",
                origin=ORIGIN,
                runtime=runtime,
            )
    await run_index_jobs(engine, runtime.settings.indexes)
    await _analyze(engine)
    seconds = time.perf_counter() - started
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        records = (
            await session.execute(
                text("SELECT count(*) FROM mat_records WHERE domain_id = :d AND branch = 'main'"),
                {"d": DOMAIN},
            )
        ).scalar_one()
        links = (
            await session.execute(
                text("SELECT count(*) FROM mat_links WHERE domain_id = :d AND branch = 'main'"),
                {"d": DOMAIN},
            )
        ).scalar_one()
    return SeedReport(int(records), int(links), seconds, len(batches) + 1)


async def _analyze(engine: AsyncEngine) -> None:
    """Without parallel index vacuuming, whose shared memory exceeds a container's default
    `/dev/shm` once the indexes of several branches are large."""
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(
            "VACUUM (PARALLEL 0, ANALYZE) mat_records, mat_links, refs, mat_schema"
        )


async def create_reader(engine: AsyncEngine) -> None:
    """A role subject to row-level security, as the application role is in production, with
    EXECUTE on the security definer read functions."""
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        exists = (
            await auto.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": READER_ROLE}
            )
        ).first()
        if exists is None:
            await auto.exec_driver_sql(f"CREATE ROLE {READER_ROLE} NOLOGIN")
        await auto.exec_driver_sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READER_ROLE}")
        await grant_read_functions(auto, READER_ROLE)


async def drop_reader(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"DROP OWNED BY {READER_ROLE}")
        await auto.exec_driver_sql(f"DROP ROLE IF EXISTS {READER_ROLE}")


# Measurements -----------------------------------------------------------------------------------

Operation = Callable[[AsyncSession, random.Random], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Workload:
    """Keys and values sampled from the seeded data."""

    schema: Schema
    parts: list[str]
    suppliers: list[str]
    words: list[str]
    supplier_kind: str
    part_kind: str


async def workload(engine: AsyncEngine, runtime: StoreRuntime) -> Workload:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        found = await head_schema(session, DOMAIN, "main", runtime=runtime)
        assert found is not None
        schema = found.schema
        part = schema.kind_by_api_name("Part")
        supplier = schema.kind_by_api_name("Supplier")
        assert part is not None
        assert supplier is not None
        keys = text(
            "SELECT key FROM mat_records WHERE domain_id = :d AND branch = 'main' AND kind_id = :k"
        )
        parts = list((await session.execute(keys, {"d": DOMAIN, "k": part.id})).scalars())
        suppliers = list((await session.execute(keys, {"d": DOMAIN, "k": supplier.id})).scalars())
        titles = text(
            "SELECT title FROM mat_records WHERE domain_id = :d AND branch = 'main' "
            "AND title IS NOT NULL ORDER BY key LIMIT 2000"
        )
        words = sorted(
            {
                word.lower()
                for title in (await session.execute(titles, {"d": DOMAIN})).scalars()
                for word in title.split()
                if word.isalpha() and len(word) > 3
            }
        )
    return Workload(schema, parts, suppliers, words, supplier.id, part.id)


def read_operations(
    work: Workload, runtime: StoreRuntime, branch: str | BranchLayout = "main"
) -> dict[str, Operation]:
    """The timed reads on `branch`; a layout, as Keeper passes it, spares the ref lookup."""
    schema = work.schema
    supplier = schema.kinds[work.supplier_kind]
    risk = next(a for a in supplier.attributes.values() if a.api_name == "risk_score")
    risk_index = index_spec(schema, work.supplier_kind, risk.id)
    assert risk_index is not None

    async def get(session: AsyncSession, rng: random.Random) -> Any:
        return await get_record(session, DOMAIN, branch, work.part_kind, rng.choice(work.parts))

    async def page(session: AsyncSession, rng: random.Random) -> Any:
        return await list_records(
            session,
            DOMAIN,
            branch,
            work.part_kind,
            limit=50,
            after=rng.choice(work.parts),
            runtime=runtime,
        )

    async def links(session: AsyncSession, rng: random.Random) -> Any:
        return await links_of_record(
            session,
            DOMAIN,
            branch,
            schema,
            work.supplier_kind,
            rng.choice(work.suppliers),
            limit=50,
            runtime=runtime,
        )

    async def hood(session: AsyncSession, rng: random.Random) -> Any:
        return await neighborhood(
            session,
            DOMAIN,
            branch,
            schema,
            [(work.part_kind, rng.choice(work.parts))],
            depth=2,
            runtime=runtime,
        )

    async def by_attribute(session: AsyncSession, rng: random.Random) -> Any:
        return await query_by_attribute(
            session,
            DOMAIN,
            branch,
            risk_index,
            Comparison.EQ,
            str(rng.randrange(0, 100)),
            limit=50,
            runtime=runtime,
        )

    async def search(session: AsyncSession, rng: random.Random) -> Any:
        return await search_records(
            session, DOMAIN, branch, rng.choice(work.words), limit=20, runtime=runtime
        )

    async def search_titles(session: AsyncSession, rng: random.Random) -> Any:
        return await search_records(
            session,
            DOMAIN,
            branch,
            rng.choice(work.words),
            limit=20,
            titles=TITLE_SIMILARITY,
            runtime=runtime,
        )

    return {
        "get by key": get,
        "list page of 50": page,
        "links of a record": links,
        "2-hop neighborhood": hood,
        "query by indexed attribute": by_attribute,
        "full-text search": search,
        "full-text search with typo-tolerant titles": search_titles,
    }


async def measure_reads(
    engine: AsyncEngine,
    operations: dict[str, Operation],
    *,
    iterations: int,
    warmup: int,
    role: str | None,
    seed: int = 7,
) -> dict[str, Measurement]:
    """p50/p95 of each operation; one transaction per call, like one API request."""
    rng = random.Random(seed)
    results: dict[str, Measurement] = {}
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        for name, operation in operations.items():
            measurement = Measurement(name)
            for index in range(warmup + iterations):
                started = time.perf_counter()
                async with session.begin():
                    if role is not None:
                        await session.execute(text(f"SET LOCAL ROLE {role}"))
                    await scope_to_domain(session, DOMAIN)
                    await operation(session, rng)
                if index >= warmup:
                    measurement.samples_ms.append((time.perf_counter() - started) * 1000)
            results[name] = measurement
    return results


async def measure_branch_copy(
    engine: AsyncEngine, runtime: StoreRuntime, *, iterations: int
) -> Measurement:
    """Create a fully materialized branch from `main` (copy), then delete it."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    measurement = Measurement("branch creation (copy)")
    for index in range(iterations):
        name = f"perf-{index}"
        started = time.perf_counter()
        async with sessions() as session:
            await create_branch(session, DOMAIN, name, created_by="usr_perf", runtime=runtime)
        measurement.samples_ms.append((time.perf_counter() - started) * 1000)
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await RefRepository(session, DOMAIN).delete(name)
            await BranchIndexes(session, DOMAIN, name, settings=runtime.settings.indexes).drop_all()
        await run_index_jobs(engine, runtime.settings.indexes)
    return measurement


OVERLAY_BRANCH = "agent/perf"
COPY_BRANCH = "perf-copy"


async def measure_branch_overlay(
    engine: AsyncEngine, runtime: StoreRuntime, *, iterations: int
) -> Measurement:
    """Create an overlay branch of `main`, then delete it."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    measurement = Measurement("branch creation (overlay)")
    for index in range(iterations):
        name = f"agent/perf-{index}"
        started = time.perf_counter()
        async with sessions() as session:
            created = await create_branch(
                session,
                DOMAIN,
                name,
                created_by="usr_perf",
                mode=BranchMode.OVERLAY,
                runtime=runtime,
            )
        measurement.samples_ms.append((time.perf_counter() - started) * 1000)
        assert created.mode is MaterializationMode.OVERLAY
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            await RefRepository(session, DOMAIN).delete(name)
    return measurement


@dataclass(frozen=True, slots=True)
class OverlayPair:
    """An overlay and a full copy of `main` with the same commits, `main` moved since."""

    overlay: BranchLayout
    copy: BranchLayout
    overlay_rows: int
    copy_rows: int


async def overlay_pair(
    engine: AsyncEngine, runtime: StoreRuntime, work: Workload, *, changes: int
) -> OverlayPair:
    """Fork an overlay and a copy from `main`; commit `changes` Part updates to both, then
    `changes` other Part updates to `main` (copied into the overlay before they land)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    for name, mode in ((OVERLAY_BRANCH, BranchMode.OVERLAY), (COPY_BRANCH, BranchMode.COPY)):
        async with sessions() as session:
            await create_branch(
                session, DOMAIN, name, created_by="usr_perf", mode=mode, runtime=runtime
            )
    part = work.schema.kinds[work.part_kind]
    cost = next(a for a in part.attributes.values() if a.api_name == "unit_cost")
    rng = random.Random(17)
    keys = rng.sample(work.parts, 2 * changes)
    plan = ((keys[:changes], (OVERLAY_BRANCH, COPY_BRANCH)), (keys[changes:], ("main",)))
    for round_keys, branches in plan:
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            rows = await _records(session, work.part_kind, round_keys)
        updates = [
            RecordUpsert(
                record=Record(
                    kind=work.part_kind,
                    key=key,
                    values={**rows[key], cost.id: f"7.{rng.randrange(10, 99)}"},
                )
            )
            for key in round_keys
        ]
        for branch in branches:
            async with sessions() as session:
                await commit(
                    session,
                    DOMAIN,
                    branch,
                    updates,
                    author=AUTHOR,
                    message="perf overlay",
                    origin=ORIGIN,
                    runtime=runtime,
                )
    await run_index_jobs(engine, runtime.settings.indexes)
    await _analyze(engine)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        overlay = await load_layout(session, DOMAIN, OVERLAY_BRANCH)
        copy = await load_layout(session, DOMAIN, COPY_BRANCH)
        own = text(
            "SELECT (SELECT count(*) FROM mat_records WHERE domain_id = :d AND branch = :b) "
            "+ (SELECT count(*) FROM mat_links WHERE domain_id = :d AND branch = :b)"
        )
        overlay_rows = (await session.execute(own, {"d": DOMAIN, "b": OVERLAY_BRANCH})).scalar()
        copy_rows = (await session.execute(own, {"d": DOMAIN, "b": COPY_BRANCH})).scalar()
    assert overlay.overlay, "the overlay must not have been promoted"
    return OverlayPair(overlay, copy, int(overlay_rows or 0), int(copy_rows or 0))


async def measure_commits(
    engine: AsyncEngine, runtime: StoreRuntime, work: Workload, *, changes: int, iterations: int
) -> Measurement:
    """Commit `changes` Part updates (one Attribute each) as one Revision."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    part = work.schema.kinds[work.part_kind]
    cost = next(a for a in part.attributes.values() if a.api_name == "unit_cost")
    measurement = Measurement(f"commit of {changes} changes")
    rng = random.Random(11)
    for index in range(iterations):
        keys = rng.sample(work.parts, changes)
        async with sessions() as session, session.begin():
            await scope_to_domain(session, DOMAIN)
            rows = await _records(session, work.part_kind, keys)
        updates = [
            RecordUpsert(
                record=Record(
                    kind=work.part_kind,
                    key=key,
                    values={**rows[key], cost.id: f"{index + 1}.{rng.randrange(10, 99)}"},
                )
            )
            for key in keys
        ]
        started = time.perf_counter()
        async with sessions() as session:
            await commit(
                session,
                DOMAIN,
                "main",
                updates,
                author=AUTHOR,
                message="perf update",
                origin=ORIGIN,
                runtime=runtime,
            )
        measurement.samples_ms.append((time.perf_counter() - started) * 1000)
    return measurement


async def _records(session: AsyncSession, kind_id: str, keys: Sequence[str]) -> dict[str, Any]:
    result = await session.execute(
        text(
            'SELECT key, "values"::text FROM mat_records WHERE domain_id = :d AND branch = '
            "'main' AND kind_id = :k AND key = ANY(CAST(:keys AS text[]))"
        ),
        {"d": DOMAIN, "k": kind_id, "keys": list(keys)},
    )
    return {key: json.loads(values) for key, values in result}


# Report -----------------------------------------------------------------------------------------


def hardware() -> dict[str, str]:
    notes = {
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
    }
    if platform.system() == "Darwin":
        for key, name in (
            ("machdep.cpu.brand_string", "cpu"),
            ("hw.ncpu", "cores"),
            ("hw.memsize", "memory"),
        ):
            try:
                value = subprocess.run(
                    ["/usr/sbin/sysctl", "-n", key], capture_output=True, text=True, check=True
                ).stdout.strip()
            except (OSError, subprocess.CalledProcessError):
                continue
            notes[name] = f"{int(value) // 2**30} GiB" if name == "memory" else value
    return notes


async def server_notes(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as connection:
        version = (await connection.execute(text("SHOW server_version"))).scalar_one()
        buffers = (await connection.execute(text("SHOW shared_buffers"))).scalar_one()
        memory = (await connection.execute(text("SHOW work_mem"))).scalar_one()
    return {"postgres": str(version), "shared_buffers": str(buffers), "work_mem": str(memory)}
