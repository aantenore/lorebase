"""Recall loss of pgvector half precision (halfvec) against full precision (vector) on HNSW.

Method: embed the "supply" records of the perf seed with a real model (fastembed, not a project
dependency), load them into a scratch database as a dimensionless ``vector`` column (like the
real ``embeddings`` table) and build two HNSW indexes with default parameters: the one Lorebase
uses (``embedding::vector(384)``) and a half precision expression index
(``embedding::halfvec(384)``). Ground truth is the exact cosine top-k computed with numpy over all
corpus embeddings. Reported: recall@k of exact/HNSW full and half precision at several
``hnsw.ef_search`` values, index sizes, build times and warm per-query latency.

Usage (fastembed from the uv cache, no network):
    uv run --offline --with fastembed python tests/perf/halfvec_recall.py \
        [--records 30000] [--queries 300] [--k 10] [--keep] [--out build/perf/halfvec-recall.md]

Only the scratch database is written (created and dropped); the seed database is read only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

DEFAULT_SERVER = "postgresql+asyncpg://perf:perf@127.0.0.1:55410"
DEFAULT_SOURCE_DB = "perf_seed_1m_zipf"
SCRATCH_DB = "ws_vectors"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384
TEXT_CAP = 512
EF_SEARCH_VALUES = (40, 100)
QUERY_SEED = 20240607
LOAD_BATCH = 1000

SOURCE_SQL = text(
    """
    SELECT kind_id, key, title, "values" FROM mat_records
    WHERE domain_id = 'supply' AND branch = 'main' AND NOT deleted
    ORDER BY key LIMIT :n
    """
)

FULL_ORDER = f"embedding::vector({DIMS}) <=> CAST(:q AS vector({DIMS}))"
HALF_ORDER = f"embedding::halfvec({DIMS}) <=> CAST(:q AS halfvec({DIMS}))"
VERSION_SQL = "SELECT extversion FROM pg_extension WHERE extname = 'vector'"


def _select(order: str) -> Any:
    return text(f"SELECT id FROM v ORDER BY {order} LIMIT :k")


@dataclass
class Variant:
    """One measured search variant: its SQL and the per-query results."""

    name: str
    sql: Any
    index: bool
    ef_search: int | None = None
    recalls: list[float] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)


def string_values(value: Any) -> list[str]:
    """Collect every string found in a JSON value, depth first."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in string_values(item)]
    if isinstance(value, list):
        return [s for item in value for s in string_values(item)]
    return []


def record_text(title: str, values: Any) -> str:
    """Title plus every string value joined with '. ', capped at TEXT_CAP characters."""
    if isinstance(values, str):
        values = json.loads(values)
    parts = [title or "", *string_values(values or {})]
    return ". ".join(p for p in parts if p)[:TEXT_CAP]


async def fetch_titles_and_texts(url: str, limit: int) -> tuple[list[str], list[str]]:
    """Read titles and embedding texts of the supply records (read only)."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(SOURCE_SQL, {"n": limit})).all()
    finally:
        await engine.dispose()
    titles = [r.title or "" for r in rows]
    texts = [record_text(r.title or "", r._mapping["values"]) for r in rows]
    return titles, texts


def embed(texts: Sequence[str]) -> np.ndarray:
    """Embed texts with fastembed (imported lazily: not a project dependency)."""
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise SystemExit(
            "fastembed is required: uv run --offline --with fastembed python "
            "tests/perf/halfvec_recall.py"
        ) from exc
    model = TextEmbedding(model_name=MODEL_NAME)
    arr = np.asarray(list(model.embed(list(texts), batch_size=256)), dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.maximum(norms, 1e-12)


def misspell(word: str, rng: random.Random) -> str:
    """Replace one character of the word with a different letter."""
    pos = rng.randrange(len(word))
    alphabet = [c for c in "abcdefghijklmnopqrstuvwxyz" if c != word[pos].lower()]
    return word[:pos] + rng.choice(alphabet) + word[pos + 1 :]


def build_queries(titles: Sequence[str], count: int, seed: int = QUERY_SEED) -> list[str]:
    """Mixed workload: single words, cross-title phrases, misspelled words, shortened titles."""
    rng = random.Random(seed)
    words = sorted({w for t in titles for w in t.split() if len(w) > 3 and w.isalpha()})
    multi = [t for t in titles if len(t.split()) > 1]
    if not words or not multi:
        raise SystemExit("not enough title words to build the query workload")

    def single() -> str:
        return rng.choice(words)

    def phrase() -> str:
        picks = [p for p in rng.sample(titles, rng.choice((2, 3))) if p.split()]
        return " ".join(rng.choice(p.split()) for p in picks)

    def typo() -> str:
        return misspell(rng.choice(words), rng)

    def dropped() -> str:
        parts = rng.choice(multi).split()
        parts.pop(rng.randrange(len(parts)))
        return " ".join(parts)

    makers = (single, phrase, typo, dropped)
    return [makers[i % len(makers)]() for i in range(count)]


def vector_literal(vec: np.ndarray) -> str:
    """Text form accepted by CAST(... AS vector/halfvec)."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


