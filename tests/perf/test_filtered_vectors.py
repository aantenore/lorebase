"""Filtered semantic search on a large embeddings table (`pytest tests/perf/test_filtered_vectors.py
-m perf`; skipped otherwise; design 8.8, docs/performance.md#filtered-semantic-search).

Method: a scratch database (migrated, the example schema with an embedding template on every
Kind) gets `RECORDS` Records of six Kinds, each with a synthetic unit vector of the configured
dimension, drawn around `CLUSTERS` centres with a fixed seed (`setseed`) so that neighbourhoods
exist, and so that the Kind of a Record has nothing to do with where its vector lies. The HNSW
index is built after the load, with the migration's definition. Searches go through the real
Keeper application (`POST /search`, `mode: semantic`) as a role subject to row-level security,
filtered to the smallest Kind (1% of the rows). Ground truth is the exact cosine top-k over the
rows of that Kind, by a sequential scan with the index scans off.

Asserted: the plan reads the HNSW index; with iterative scans the list keeps its limit and
matches the exact neighbours (recall@k) within a latency budget; the over-fetch path
fills the list for a one-Kind filter, which restricts the vectors before its limit. The
measurements are written to `build/perf/filtered-vectors.md` (override with `LOREBASE_PERF_FILTERED_VECTORS_OUT`).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import statistics
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.elements import ClauseElement

from lorebase_core.model import Schema
from lorebase_keeper.app import create_app
from lorebase_keeper.reading import search as search_module
from lorebase_platform.settings import Settings
from lorebase_store import migrations, testing
from lorebase_store.db import create_engine
from lorebase_store.embeddings import iterative_scans, template_fingerprints
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.storage import hnsw_index_name, hnsw_index_sql

from . import harness

pytestmark = pytest.mark.perf

RECORDS = int(os.environ.get("LOREBASE_PERF_VECTOR_RECORDS", "100000"))
"""Records (and vectors) loaded."""
CLUSTERS = 400
"""Centres the vectors are drawn around."""
SPREAD = 0.1
"""Width of the uniform noise added to each component of a unit centre (a vector lies at a
cosine of about 0.85 from its centre)."""
KIND_SHARES = {
    "Supplier": 0.40,
    "Part": 0.30,
    "Plant": 0.15,
    "PurchaseOrder": 0.10,
    "Contract": 0.04,
    "RiskFlag": 0.01,
}
FILTERED_KIND = "RiskFlag"
"""The Kind the filtered searches keep: 1% of the rows."""
QUERIES = 40
LIMIT = 20
"""Results asked for, as in the budget of design 8.7."""
MODEL = "BAAI/bge-small-en-v1.5"
DIMS = StoreSettings().embeddings.dims
ROLE = "lorebase_perf_vectors"
ROLE_PASSWORD = "perf-vectors-password"
BASE = f"/api/v1/domains/{harness.DOMAIN}"

MIN_RECALL = 0.90
"""Mean recall@LIMIT of the iterative scan against the exact neighbours of the filter."""
BUDGET_P95_MS = 200.0
"""Filtered semantic search, whole request (planning, scan, final page), p95, at the default
100,000 vectors; design 8.7 holds it to the same figure. The statement is exact today (see
`test_iterative_scan_reads_the_hnsw_index`), so the cost grows with the table: 300,000
vectors miss this budget (docs/performance.md#filtered-semantic-search)."""
OUT = Path(os.environ.get("LOREBASE_PERF_FILTERED_VECTORS_OUT", "build/perf/filtered-vectors.md"))

#: Cluster centres (random unit vectors).
CENTRES_SQL = """
CREATE TABLE perf_centres AS
SELECT c, l2_normalize(CAST(array_agg(random() - 0.5 ORDER BY d) AS real[])::vector({dims})) AS v
FROM generate_series(0, {clusters} - 1) c CROSS JOIN generate_series(1, {dims}) d GROUP BY c
"""
#: Records: the Kind of Record `i` comes from `i % 100`, its cluster from `hashint8(i)`.
RECORDS_SQL = """
INSERT INTO mat_records (domain_id, branch, kind_id, key, "values", title, object_hash, lineage,
                         updated_revision, updated_at)
SELECT :d, 'main', k.id, k.prefix || '-' || i, '{{}}', k.prefix || '-' || i,
       sha256(convert_to('object-' || i, 'UTF8')), '{{}}', sha256(convert_to('revision', 'UTF8')),
       now()
FROM generate_series(0, :n - 1) i
JOIN (VALUES {kinds}) AS k(id, prefix, low, high) ON i % 100 >= k.low AND i % 100 < k.high
"""
EMBEDDINGS_SQL = """
INSERT INTO embeddings (domain_id, object_hash, model, fingerprint, dims, embedding)
SELECT :d, sha256(convert_to('object-' || i, 'UTF8')), :m, f.fingerprint, {dims},
       l2_normalize(ctr.v + noise.v)
FROM generate_series(0, :n - 1) i
JOIN (VALUES {fingerprints}) AS f(low, high, fingerprint) ON i % 100 >= f.low AND i % 100 < f.high
JOIN perf_centres ctr ON ctr.c = abs(hashint8(i)) % {clusters}
CROSS JOIN LATERAL (
    SELECT CAST(array_agg((random() - 0.5) * {spread}) AS real[])::vector({dims}) AS v
    FROM generate_series(1, {dims}) n WHERE i >= 0) AS noise
"""
QUERIES_SQL = """
SELECT CAST(l2_normalize(ctr.v + noise.v) AS text)
FROM generate_series(1, {queries}) q
JOIN perf_centres ctr ON ctr.c = abs(hashint8(q + 1000003)) % {clusters}
CROSS JOIN LATERAL (
    SELECT CAST(array_agg((random() - 0.5) * {spread}) AS real[])::vector({dims}) AS v
    FROM generate_series(1, {dims}) n WHERE q >= 0) AS noise
"""
EXACT_SQL = """
SELECT r.key FROM mat_records r
JOIN embeddings e ON e.domain_id = r.domain_id AND e.object_hash = r.object_hash
WHERE r.domain_id = :d AND r.branch = 'main' AND e.dims = :dims
  AND (CAST(:kind AS text) IS NULL OR r.kind_id = :kind)
ORDER BY e.embedding <=> CAST(:q AS vector) LIMIT :k
"""

REPORT: list[str] = []
FACTS: dict[str, Any] = {}


@dataclass
class Dataset:
    owner_url: str
    app_url: str
    queries: list[list[float]]
    ids: dict[str, str]
    """Kind api name to Kind id."""
    kind_rows: dict[str, int]
    iterative: bool
    version: str


@dataclass
class Run:
    label: str
    items: list[int] = field(default_factory=list)
    recalls: list[float] = field(default_factory=list)
    truncated: list[bool] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)

    def p95(self) -> float:
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]

    def row(self) -> str:
        return (
            f"| {self.label} | {statistics.mean(self.items):.1f} | {min(self.items)} "
            f"| {statistics.mean(self.recalls):.3f} | {min(self.recalls):.2f} "
            f"| {sum(self.truncated)}/{len(self.truncated)} "
            f"| {statistics.median(self.latencies_ms):.1f} | {self.p95():.1f} |"
        )


