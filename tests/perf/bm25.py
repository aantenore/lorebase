"""BM25 search against the default ranking on a seeded database (docs/performance.md#bm25-search).

    make perf-up PERF_VM=16g PERF_SEARCH=bm25
    LOREBASE_TEST_DATABASE_URL=postgresql://perf:perf@127.0.0.1:55410/postgres \\
        uv run python tests/perf/bm25.py --database perf_seed_1m_zipf --out build/perf/bm25-1m.md

against the perf Postgres on the ParadeDB image (`pg_search` preloaded), on a database
`bench.py --seed-database` loaded, with the migrations applied first. In order:

1. commits of `--changes` Part updates on `main` without the BM25 index (after as many
   untimed ones of the same keys);
2. the BM25 index built as `storage apply` builds it (time, size of its partitions);
3. the same commits with the index;
4. Keeper's lexical search (`search_records` with typo corrections, partial matching and the
   lexicon-sized ranking, as `reading.search` calls it) with each backend, called in turn
   with the same queries, alternating which goes first: cold, a new random query per call;
   warm, the same twenty queries in turn. On `main` after `VACUUM`; on `main` again while
   another connection commits `--changes` Part updates every `--write-interval` seconds
   (cold, a title word and a common word); and on an overlay with `--changes` Part updates
   (its own rows and the base's, `harness.overlay_pair`, which vacuums too);
5. the index dropped again unless `--keep-index`.

BM25 top-k scans slow down once rows changed since the last `VACUUM` (the index keeps the
old row versions and the scan checks visibility), which is why both states are measured.

Reads run as the owner role, which bypasses row-level security; the definer functions are the
same for both roles. The overlay is deleted at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib

import harness
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_core.model import Record
from lorebase_core.validate import RecordUpsert
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.search import common_terms, partial_matching, typo_tolerance
from lorebase_keeper.reading.settings import ReadSettings, SearchSettings
from lorebase_store import bm25, migrations, storage, testing
from lorebase_store.bm25 import SearchBackend
from lorebase_store.commit import commit
from lorebase_store.db import async_url, create_engine
from lorebase_store.layout import BranchLayout, load_layout
from lorebase_store.reads import search_corrections, search_records
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StorageSettings, StoreSettings

SEARCH_LIMIT = 20
WARM_QUERIES = 20
BACKENDS: tuple[SearchBackend, ...] = ("postgres", "bm25")

Query = Callable[[random.Random], str]


@dataclass
class Sample:
    """Timings of one case and how many of its calls answered `truncated`."""

    samples_ms: list[float] = field(default_factory=list)
    truncated: int = 0
    ranked_by: set[str] = field(default_factory=set)

    def p(self, q: float) -> float:
        ordered = sorted(self.samples_ms)
        return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _queries(work: harness.Workload) -> dict[str, Query]:
    words, common = work.words, work.common or work.words
    return {
        "title word": lambda rng: rng.choice(words),
        "common word": lambda rng: rng.choice(common),
        "two common words": lambda rng: " ".join(rng.sample(common, 2)),
        "title word and a common one": lambda rng: f"{rng.choice(words)} {rng.choice(common)}",
        "misspelled title word": lambda rng: harness.misspelled(rng.choice(words)),
        "phrase of two common words": lambda rng: '"' + " ".join(rng.sample(common, 2)) + '"',
    }


async def measure_search(
    engine: AsyncEngine,
    runtime: StoreRuntime,
    work: harness.Workload,
    branch: str,
    *,
    iterations: int,
    warmup: int,
    seed: int = 11,
    only: Sequence[str] | None = None,
    modes: Sequence[str] = ("cold", "warm"),
) -> dict[tuple[str, str], dict[SearchBackend, Sample]]:
    """(query, cold|warm) -> backend -> timings; `only` names the query kinds."""
    reading = ReadRuntime(
        sessions=async_sessionmaker(), read=ReadSettings(), search=SearchSettings(), store=runtime
    )
    typos, partial, sized = (
        typo_tolerance(reading),
        partial_matching(reading),
        common_terms(reading),
    )
    kinds = sorted(work.schema.kinds)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, harness.DOMAIN)
        layout: BranchLayout = await load_layout(session, harness.DOMAIN, branch)

    async def search(session: AsyncSession, q: str, backend: SearchBackend) -> tuple[bool, str]:
        fixes = await search_corrections(
            session, harness.DOMAIN, layout, q, typos, kind_ids=kinds, runtime=runtime
        )
        hits = await search_records(
            session,
            harness.DOMAIN,
            layout,
            q,
            limit=SEARCH_LIMIT,
            kind_ids=kinds,
            corrections=fixes,
            correction_weight=typos.weight,
            partial=partial,
            common=sized,
            backend=backend,
            runtime=runtime,
        )
        return hits.truncated, hits.backend

    results: dict[tuple[str, str], dict[SearchBackend, Sample]] = {}
    for name, query in _queries(work).items():
        if only is not None and name not in only:
            continue
        for mode in modes:
            samples: dict[SearchBackend, Sample] = {b: Sample() for b in BACKENDS}
            rng = random.Random(seed)
            fixed = [query(rng) for _ in range(WARM_QUERIES)]
            async with sessions() as session:
                for index in range(warmup + iterations):
                    q = query(rng) if mode == "cold" else fixed[index % WARM_QUERIES]
                    order = BACKENDS[index % 2 :] + BACKENDS[: index % 2]
                    for backend in order:
                        started = time.perf_counter()
                        async with session.begin():
                            await scope_to_domain(session, harness.DOMAIN)
                            truncated, ranked = await search(session, q, backend)
                        if index >= warmup:
                            sample = samples[backend]
                            sample.samples_ms.append((time.perf_counter() - started) * 1000)
                            sample.truncated += truncated
                            sample.ranked_by.add(ranked)
            harness._progress(
                f"search {branch} {name} {mode}: "
                + " | ".join(f"{b} {_line(s)}" for b, s in samples.items())
            )
            results[(name, mode)] = samples
    return results


def _line(sample: Sample) -> str:
    return f"p50 {statistics.median(sample.samples_ms):.2f} p95 {sample.p(0.95):.2f}"


async def keep_writing(
    engine: AsyncEngine,
    runtime: StoreRuntime,
    work: harness.Workload,
    args: argparse.Namespace,
    stop: asyncio.Event,
) -> int:
    """Commit batches of Part updates every `args.write_interval` seconds until `stop`."""
    batches = 0
    one = argparse.Namespace(changes=args.changes, commits=1, commit_warmup=0)
    while not stop.is_set():
        await measure_commits(engine, runtime, work, one, seed=time.time_ns() % 40_000)
        batches += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), args.write_interval)
    return batches


async def measure_commits(
    engine: AsyncEngine,
    runtime: StoreRuntime,
    work: harness.Workload,
    args: argparse.Namespace,
    seed: int,
) -> harness.Measurement:
    """Commits of `args.changes` Part updates (`unit_cost`, a value no commit wrote before:
    a commit that changes nothing writes nothing), each case on its own random Parts, after
    `args.commit_warmup` untimed ones."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    part = work.schema.kinds[work.part_kind]
    cost = next(a for a in part.attributes.values() if a.api_name == "unit_cost")
    measurement = harness.Measurement(f"commit of {args.changes} changes")
    rng = random.Random(seed)
    for index in range(args.commit_warmup + args.commits):
        keys = rng.sample(work.parts, args.changes)
        async with sessions() as session, session.begin():
            await scope_to_domain(session, harness.DOMAIN)
            rows = await harness._records(session, work.part_kind, keys)
        value = f"{seed * 1000 + index}.00"  # decimal(10,2), canonical, new each time
        updates = [
            RecordUpsert(
                record=Record(kind=work.part_kind, key=key, values={**rows[key], cost.id: value})
            )
            for key in keys
        ]
        started = time.perf_counter()
        async with sessions() as session:
            await commit(
                session,
                harness.DOMAIN,
                "main",
                updates,
                author=harness.AUTHOR,
                message="perf bm25 update",
                origin=harness.ORIGIN,
                runtime=runtime,
            )
        if index >= args.commit_warmup:
            measurement.samples_ms.append((time.perf_counter() - started) * 1000)
    return measurement


