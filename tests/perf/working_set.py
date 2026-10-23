"""Working set of the read paths: relation sizes, column widths, pages touched per read.

    uv run python tests/perf/working_set.py --database perf_seed_1m_zipf --out build/perf/ws.md

against the server of `LOREBASE_TEST_DATABASE_URL`, on a database `bench.py --seed-database`
loaded. Nothing is written to the Domain unless `--commits` asks for commits. The report has:

1. The size of every relation (heap, TOAST, each index; hash partitions and the indexes of
   the partitions folded under their table), per Record, with `idx_scan` and the reads that
   touched it; the hot set is the sum of the relations some measured read touched.
2. The width of the columns of the large tables (`pg_stats`), the average row size of a 1%
   sample and rows per heap page.
3. Pages touched per call of each read: the deltas of `pg_statio_user_tables` and
   `pg_statio_user_indexes` over `--iterations` calls (one transaction each, random keys,
   all on one connection, with the statistics of that backend flushed before each snapshot),
   and the latency of those calls (cold) and of the same keys read again (warm).
4. With `--buffercache`: the shared buffers each relation holds after the reads.
5. The server settings and load average.

Counters are cumulative for the server: run on an otherwise idle server. The row counts and
the row size sample read the tables, so they run after the measured reads. Reads run as the
owner role, which bypasses row-level security. `--migrate` applies the migrations first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from lorebase_store import migrations, testing
from lorebase_store.db import async_url, create_engine
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

#: Tables reported, in this order; other `mat_*` and `lineage*` tables follow them.
TABLES = (
    "mat_records",
    "mat_links",
    "mat_history",
    "mat_lexicon",
    "embeddings",
    "objects",
    "tree_nodes",
    "changesets",
    "revisions",
    "provenance",
    "refs",
    "mat_schema",
)
EXTRA_PREFIXES = ("mat_", "lineage")
WIDTH_TABLES = ("mat_records", "mat_links", "mat_history")
#: Runtime indexes of `mat_records`, one per indexed Attribute and branch.
ATTRIBUTE_PREFIX = "attr_"
SEED = 7
TOP_LINES = 12
TOP_BUFFERS = 25
MB = 1024 * 1024

_PARTITION = re.compile(r"_p\d+(?=_|$)")

_CATALOG = text(
    """
SELECT c.oid::bigint AS oid, c.relname, n.nspname AS schema, c.relkind::text AS kind,
    c.reltoastrelid::bigint AS toast,
    c.relpages, c.reltuples::float8 AS tuples,
    (SELECT i.inhparent::bigint FROM pg_inherits i WHERE i.inhrelid = c.oid) AS parent,
    x.indrelid::bigint AS indrelid,
    CASE WHEN c.relkind IN ('r', 'i') THEN pg_relation_size(c.oid) ELSE 0 END AS size,
    CASE WHEN c.reltoastrelid <> 0 THEN pg_total_relation_size(c.reltoastrelid) ELSE 0 END
        AS toast_size,
    coalesce(s.idx_scan, 0) AS scans
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_index x ON x.indexrelid = c.oid
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = c.oid
WHERE (n.nspname = 'public' AND c.relkind IN ('r', 'p', 'i'))
   OR (n.nspname = 'pg_toast' AND c.relkind IN ('t', 'i'))