# Dataset ----------------------------------------------------------------------------------------


def embedded(schema: Schema) -> Schema:
    """`schema` with an embedding template on every Kind (its first Attribute not sensitive)."""
    kinds = {}
    for kind in schema.kinds.values():
        attribute = next(a for a in kind.attributes.values() if not a.is_sensitive)
        search = kind.search.model_copy(update={"embedding_template": f"{{{attribute.api_name}}}"})
        kinds[kind.id] = kind.model_copy(update={"search": search})
    return schema.model_copy(update={"kinds": kinds})


async def execute(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def load(owner_url: str, schema: Schema) -> list[list[float]]:
    """Records and vectors by set-based SQL, then the HNSW index. Returns the query vectors."""
    fingerprints = template_fingerprints(schema.kinds.values())
    ids = {k.api_name: k.id for k in schema.kinds.values()}
    edges = [0, *itertools.accumulate(round(share * 100) for share in KIND_SHARES.values())]
    bands = [
        (ids[name], name[:2].upper(), low, high)
        for name, low, high in zip(KIND_SHARES, edges, edges[1:], strict=False)
    ]
    kinds = ", ".join(f"('{i}', '{prefix}', {low}, {high})" for i, prefix, low, high in bands)
    prints = ", ".join(f"({low}, {high}, '{fingerprints[i]}')" for i, _, low, high in bands)
    shape = {"dims": DIMS, "clusters": CLUSTERS, "spread": SPREAD}
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await auto.exec_driver_sql(f"DROP INDEX IF EXISTS {hnsw_index_name(DIMS, 'full')}")
            await auto.exec_driver_sql("SELECT setseed(0.20261030)")
            await auto.exec_driver_sql(CENTRES_SQL.format(**shape))
            started = time.perf_counter()
            params = {"d": harness.DOMAIN, "n": RECORDS, "m": MODEL}
            await auto.execute(text(RECORDS_SQL.format(kinds=kinds)), params)
            await auto.execute(text(EMBEDDINGS_SQL.format(fingerprints=prints, **shape)), params)
            FACTS["load_s"] = time.perf_counter() - started
            await auto.exec_driver_sql("SET maintenance_work_mem = '1GB'")
            await auto.exec_driver_sql("SET max_parallel_maintenance_workers = 0")
            started = time.perf_counter()
            await auto.exec_driver_sql(hnsw_index_sql(DIMS, "full", concurrently=False))
            FACTS["index_build_s"] = time.perf_counter() - started
            await auto.exec_driver_sql("ANALYZE mat_records")
            await auto.exec_driver_sql("ANALYZE embeddings")
            size = (
                await auto.execute(
                    text("SELECT pg_relation_size(CAST(:i AS regclass))"),
                    {"i": hnsw_index_name(DIMS, "full")},
                )
            ).scalar_one()
            FACTS["index_mb"] = size / 2**20
            rows = await auto.exec_driver_sql(QUERIES_SQL.format(queries=QUERIES, **shape))
            return [json.loads(vector) for (vector,) in rows]
    finally:
        await engine.dispose()


async def seed(owner_url: str, app_url: str) -> Dataset:
    schema = embedded(harness.perf_schema())
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        await harness._create_schema(sessions, schema, runtime)
        queries = await load(owner_url, schema)
        async with sessions() as session:
            iterative = await iterative_scans(session)
            version = (
                await session.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one()
            counts = (
                await session.execute(text("SELECT kind_id, count(*) FROM mat_records GROUP BY 1"))
            ).all()
    finally:
        await engine.dispose()
    return Dataset(
        owner_url,
        app_url,
        queries,
        {k.api_name: k.id for k in schema.kinds.values()},
        dict(counts),
        iterative,
        str(version),
    )


@pytest.fixture(scope="module")
def dataset() -> Iterator[Dataset]:
    with testing.postgres_server() as server, testing.scratch_database(server) as url:
        asyncio.run(execute(url, f"CREATE ROLE {ROLE} LOGIN PASSWORD '{ROLE_PASSWORD}'"))
        migrations.upgrade(
            database=DatabaseSettings(application_role=ROLE), store=StoreSettings(), url=url
        )
        asyncio.run(
            execute(
                url,
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}",
                f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {ROLE}",
            )
        )
        app_url = (
            make_url(url)
            .set(username=ROLE, password=ROLE_PASSWORD)
            .render_as_string(hide_password=False)
        )
        try:
            yield asyncio.run(seed(url, app_url))
        finally:
            asyncio.run(execute(url, f"DROP OWNED BY {ROLE}", f"DROP ROLE {ROLE}"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    head = (
        f"Filtered semantic search: {RECORDS:,} vectors of {DIMS} dimensions in {CLUSTERS} "
        f"clusters, six Kinds, {QUERIES} queries, limit {LIMIT}; HNSW index built in "
        f"{FACTS.get('index_build_s', 0):.0f} s, {FACTS.get('index_mb', 0):.0f} MB.",
        "",
        "| Search | Results (mean) | Fewest | Recall@20 (mean) | Lowest | semantic_truncated "
        "| p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|",
    )
    OUT.write_text("\n".join([*head, *REPORT]) + "\n", encoding="utf-8")


# Searches ---------------------------------------------------------------------------------------


@asynccontextmanager
async def keeper(app_url: str, **sections: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(keeper={}, database={"dsn": app_url}, **sections))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test", timeout=60) as http,
    ):
        yield http


class _ExplainAnalyze(Executable, ClauseElement):
    inherit_cache = False

    def __init__(self, statement: ClauseElement) -> None:
        self.statement = statement


@compiles(_ExplainAnalyze)
def _compile_explain(element: _ExplainAnalyze, compiler: SQLCompiler, **kw: Any) -> str:
    return "EXPLAIN (ANALYZE, FORMAT JSON) " + compiler.process(element.statement, **kw)


@contextmanager
def plans_of_search() -> Iterator[list[dict[str, Any]]]:
    """Plans (`EXPLAIN (ANALYZE, FORMAT JSON)`) of the statements the search compiler passes to
    `explain`, planned inside the read transaction, after its `SET LOCAL`s."""
    plans: list[dict[str, Any]] = []

    async def capture(session: AsyncSession, query: Any, domain_id: str, label: str) -> None:
        result = await session.execute(_ExplainAnalyze(query))
        document = result.scalar_one()
        document = json.loads(document) if isinstance(document, str) else document
        plans.append(document[0]["Plan"])

    with mock.patch.object(search_module, "explain", capture):
        yield plans


def nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", ()):
        yield from nodes(child)


def indexes_of(plan: dict[str, Any]) -> set[str]:
    return {n["Index Name"] for n in nodes(plan) if "Index Name" in n}


async def exact(owner_url: str, queries: list[list[float]], kind_id: str | None) -> list[list[str]]:
    """Keys of the exact top `LIMIT` by cosine distance among the rows of the Kind: a
    sequential scan, with index scans off."""
    engine = create_async_engine(owner_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            for setting in ("enable_indexscan", "enable_bitmapscan", "enable_indexonlyscan"):
                await connection.exec_driver_sql(f"SET {setting} = off")
            found = []
            for vector in queries:
                rows = await connection.execute(
                    text(EXACT_SQL),
                    {
                        "d": harness.DOMAIN,
                        "dims": DIMS,
                        "kind": kind_id,
                        "q": str(vector),
                        "k": LIMIT,
                    },
                )
                found.append([key for (key,) in rows])
            return found
    finally:
        await engine.dispose()


async def run_queries(
    data: Dataset, label: str, kind: str | None, **sections: Any
) -> tuple[Run, dict[str, Any]]:
    run = Run(label)
    body: dict[str, Any] = {"mode": "semantic", "limit": LIMIT}
    if kind is not None:
        body["kinds"] = [kind]
    truths = await exact(data.owner_url, data.queries, None if kind is None else data.ids[kind])
    async with keeper(data.app_url, **sections) as http:
        for warm in data.queries[:3]:
            await http.post(f"{BASE}/search", json={**body, "query_vector": warm})
        for query, truth in zip(data.queries, truths, strict=True):
            started = time.perf_counter()
            response = await http.post(f"{BASE}/search", json={**body, "query_vector": query})
            run.latencies_ms.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200, response.text
            found = response.json()
            keys = [hit["key"] for hit in found["items"]]
            run.items.append(len(keys))
            run.recalls.append(len(set(keys) & set(truth)) / len(truth))
            run.truncated.append(bool(found["semantic_truncated"]))
        with plans_of_search() as plans:
            await http.post(f"{BASE}/search", json={**body, "query_vector": data.queries[0]})
    REPORT.append(run.row())
    return run, plans[0] if plans else {}


# Tests ------------------------------------------------------------------------------------------


def test_the_filter_keeps_one_percent_of_the_rows(dataset: Dataset) -> None:
    filtered = dataset.kind_rows[dataset.ids[FILTERED_KIND]]
    assert filtered / RECORDS == pytest.approx(0.01, abs=0.001), dataset.kind_rows
    assert filtered >= LIMIT * 10


MEASURED: dict[str, tuple[Run, dict[str, Any]]] = {}


async def measured(
    data: Dataset, label: str, kind: str | None, **sections: Any
) -> tuple[Run, dict[str, Any]]:
    if label not in MEASURED:
        MEASURED[label] = await run_queries(data, label, kind, **sections)
    return MEASURED[label]


OVER_FETCH = {"search": {"semantic": {"iterative_scan": "off"}}}
HNSW = hnsw_index_name(DIMS, "full")


def shape(plan: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(n["Node Type"], n.get("Index Name")) for n in nodes(plan)]


@pytest.mark.parametrize(("label", "kind"), [("no filter", None), ("40% filter", "Supplier")])
async def test_iterative_scan_keeps_the_limit_of_a_broad_filter(
    dataset: Dataset, label: str, kind: str | None
) -> None:
    if not dataset.iterative:
        pytest.skip(f"pgvector {dataset.version} has no iterative scans")
    run, _ = await measured(dataset, f"{label}, iterative scan", kind)
    assert min(run.items) == LIMIT
    assert statistics.mean(run.recalls) >= MIN_RECALL, run.recalls
    assert run.p95() <= BUDGET_P95_MS, run.latencies_ms
    assert not any(run.truncated)


async def test_iterative_scan_keeps_the_limit_of_a_one_percent_filter(dataset: Dataset) -> None:
    if not dataset.iterative:
        pytest.skip(f"pgvector {dataset.version} has no iterative scans")
    run, _ = await measured(dataset, "1% filter, iterative scan", FILTERED_KIND)
    assert min(run.items) == LIMIT
    assert statistics.mean(run.recalls) >= MIN_RECALL, run.recalls
    assert run.p95() <= BUDGET_P95_MS, run.latencies_ms
    assert not any(run.truncated)


@pytest.mark.parametrize(("label", "kind"), [("no filter", None), ("40% filter", "Supplier")])
async def test_iterative_scan_reads_the_hnsw_index(
    dataset: Dataset, label: str, kind: str | None
) -> None:
    _, plan = await measured(dataset, f"{label}, iterative scan", kind)
    assert HNSW in indexes_of(plan), shape(plan)


async def test_over_fetch_of_a_one_percent_kind_is_exact(dataset: Dataset) -> None:
    # The search reads the vectors of the Kind's template fingerprint alone (a Kind has its
    # own fingerprint): for 1% of the table the planner reads those rows and sorts them, so
    # the list is complete whatever the over-fetch.
    run, plan = await measured(dataset, "1% filter, over-fetch x4", FILTERED_KIND, **OVER_FETCH)
    assert HNSW not in indexes_of(plan), shape(plan)
    assert min(run.items) == LIMIT
    assert statistics.mean(run.recalls) >= MIN_RECALL, run.recalls
    assert not any(run.truncated)


async def test_over_fetch_keeps_the_limit_of_a_broad_filter(dataset: Dataset) -> None:
    run, plan = await measured(dataset, "40% filter, over-fetch x4", "Supplier", **OVER_FETCH)
    assert HNSW in indexes_of(plan), shape(plan)
    assert min(run.items) == LIMIT
    assert statistics.mean(run.recalls) >= MIN_RECALL, run.recalls
    assert run.p95() <= BUDGET_P95_MS, run.latencies_ms