async def execute_scalar(conn: AsyncConnection, sql: str) -> Any:
    return (await conn.execute(text(sql))).scalar_one()


async def admin_statement(server: str, sql: str) -> None:
    """Run one statement on the maintenance database, outside a transaction."""
    admin = create_async_engine(f"{server}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await admin.dispose()


async def load_vectors(conn: AsyncConnection, vectors: np.ndarray) -> None:
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await conn.execute(text("CREATE TABLE v (id int PRIMARY KEY, embedding vector)"))
    insert = text("INSERT INTO v (id, embedding) VALUES (:id, CAST(:e AS vector))")
    for start in range(0, len(vectors), LOAD_BATCH):
        batch = [
            {"id": i, "e": vector_literal(vectors[i])}
            for i in range(start, min(start + LOAD_BATCH, len(vectors)))
        ]
        await conn.execute(insert, batch)
    await conn.execute(text("ANALYZE v"))
    await conn.commit()


async def build_index(conn: AsyncConnection, name: str, expr: str, ops: str) -> float:
    """Create an HNSW index with default parameters; return the build time in seconds."""
    start = time.perf_counter()
    await conn.execute(text(f"CREATE INDEX {name} ON v USING hnsw (({expr}) {ops})"))
    await conn.commit()
    return time.perf_counter() - start


async def set_session(conn: AsyncConnection, indexscan: bool, ef_search: int) -> None:
    await conn.execute(text(f"SET enable_indexscan = {'on' if indexscan else 'off'}"))
    await conn.execute(text(f"SET hnsw.ef_search = {ef_search}"))


async def explain(conn: AsyncConnection, sql: Any, params: dict[str, Any]) -> str:
    plan = await conn.execute(text("EXPLAIN " + str(sql)), params)
    return re.sub(r"'\[[^\]]*\]'", "'[...]'", "\n".join(row[0] for row in plan))


async def run_variant(
    conn: AsyncConnection,
    variant: Variant,
    literals: Sequence[str],
    truth: Sequence[set[int]],
    k: int,
) -> None:
    await set_session(conn, variant.index, variant.ef_search or EF_SEARCH_VALUES[0])
    for lit, expected in zip(literals, truth, strict=True):
        params = {"q": lit, "k": k}
        if variant.index:  # warm run, not measured
            await conn.execute(variant.sql, params)
        start = time.perf_counter()
        ids = {row[0] for row in await conn.execute(variant.sql, params)}
        variant.latencies_ms.append((time.perf_counter() - start) * 1000)
        variant.recalls.append(len(ids & expected) / len(expected))


def percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[round(pct / 100 * (len(ordered) - 1))]


def verdict(variants: list[Variant], k: int) -> str:
    """One sentence comparing mean recall of half and full HNSW at each ef_search."""
    mean = {(v.name, v.ef_search): statistics.fmean(v.recalls) for v in variants}
    parts = []
    for ef in EF_SEARCH_VALUES:
        full, half = mean[("HNSW full", ef)], mean[("HNSW half", ef)]
        parts.append(f"ef_search={ef}: {half:.4f} vs {full:.4f} ({(half - full) * 100:+.2f} pt)")
    quant = mean[("exact full", None)] - mean[("exact half", None)]
    return (
        f"Mean recall@{k} of half precision HNSW against full precision HNSW is "
        + "; ".join(parts)
        + f"; quantization alone costs {quant * 100:.2f} pt of exact recall."
    )


def render(
    args: argparse.Namespace,
    version: str,
    embed_seconds: float,
    sizes: dict[str, int],
    builds: dict[str, float],
    variants: list[Variant],
    plans: dict[str, str],
) -> str:
    rows = args.records
    load1, load5, load15 = os.getloadavg()
    lines = [
        "# halfvec recall",
        "",
        f"Setup: {rows} records, {args.queries} queries, k={args.k}, model {MODEL_NAME} "
        f"({DIMS} dims, embedded in {embed_seconds:.1f}s), pgvector {version}, "
        f"{date.today().isoformat()}, load average {load1:.2f} {load5:.2f} {load15:.2f}.",
        "",
        "## Sizes",
        "",
        "| Relation | MB | Bytes per vector | Build time (s) |",
        "|---|---:|---:|---:|",
    ]
    for label, key in (
        ("table (vector column)", "table"),
        ("HNSW full (vector)", "full"),
        ("HNSW half (halfvec)", "half"),
    ):
        build = f"{builds[key]:.1f}" if key in builds else "-"
        lines.append(f"| {label} | {sizes[key] / 1e6:.1f} | {sizes[key] / rows:.0f} | {build} |")
    lines += [
        "",
        "## Recall",
        "",
        f"| Variant | ef_search | Mean recall@{args.k} | Min recall | Queries with recall 1.0 |",
        "|---|---:|---:|---:|---:|",
    ]
    for v in variants:
        perfect = 100 * sum(r >= 1.0 for r in v.recalls) / len(v.recalls)
        ef = str(v.ef_search) if v.ef_search else "-"
        lines.append(
            f"| {v.name} | {ef} | {statistics.fmean(v.recalls):.4f} | {min(v.recalls):.2f} "
            f"| {perfect:.1f}% |"
        )
    lines += [
        "",
        "## Latency (warm, ms per query)",
        "",
        "| Variant | ef_search | p50 | p95 |",
        "|---|---:|---:|---:|",
    ]
    for v in variants:
        if v.index:
            lines.append(
                f"| {v.name} | {v.ef_search} | {percentile(v.latencies_ms, 50):.2f} "
                f"| {percentile(v.latencies_ms, 95):.2f} |"
            )
    lines += ["", "## Plans", ""]
    for label, plan in plans.items():
        lines += [f"{label}:", "", "```", plan, "```", ""]
    lines += ["## Verdict", "", verdict(variants, args.k)]
    return "\n".join(lines) + "\n"


async def measure(
    conn: AsyncConnection,
    corpus: np.ndarray,
    literals: Sequence[str],
    truth: Sequence[set[int]],
    k: int,
) -> tuple[str, dict[str, int], dict[str, float], list[Variant], dict[str, str]]:
    """Load the corpus, build both indexes and run every variant."""
    await load_vectors(conn, corpus)
    version = await execute_scalar(conn, VERSION_SQL)
    builds = {
        "full": await build_index(
            conn, "v_full", f"embedding::vector({DIMS})", "vector_cosine_ops"
        ),
        "half": await build_index(
            conn, "v_half", f"embedding::halfvec({DIMS})", "halfvec_cosine_ops"
        ),
    }
    sizes = {
        "table": await execute_scalar(conn, "SELECT pg_relation_size('v')"),
        "full": await execute_scalar(conn, "SELECT pg_relation_size('v_full')"),
        "half": await execute_scalar(conn, "SELECT pg_relation_size('v_half')"),
    }
    select_full, select_half = _select(FULL_ORDER), _select(HALF_ORDER)
    variants = [
        Variant("exact full", select_full, index=False),
        Variant("exact half", select_half, index=False),
    ]
    for ef in EF_SEARCH_VALUES:
        variants.append(Variant("HNSW full", select_full, index=True, ef_search=ef))
        variants.append(Variant("HNSW half", select_half, index=True, ef_search=ef))

    await set_session(conn, True, EF_SEARCH_VALUES[0])
    sample = {"q": literals[0], "k": k}
    plans = {
        "HNSW full": await explain(conn, select_full, sample),
        "HNSW half": await explain(conn, select_half, sample),
    }
    for label, index in (("HNSW full", "v_full"), ("HNSW half", "v_half")):
        if index not in plans[label]:
            raise SystemExit(f"{label} does not use {index}:\n{plans[label]}")
    for variant in variants:
        await run_variant(conn, variant, literals, truth, k)
    return version, sizes, builds, variants, plans


async def main_async(args: argparse.Namespace) -> str:
    server = args.server.rstrip("/")
    titles, texts = await fetch_titles_and_texts(f"{server}/{args.source_db}", args.records)
    args.records = len(texts)
    queries = build_queries(titles, args.queries)

    start = time.perf_counter()
    corpus = embed(texts)
    query_vecs = embed(queries)
    embed_seconds = time.perf_counter() - start

    top = np.argsort(-(query_vecs @ corpus.T), axis=1)[:, : args.k]
    truth = [set(map(int, row)) for row in top]
    literals = [vector_literal(q) for q in query_vecs]

    await admin_statement(server, f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    await admin_statement(server, f"CREATE DATABASE {SCRATCH_DB}")
    engine = create_async_engine(f"{server}/{SCRATCH_DB}")
    try:
        async with engine.connect() as conn:
            result = await measure(conn, corpus, literals, truth, args.k)
    finally:
        await engine.dispose()
        if not args.keep:
            await admin_statement(server, f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
    return render(args, result[0], embed_seconds, *result[1:])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--records", type=int, default=30000)
    p.add_argument("--queries", type=int, default=300)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--keep", action="store_true", help=f"keep the {SCRATCH_DB} database")
    p.add_argument("--out", type=Path, default=Path("build/perf/halfvec-recall.md"))
    p.add_argument("--server", default=DEFAULT_SERVER, help="SQLAlchemy URL without database")
    p.add_argument("--source-db", default=DEFAULT_SOURCE_DB)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(main_async(args))
    print(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)


if __name__ == "__main__":
    main()