"""
)
_STATIO_TABLES = text(
    "SELECT relid::bigint AS oid, coalesce(heap_blks_read, 0) AS heap_read, "
    "coalesce(heap_blks_hit, 0) AS heap_hit, coalesce(toast_blks_read, 0) + "
    "coalesce(tidx_blks_read, 0) AS toast_read, coalesce(toast_blks_hit, 0) + "
    "coalesce(tidx_blks_hit, 0) AS toast_hit FROM pg_statio_user_tables"
)
_STATIO_INDEXES = text(
    "SELECT indexrelid::bigint AS oid, coalesce(idx_blks_read, 0) AS blks_read, "
    "coalesce(idx_blks_hit, 0) AS blks_hit FROM pg_statio_user_indexes"
)
_BUFFERS = text(
    "SELECT c.oid::bigint AS oid, count(*) AS buffers FROM pg_buffercache b "
    "JOIN pg_class c ON b.relfilenode = pg_relation_filenode(c.oid) "
    "AND b.reldatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "GROUP BY c.oid"
)
_STATS = text(
    "SELECT s.attname, s.avg_width, s.null_frac, s.n_distinct FROM pg_stats s "
    "JOIN pg_attribute a ON a.attrelid = to_regclass(format('public.%I', s.tablename)) "
    "AND a.attname = s.attname WHERE s.schemaname = 'public' AND s.tablename = :t "
    "ORDER BY a.attnum"
)


@dataclass
class Entry:
    """One reported relation: a heap, the TOAST of a table, an index or a group of indexes."""

    kind: str
    table: str
    size: int = 0
    scans: int = 0
    count: int = 0
    used_by: list[str] = field(default_factory=list)


@dataclass
class Catalog:
    tables: list[str]
    #: label -> entry, in report order.
    entries: dict[str, Entry]
    #: relation oid -> label (heaps, indexes, TOAST tables and their indexes).
    labels: dict[int, str]
    #: table -> (partition with the most rows or the table itself, pages, rows) of its heaps.
    stats_table: dict[str, str]
    pages: dict[str, int]
    rows: dict[str, float]
    #: Names of the runtime per-Attribute indexes: name -> (size, idx_scan).
    attribute_indexes: dict[str, tuple[int, int]]


def _index_label(table: str, name: str) -> str:
    plain = _PARTITION.sub("", name)
    if plain.startswith(f"{table}_{ATTRIBUTE_PREFIX}"):
        return f"{table} per-Attribute indexes"
    rest = plain[len(table) + 1 :] if plain.startswith(f"{table}_") else plain
    return f"{table} {rest}"


async def load_catalog(connection: AsyncConnection) -> Catalog:
    rows = [dict(r) for r in (await connection.execute(_CATALOG)).mappings()]
    by_oid = {r["oid"]: r for r in rows}

    def top(oid: int) -> str:
        row = by_oid[oid]
        return str(by_oid[row["parent"]]["relname"] if row["parent"] in by_oid else row["relname"])

    heaps = [r for r in rows if r["kind"] in ("r", "p")]
    names = {top(r["oid"]) for r in heaps}
    wanted = {n for n in names if n in TABLES or n.startswith(EXTRA_PREFIXES)}
    tables = [n for n in TABLES if n in wanted] + sorted(wanted - set(TABLES))
    entries: dict[str, Entry] = {}
    labels: dict[int, str] = {}
    toast_owner: dict[int, str] = {}
    pages = dict.fromkeys(tables, 0)
    tuples = dict.fromkeys(tables, 0.0)
    best: dict[str, tuple[float, str]] = {}
    attributes: dict[str, tuple[int, int]] = {}
    for table in tables:
        entries[table] = Entry("heap", table)
        entries[f"{table} toast"] = Entry("toast", table)
    for r in heaps:
        table = top(r["oid"])
        if table not in wanted:
            continue
        labels[r["oid"]] = table
        entries[table].size += r["size"]
        if r["toast"]:
            toast_owner[r["toast"]] = table
            entries[f"{table} toast"].size += r["toast_size"]
        if r["kind"] == "r":
            pages[table] += max(r["relpages"], 0)
            tuples[table] += max(r["tuples"], 0.0)
            if r["tuples"] > best.get(table, (0.0, ""))[0] or table not in best:
                best[table] = (r["tuples"], r["relname"])
    for r in rows:
        if r["kind"] == "t" and r["oid"] in toast_owner:
            labels[r["oid"]] = f"{toast_owner[r['oid']]} toast"
        elif r["kind"] == "i" and r["indrelid"] in toast_owner:
            labels[r["oid"]] = f"{toast_owner[r['indrelid']]} toast"
        elif r["kind"] == "i" and r["indrelid"] in by_oid and r["schema"] == "public":
            table = top(r["indrelid"])
            if table not in wanted:
                continue
            label = _index_label(table, r["relname"])
            entry = entries.setdefault(label, Entry("index", table))
            entry.size += r["size"]
            entry.scans += r["scans"]
            entry.count += 1
            labels[r["oid"]] = label
            if label.endswith("per-Attribute indexes"):
                attributes[r["relname"]] = (r["size"], r["scans"])
    entries = {k: v for k, v in entries.items() if v.size or v.kind != "toast"}
    stats = {t: best[t][1] if t in best and best[t][0] > 0 else t for t in tables}
    return Catalog(tables, entries, labels, stats, pages, tuples, attributes)


# Reads ----------------------------------------------------------------------------------------

Counters = dict[str, tuple[int, int]]


async def snapshot(session: AsyncSession, catalog: Catalog) -> Counters:
    """(hit, read) blocks per label from the statistics views, after this backend flushed its
    pending statistics."""
    async with session.begin():
        await session.execute(text("SELECT pg_stat_force_next_flush()"))
    totals: Counters = {}

    def add(label: str, hit: int, read: int) -> None:
        old = totals.get(label, (0, 0))
        totals[label] = (old[0] + hit, old[1] + read)

    async with session.begin():
        for r in (await session.execute(_STATIO_TABLES)).mappings():
            label = catalog.labels.get(r["oid"])
            if label is not None:
                add(label, r["heap_hit"], r["heap_read"])
                add(f"{label} toast", r["toast_hit"], r["toast_read"])
        for r in (await session.execute(_STATIO_INDEXES)).mappings():
            label = catalog.labels.get(r["oid"])
            if label is not None:
                add(label, r["blks_hit"], r["blks_read"])
    return totals


async def timed_calls(
    session: AsyncSession, operation: harness.Operation, seed: int, count: int
) -> harness.Measurement:
    """`count` calls, one transaction each, keys drawn from `random.Random(seed)`."""
    rng = random.Random(seed)
    measurement = harness.Measurement("")
    for _ in range(count):
        started = time.perf_counter()
        async with session.begin():
            await scope_to_domain(session, harness.DOMAIN)
            await operation(session, rng)
        measurement.samples_ms.append((time.perf_counter() - started) * 1000)
    return measurement


@dataclass
class OpResult:
    name: str
    cold: harness.Measurement
    warm: harness.Measurement
    #: label -> (hit, read) blocks over all the timed calls.
    blocks: Counters
    calls: int

    @property
    def pages(self) -> float:
        return sum(h + r for h, r in self.blocks.values()) / self.calls

    @property
    def reads(self) -> float:
        return sum(r for _, r in self.blocks.values()) / self.calls


async def measure_ops(
    engine: AsyncEngine,
    operations: dict[str, harness.Operation],
    catalog: Catalog,
    *,
    iterations: int,
    warmup: int,
) -> list[OpResult]:
    results: list[OpResult] = []
    async with engine.connect() as connection:
        session = AsyncSession(bind=connection, expire_on_commit=False)
        async with session.begin():
            await session.execute(text("SET stats_fetch_consistency = 'none'"))
        async with session.begin():
            backend = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        harness._progress(f"measuring on backend {backend}")
        for name, operation in operations.items():
            await timed_calls(session, operation, SEED + 1000, warmup)
            before = await snapshot(session, catalog)
            cold = await timed_calls(session, operation, SEED, iterations)
            after = await snapshot(session, catalog)
            warm = await timed_calls(session, operation, SEED, iterations)
            blocks: Counters = {}
            for label, (hit, read) in after.items():
                b_hit, b_read = before.get(label, (0, 0))
                if hit - b_hit or read - b_read:
                    blocks[label] = (hit - b_hit, read - b_read)
            result = OpResult(name, cold, warm, blocks, iterations)
            results.append(result)
            harness._progress(
                f"{name}: cold p50 {cold.p50:.2f} warm p50 {warm.p50:.2f} ms, "
                f"{result.pages:.1f} pages, {result.reads:.1f} read per call"
            )
            for label in blocks:
                if label in catalog.entries:
                    catalog.entries[label].used_by.append(name)
        await session.close()
    return results


async def buffer_residency(connection: AsyncConnection, catalog: Catalog) -> dict[str, int]:
    """Shared buffers per label."""
    await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_buffercache"))
    await connection.commit()
    found: dict[str, int] = {}
    for r in (await connection.execute(_BUFFERS)).mappings():
        label = catalog.labels.get(r["oid"])
        if label is not None:
            found[label] = found.get(label, 0) + r["buffers"]
    await connection.commit()
    return found


async def sample_widths(connection: AsyncConnection, catalog: Catalog) -> dict[str, Any]:
    """Statistics, average row size of a 1% sample and rows per page of the wide tables."""
    found: dict[str, Any] = {}
    for table in WIDTH_TABLES:
        if table not in catalog.tables:
            continue
        columns = [
            {
                "column": r["attname"],
                "avg_width": r["avg_width"],
                "null_frac": float(r["null_frac"]),
                "n_distinct": float(r["n_distinct"]),
            }
            for r in (
                await connection.execute(_STATS, {"t": catalog.stats_table[table]})
            ).mappings()
        ]
        sampled = (
            await connection.execute(
                text(
                    f"SELECT avg(pg_column_size(t.*))::float8, count(*) "
                    f"FROM {table} t TABLESAMPLE SYSTEM (1)"
                )
            )
        ).one()
        pages = catalog.pages[table]
        found[table] = {
            "stats_from": catalog.stats_table[table],
            "columns": columns,
            "avg_row_bytes": sampled[0],
            "sampled_rows": sampled[1],
            "rows_per_page": catalog.rows[table] / pages if pages else None,
        }
    return found


async def count_records(connection: AsyncConnection) -> int:
    sql = "SELECT count(*) FROM mat_records WHERE branch = 'main' AND NOT deleted"
    return int((await connection.execute(text(sql))).scalar_one())


# Report ---------------------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]], left: int = 1) -> list[str]:
    """Markdown table; the first `left` columns are left aligned."""
    align = [":---" if i < left else "---:" for i in range(len(headers))]
    lines = [f"| {' | '.join(headers)} |", f"|{'|'.join(align)}|"]
    lines += [f"| {' | '.join(str(c) for c in row)} |" for row in rows]
    return lines


def _mb(size: float) -> str:
    return f"{size / MB:,.1f}"


def render(report: dict[str, Any], catalog: Catalog, results: list[OpResult]) -> str:
    records = report["records"]
    env = report["environment"]
    title = f"# Working set: {report['database']}"
    if report["label"]:
        title += f" ({report['label']})"
    lines = [title, "", f"{records:,} Records on `main`.", ""]
    hot = {label for label, e in catalog.entries.items() if e.used_by}
    lines += ["## Relation sizes", ""]
    rows: list[list[object]] = []
    total = hot_total = 0
    for table in catalog.tables:
        for label, e in catalog.entries.items():
            if e.table != table:
                continue
            name = label
            if label.endswith("per-Attribute indexes"):
                name = f"{label} ({e.count})"
            total += e.size
            hot_total += e.size if label in hot else 0
            per = e.size // records if records else 0
            rows.append(
                [name, e.kind, _mb(e.size), f"{per:,}", f"{e.scans:,}", ", ".join(e.used_by)]
            )
    rows.append(["total", "", _mb(total), f"{total // records if records else 0:,}", "", ""])
    share = f" ({hot_total / total:.0%})" if total else ""
    per_hot = hot_total // records if records else 0
    rows.append(["hot set", "", _mb(hot_total), f"{per_hot:,}", "", f"{len(hot)} relations{share}"])
    lines += _table(["relation", "kind", "MB", "bytes/Record", "idx_scan", "used by"], rows, left=2)
    if catalog.attribute_indexes:
        lines += ["", "Per-Attribute indexes:", ""]
        lines += _table(
            ["index", "MB", "idx_scan"],
            [
                [name, _mb(size), f"{scans:,}"]
                for name, (size, scans) in sorted(catalog.attribute_indexes.items())
            ],
        )
    lines += ["", "## Column widths", ""]
    for table, info in report["widths"].items():
        per_page = info["rows_per_page"]
        lines += [
            f"### {table}",
            "",
            f"Statistics of `{info['stats_from']}`; average row {info['avg_row_bytes'] or 0:,.0f} "
            f"bytes (1% sample, {info['sampled_rows']:,} rows); "
            f"{f'{per_page:,.1f}' if per_page else 'n/a'} rows per heap page.",
            "",
        ]
        if info["columns"]:
            lines += _table(
                ["column", "avg_width", "null_frac", "n_distinct"],
                [
                    [c["column"], c["avg_width"], f"{c['null_frac']:.3f}", f"{c['n_distinct']:g}"]
                    for c in info["columns"]
                ],
            )
        else:
            lines.append("No statistics (not analyzed).")
        lines.append("")
    lines += [
        "## Pages touched per read",
        "",
        f"{report['iterations']} calls per read, random keys (cold) and the same keys again "
        "(warm); latency in ms. `read` pages came from outside shared buffers.",
        "",
    ]
    lines += _table(
        ["read", "cold p50", "cold p95", "warm p50", "warm p95", "pages/call", "read pages/call"],
        [
            [
                r.name,
                f"{r.cold.p50:.2f}",
                f"{r.cold.p95:.2f}",
                f"{r.warm.p50:.2f}",
                f"{r.warm.p95:.2f}",
                f"{r.pages:.1f}",
                f"{r.reads:.1f}",
            ]
            for r in results
        ],
    )
    for r in results:
        lines += ["", f"### {r.name}: {r.pages:.1f} pages, {r.reads:.1f} read per call", ""]
        ranked = sorted(r.blocks.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:TOP_LINES]
        lines += _table(
            ["relation", "hit", "read", "total"],
            [
                [label, f"{h / r.calls:.2f}", f"{rd / r.calls:.2f}", f"{(h + rd) / r.calls:.2f}"]
                for label, (h, rd) in ranked
            ],
        )
    if report["commits"] is not None:
        samples = sorted(report["commits"]["samples_ms"])
        lines += [
            "",
            "## Commits",
            "",
            f"{report['commits']['name']}, {len(samples)} runs after the reads: p50 "
            f"{statistics.median(samples):,.0f} ms, max {samples[-1]:,.0f} ms, all "
            + ", ".join(f"{v:,.0f}" for v in report["commits"]["samples_ms"])
            + " ms.",
        ]
    if report["buffers"] is not None:
        lines += [
            "",
            "## Buffer residency",
            "",
            f"shared_buffers {env['shared_buffers']}; after the reads above.",
            "",
        ]
        ranked_buffers = sorted(report["buffers"].items(), key=lambda kv: -kv[1])[:TOP_BUFFERS]
        lines += _table(
            ["relation", "resident MB", "% of relation"],
            [
                [
                    label,
                    _mb(n * report["block_size"]),
                    f"{n * report['block_size'] / catalog.entries[label].size:.0%}"
                    if label in catalog.entries and catalog.entries[label].size
                    else "n/a",
                ]
                for label, n in ranked_buffers
            ],
        )
    lines += [
        "",
        "## Environment",
        "",
        f"PostgreSQL {env['postgres']}, shared_buffers {env['shared_buffers']}, "
        f"effective_cache_size {env['effective_cache_size']}; load average "
        f"{_load(env['load_start'])} at start, {_load(env['load_end'])} at end; "
        f"{env['date']}.",
        "",
    ]
    return "\n".join(lines)


def _load(values: Sequence[float]) -> str:
    return "/".join(f"{v:.2f}" for v in values)


async def run(args: argparse.Namespace, url: str) -> tuple[str, dict[str, Any]]:
    started = datetime.now(UTC)
    load_start = os.getloadavg()
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=url, statement_timeout_ms=0)
    try:
        work = await harness.workload(engine, runtime)
        available = harness.read_operations(work, runtime)
        names = args.ops or [n for n in available if n != harness.COMMON_SEARCH]
        unknown = [n for n in names if n not in available]
        if unknown:
            raise SystemExit(f"unknown reads {unknown}; available: {sorted(available)}")
        environment = await harness.server_notes(engine)
        async with engine.connect() as connection:
            catalog = await load_catalog(connection)
        results = await measure_ops(
            engine,
            {n: available[n] for n in names},
            catalog,
            iterations=args.iterations,
            warmup=args.warmup,
        )
        async with engine.connect() as connection:
            await connection.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
            buffers = block_size = None
            if args.buffercache:
                buffers = await buffer_residency(connection, catalog)
                block_size = int(
                    (
                        await connection.execute(text("SELECT current_setting('block_size')"))
                    ).scalar_one()
                )
            fresh = await load_catalog(connection)  # idx_scan after the reads
            for label, entry in catalog.entries.items():
                entry.scans = fresh.entries[label].scans if label in fresh.entries else 0
            catalog.attribute_indexes = fresh.attribute_indexes
            records = args.records if args.records is not None else await count_records(connection)
            widths = await sample_widths(connection, catalog)
        commits = None
        if args.commits:
            commits = await harness.measure_commits(
                engine, runtime, work, changes=args.commit_changes, iterations=args.commits
            )
    finally:
        await engine.dispose()
    report: dict[str, Any] = {
        "database": args.database,
        "label": args.label,
        "iterations": args.iterations,
        "records": records,
        "buffers": buffers,
        "block_size": block_size,
        "commits": None
        if commits is None
        else {"name": commits.name, "samples_ms": commits.samples_ms},
        "widths": widths,
        "environment": {
            "postgres": environment["postgres"],
            "shared_buffers": environment["shared_buffers"],
            "effective_cache_size": environment["effective_cache_size"],
            "load_start": load_start,
            "load_end": os.getloadavg(),
            "date": started.strftime("%Y-%m-%d %H:%M UTC"),
        },
    }
    markdown = render(report, catalog, results)
    dump = {
        **report,
        "relations": {
            label: {
                "kind": e.kind,
                "bytes": e.size,
                "idx_scan": e.scans,
                "count": e.count,
                "used_by": e.used_by,
            }
            for label, e in catalog.entries.items()
        },
        "per_attribute_indexes": catalog.attribute_indexes,
        "reads": {
            r.name: {
                "cold_ms": r.cold.samples_ms,
                "warm_ms": r.warm.samples_ms,
                "blocks": {k: {"hit": h, "read": rd} for k, (h, rd) in r.blocks.items()},
                "calls": r.calls,
                "pages_per_call": r.pages,
                "read_pages_per_call": r.reads,
            }
            for r in results
        },
    }
    return markdown, dump


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="a database bench.py seeded")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--ops", type=lambda s: [n.strip() for n in s.split(",") if n.strip()])
    parser.add_argument("--migrate", action="store_true", help="apply the migrations first")
    parser.add_argument("--buffercache", action="store_true", help="report buffer residency")
    parser.add_argument("--records", type=int, default=None, help="Record count (default: exact)")
    parser.add_argument("--label", default="", help="text for the heading")
    parser.add_argument("--out", type=Path, default=None, help="write the markdown here")
    parser.add_argument("--json", type=Path, default=None, help="write every number here")
    parser.add_argument(
        "--commits", type=int, default=0, help="also time this many commits (writes the Domain)"
    )
    parser.add_argument("--commit-changes", type=int, default=1000, help="Part updates each")
    args = parser.parse_args(argv)
    with testing.postgres_server() as server:
        url = async_url(server).set(database=args.database).render_as_string(hide_password=False)
        if args.migrate:
            migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
        markdown, dump = asyncio.run(run(args, url))
    print(markdown)
    for path, content in ((args.out, markdown), (args.json, json.dumps(dump, indent=2))):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