async def build_index(engine: AsyncEngine) -> tuple[float, int, list[str]]:
    """The BM25 index as `storage apply` builds it: seconds, bytes, step report."""
    settings = StorageSettings(bm25_index=True)
    report = storage.ApplyReport("bm25", {})
    ddl = await storage._autocommit(engine)
    try:
        state = await storage.read_state(ddl, StoreSettings().embeddings.dims)
        started = time.perf_counter()
        await storage._bm25_index(ddl, state, True, settings, report)
        seconds = time.perf_counter() - started
        await ddl.exec_driver_sql("ANALYZE mat_records")
    finally:
        await ddl.close()
    bm25.forget()
    return (
        seconds,
        await index_bytes(engine),
        [f"{s.step}: {s.status} {s.detail}" for s in report.steps],
    )


async def drop_index(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        auto = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql(f"DROP INDEX IF EXISTS {bm25.INDEX}")
    bm25.forget()


async def index_bytes(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        found = await connection.scalar(
            text(
                "SELECT coalesce(sum(pg_relation_size(i.inhrelid)), 0) FROM pg_inherits i "
                "WHERE i.inhparent = to_regclass(:index)"
            ),
            {"index": bm25.INDEX},
        )
    return int(found or 0)


async def table_bytes(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT 'mat_records heap', sum(pg_relation_size(c.oid)) FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'mat_records'::regclass "
                "UNION ALL SELECT 'GIN search index', sum(pg_relation_size(i.inhrelid)) "
                "FROM pg_inherits i WHERE i.inhparent = 'ix_mat_records_branch_search'::regclass"
            )
        )
        return {str(name): int(size or 0) for name, size in rows}


def _mb(value: float) -> str:
    return f"{value / 2**20:,.0f} MB"


def render(
    database: str,
    notes: dict[str, Any],
    searches: dict[str, dict[tuple[str, str], dict[SearchBackend, Sample]]],
    commits: dict[str, harness.Measurement],
) -> str:
    lines = [
        f"# BM25 search: {database}",
        "",
        f"{notes['iterations']} timed calls per case after {notes['warmup']}; "
        f"`{harness.hardware().get('cpu', '')}`; Postgres {notes['server'].get('postgres', '')}, "
        f"shared_buffers {notes['server'].get('shared_buffers', '')}.",
        "",
        f"- BM25 index: built in {notes['build_s']:.0f} s, {_mb(notes['index_bytes'])} "
        f"({'; '.join(notes['steps'])})",
        *(f"- {name}: {_mb(size)}" for name, size in notes["tables"].items()),
        f"- common words: {', '.join(notes['common'][:5])}... ({len(notes['common'])})",
        "",
        "## Commits (ms)",
        "",
        "| Commit | p50 | p95 |",
        "|---|---:|---:|",
        *(f"| {name} | {m.p50:.1f} | {m.p95:.1f} |" for name, m in commits.items()),
    ]
    for branch, results in searches.items():
        lines += [
            "",
            f"## Search on {branch} (ms): postgres / bm25",
            "",
            "| Query | Calls | postgres p50 | postgres p95 | bm25 p50 | bm25 p95 "
            "| truncated postgres / bm25 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for (name, mode), samples in results.items():
            pg, bm = samples["postgres"], samples["bm25"]
            ranked = "" if bm.ranked_by == {"bm25"} else f" (ranked by {sorted(bm.ranked_by)})"
            lines.append(
                f"| {name}{ranked} | {mode} | {statistics.median(pg.samples_ms):.2f} "
                f"| {pg.p(0.95):.2f} | {statistics.median(bm.samples_ms):.2f} "
                f"| {bm.p(0.95):.2f} | {pg.truncated} / {bm.truncated} |"
            )
    return "\n".join([*lines, ""])


async def run(args: argparse.Namespace, url: str) -> str:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=url, statement_timeout_ms=0)
    try:
        await harness.remove_perf_branches(engine, runtime)
        await drop_index(engine)
        work = await harness.workload(engine, runtime)
        commits: dict[str, harness.Measurement] = {}
        if not args.skip_commits:
            commits["without the BM25 index"] = await measure_commits(
                engine, runtime, work, args, seed=time.time_ns() % 40_000
            )
        harness._progress("building the BM25 index")
        build_s, size, steps = await build_index(engine)
        harness._progress(f"built in {build_s:.0f} s, {_mb(size)}")
        if not args.skip_commits:
            commits["with the BM25 index"] = await measure_commits(
                engine, runtime, work, args, seed=time.time_ns() % 40_000 + 45_000
            )
        await harness._analyze(engine)
        searches = {
            "main": await measure_search(
                engine, runtime, work, "main", iterations=args.iterations, warmup=args.warmup
            )
        }
        stop = asyncio.Event()
        writer = asyncio.create_task(keep_writing(engine, runtime, work, args, stop))
        try:
            searches[
                f"main, commits of {args.changes} updates every {args.write_interval:g} s"
            ] = await measure_search(
                engine,
                runtime,
                work,
                "main",
                iterations=args.iterations,
                warmup=args.warmup,
                only=("title word", "common word"),
                modes=("cold",),
            )
        finally:
            stop.set()
            harness._progress(f"{await writer} commits while searching")
        if not args.skip_overlay:
            pair = await harness.overlay_pair(
                engine, runtime, work, changes=args.changes, copy=False
            )
            harness._progress(f"overlay with {pair.overlay_rows} rows of its own")
            try:
                searches[f"an overlay ({pair.overlay_rows} own rows)"] = await measure_search(
                    engine,
                    runtime,
                    work,
                    harness.OVERLAY_BRANCH,
                    iterations=args.iterations,
                    warmup=args.warmup,
                )
            finally:
                await harness.remove_perf_branches(engine, runtime)
        notes = {
            "iterations": args.iterations,
            "warmup": args.warmup,
            "server": await harness.server_notes(engine),
            "build_s": build_s,
            "index_bytes": size,
            "steps": steps,
            "tables": await table_bytes(engine),
            "common": work.common,
        }
        if not args.keep_index:
            await drop_index(engine)
    finally:
        await engine.dispose()
    return render(args.database, notes, searches, commits)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="a database bench.py seeded")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--changes", type=int, default=100, help="Part updates per commit")
    parser.add_argument("--commits", type=int, default=20, help="timed commits per case")
    parser.add_argument("--commit-warmup", type=int, default=20, help="untimed commits first")
    parser.add_argument("--write-interval", type=float, default=1.0, help="seconds between commits")
    parser.add_argument("--skip-commits", action="store_true")
    parser.add_argument("--skip-overlay", action="store_true")
    parser.add_argument("--keep-index", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    with testing.postgres_server() as server:
        url = async_url(server).set(database=args.database).render_as_string(hide_password=False)
        migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
        markdown = asyncio.run(run(args, url))
    print(markdown)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")


if __name__ == "__main__":
    main()
