"""Focused comparisons on a seeded database, without the full baseline run of `bench.py`.

    uv run python tests/perf/focus.py --database perf_seed_1m_zipf --out build/perf/focus-1m.md

against the server of `LOREBASE_TEST_DATABASE_URL` (the perf Postgres of `make perf-1m`), on
a database `bench.py --seed-database` loaded. It measures, with the migrations applied first:

- Search: Keeper's lexical search primitive (`search_records` with typo corrections and
  partial matching, as `reading.search` calls it) with the ranking sized by the word lexicon
  (`search.common_term_*`, `CommonTerms`) against `store.reads.search_candidates` for every
  query (`common_term_docs: 0`, the behaviour before), called in turn with the same queries,
  for: a title word of the workload, a word above `search.common_term_docs`, two such words,
  a title word with a common one, and a misspelled title word. Each row reports how many
  responses were `truncated`.
- Neighborhood: `POST /neighborhood` (depth 2) through the Keeper application in process,
  with and without `values: false`, on `main` and on an overlay branch it creates (and
  deletes); cold, each call on a new random Part, and warm, the same Part read again; with the
  size of the response bodies.

Reads run as the owner role, which bypasses row-level security; the definer functions are the
same for both roles (`bench.py` measures both).
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

import harness
import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_keeper.app import create_app
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.reading.search import common_terms, partial_matching, typo_tolerance
from lorebase_keeper.reading.settings import ReadSettings, SearchSettings
from lorebase_keeper.settings import KeeperSettings
from lorebase_platform.settings import Settings
from lorebase_store import migrations, testing
from lorebase_store.db import async_url, create_engine
from lorebase_store.layout import BranchLayout, load_layout
from lorebase_store.materialize import BranchMode, create_branch
from lorebase_store.reads import CommonTerms, search_corrections, search_records
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

OVERLAY = "agent/perf-focus"
NEIGHBORHOOD_DEPTH = 2
SEARCH_LIMIT = 20
#: Words of the workload above `search.common_term_docs` used for the common-term queries.
COMMON_WORDS = 30

Query = Callable[[random.Random], str]
#: The `search` settings measured (`--common-term-docs`, `--common-term-candidates`).
SETTINGS = SearchSettings()


@dataclass
class Sample:
    """Timings of one case and how many of its calls answered `truncated`."""

    name: str
    samples_ms: list[float] = field(default_factory=list)
    truncated: int = 0
    sizes: list[int] = field(default_factory=list)

    def p(self, q: float) -> float:
        ordered = sorted(self.samples_ms)
        return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def common_words(engine: AsyncEngine, docs: int) -> list[tuple[str, int]]:
    """Words of `main` above `docs` Records (all Kinds), most common first."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', false)"))
        rows = await connection.execute(
            text(
                "SELECT word, sum(records) FROM mat_lexicon WHERE domain_id = :d "
                "AND branch = 'main' AND word ~ '^[a-z]{4,}$' GROUP BY word "
                "HAVING sum(records) > :docs ORDER BY 2 DESC, 1 LIMIT :n"
            ),
            {"d": harness.DOMAIN, "docs": docs, "n": COMMON_WORDS},
        )
        return [(word, int(count)) for word, count in rows]


def _queries(work: harness.Workload, common: Sequence[str]) -> dict[str, Query]:
    words = work.words
    fallback = list(common) or words
    return {
        "title word": lambda rng: rng.choice(words),
        "common word": lambda rng: rng.choice(fallback),
        "two common words": lambda rng: " ".join(rng.sample(fallback, 2)),
        "title word and a common one": lambda rng: f"{rng.choice(words)} {rng.choice(fallback)}",
        "misspelled title word": lambda rng: harness.misspelled(rng.choice(words)),
    }


async def measure_search(
    engine: AsyncEngine,
    runtime: StoreRuntime,
    work: harness.Workload,
    common: Sequence[str],
    *,
    iterations: int,
    warmup: int,
    seed: int = 11,
) -> dict[str, tuple[Sample, Sample]]:
    """Each query kind with `search.common_term_*` (lexicon-sized ranking) and without it."""
    settings = SETTINGS
    reading = ReadRuntime(
        sessions=async_sessionmaker(), read=ReadSettings(), search=settings, store=runtime
    )
    typos, partial = typo_tolerance(reading), partial_matching(reading)
    kinds = sorted(work.schema.kinds)
    layout = BranchLayout(harness.DOMAIN, "main")

    async def search(session: AsyncSession, q: str, terms: CommonTerms | None) -> bool:
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
            common=terms,
            runtime=runtime,
        )
        return hits.truncated

    results: dict[str, tuple[Sample, Sample]] = {}
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    sized = common_terms(reading)
    for name, query in _queries(work, common).items():
        legacy, lexicon = Sample(name), Sample(name)
        rng = random.Random(seed)
        cases = ((None, legacy), (sized, lexicon))
        async with sessions() as session:
            for index in range(warmup + iterations):
                q = query(rng)
                # Each case goes first in turn: the second finds the pages the first read.
                for terms, sample in cases[index % 2 :] + cases[: index % 2]:
                    started = time.perf_counter()
                    async with session.begin():
                        await scope_to_domain(session, harness.DOMAIN)
                        truncated = await search(session, q, terms)
                    if index >= warmup:
                        sample.samples_ms.append((time.perf_counter() - started) * 1000)
                        sample.truncated += truncated
        harness._progress(f"search {name}: {_line(legacy)} | {_line(lexicon)}")
        results[name] = (legacy, lexicon)
    return results


def _line(sample: Sample) -> str:
    return f"p50 {statistics.median(sample.samples_ms):.2f} p95 {sample.p(0.95):.2f}"


async def measure_neighborhood(
    url: str,
    work: harness.Workload,
    branches: Sequence[str],
    *,
    iterations: int,
    warmup: int,
    seed: int = 13,
) -> dict[tuple[str, str], tuple[Sample, Sample]]:
    """(branch, cold|warm) -> (with values, `values: false`) through Keeper in process."""
    keeper = KeeperSettings()
    settings = Settings(
        keeper={"reads": {"cache": {"enabled": False}}, "limits": {"rate": {"enabled": False}}},
        database={"dsn": url},
    )
    app = create_app(settings)
    base = f"{keeper.api_prefix}/domains/{harness.DOMAIN}"
    part = work.schema.kinds[work.part_kind].api_name
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    results: dict[tuple[str, str], tuple[Sample, Sample]] = {}

    async def call(http: httpx.AsyncClient, branch: str, key: str, values: bool) -> Any:
        body = {
            "branch": branch,
            "seeds": [{"kind": part, "key": key}],
            "depth": NEIGHBORHOOD_DEPTH,
            "values": values,
        }
        return await http.post(f"{base}/neighborhood", json=body)

    async def timed(
        http: httpx.AsyncClient, branch: str, key: str, values: bool, sample: Sample, kept: bool
    ) -> None:
        started = time.perf_counter()
        response = await call(http, branch, key, values)
        elapsed = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, response.text
        if kept:
            sample.samples_ms.append(elapsed)
            sample.sizes.append(len(response.content))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http,
    ):
        for offset, branch in enumerate(branches):
            # Cold: each variant (and branch) reads its own random Parts, alternating which
            # variant goes first.
            full, headers = Sample("values"), Sample("values: false")
            first = seed + 10 * offset
            streams = (
                (True, random.Random(first), full),
                (False, random.Random(first + 1), headers),
            )
            for index in range(warmup + iterations):
                for values, rng, sample in streams[index % 2 :] + streams[: index % 2]:
                    await timed(
                        http, branch, rng.choice(work.parts), values, sample, index >= warmup
                    )
            results[(branch, "cold")] = (full, headers)
            harness._progress(f"neighborhood {branch} cold: {_line(full)} | {_line(headers)}")
            # Warm: one Part read once, then by both variants in turn.
            full, headers = Sample("values"), Sample("values: false")
            rng = random.Random(first + 2)
            cases = ((True, full), (False, headers))
            for index in range(warmup + iterations):
                key = rng.choice(work.parts)
                await call(http, branch, key, True)
                for values, sample in cases[index % 2 :] + cases[: index % 2]:
                    await timed(http, branch, key, values, sample, index >= warmup)
            results[(branch, "warm")] = (full, headers)
            harness._progress(f"neighborhood {branch} warm: {_line(full)} | {_line(headers)}")
    return results


async def _overlay(engine: AsyncEngine, runtime: StoreRuntime, *, create: bool) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    if not create:
        await harness.remove_perf_branches(engine, runtime)  # names starting agent/perf
        return
    async with sessions() as session:
        await create_branch(
            session,
            harness.DOMAIN,
            OVERLAY,
            created_by="usr_perf",
            mode=BranchMode.OVERLAY,
            runtime=runtime,
        )
    await harness.build_indexes(engine, runtime)
    async with sessions() as session, session.begin():
        await scope_to_domain(session, harness.DOMAIN)
        assert (await load_layout(session, harness.DOMAIN, OVERLAY)).overlay


def _ms(value: float) -> str:
    return f"{value:.2f}"


def render(
    database: str,
    common: Sequence[tuple[str, int]],
    search: dict[str, tuple[Sample, Sample]],
    hood: dict[tuple[str, str], tuple[Sample, Sample]],
    iterations: int,
) -> str:
    settings = SETTINGS
    lines = [
        f"# Focused reads: {database}",
        "",
        f"{iterations} timed calls per case; `{harness.hardware().get('cpu', '')}`.",
        "",
        f"## Search (ms): ranking sized by the lexicon against "
        f"`store.reads.search_candidates` ({StoreSettings().reads.search_candidates})",
        "",
        f"`search.common_term_docs` {settings.common_term_docs}, "
        f"`search.common_term_candidates` {settings.common_term_candidates}; "
        f"{len(common)} common words, from {common[0][1] if common else 0:,} to "
        f"{common[-1][1] if common else 0:,} Records.",
        "",
        "| Query | before p50 | before p95 | before truncated | lexicon p50 | lexicon p95 "
        "| lexicon truncated |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, (before, after) in search.items():
        lines.append(
            f"| {name} | {_ms(statistics.median(before.samples_ms))} | {_ms(before.p(0.95))} "
            f"| {before.truncated} | {_ms(statistics.median(after.samples_ms))} "
            f"| {_ms(after.p(0.95))} | {after.truncated} |"
        )
    lines += [
        "",
        "## 2-hop neighborhood through Keeper (ms), with values and with `values: false`",
        "",
        "| Branch | Pages | values p50 | values p95 | headers p50 | headers p95 "
        "| values bytes | headers bytes |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for (branch, state), (full, headers) in hood.items():
        lines.append(
            f"| {branch} | {state} | {_ms(statistics.median(full.samples_ms))} "
            f"| {_ms(full.p(0.95))} | {_ms(statistics.median(headers.samples_ms))} "
            f"| {_ms(headers.p(0.95))} | {statistics.median(full.sizes):,.0f} "
            f"| {statistics.median(headers.sizes):,.0f} |"
        )
    return "\n".join([*lines, ""])


async def run(args: argparse.Namespace, url: str) -> str:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=url, statement_timeout_ms=0)
    try:
        await harness.remove_perf_branches(engine, runtime)
        work = await harness.workload(engine, runtime)
        common = await common_words(engine, SETTINGS.common_term_docs)
        search: dict[str, tuple[Sample, Sample]] = {}
        if not args.skip_search:
            search = await measure_search(
                engine,
                runtime,
                work,
                [w for w, _ in common],
                iterations=args.iterations,
                warmup=args.warmup,
            )
        hood: dict[tuple[str, str], tuple[Sample, Sample]] = {}
        if not args.skip_neighborhood:
            await _overlay(engine, runtime, create=True)
            try:
                hood = await measure_neighborhood(
                    url,
                    work,
                    ["main", OVERLAY],
                    iterations=args.iterations,
                    warmup=args.warmup,
                )
            finally:
                await _overlay(engine, runtime, create=False)
    finally:
        await engine.dispose()
    return render(args.database, common, search, hood, args.iterations)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="a database bench.py seeded")
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--common-term-docs", type=int, default=None)
    parser.add_argument("--common-term-candidates", type=int, default=None)
    parser.add_argument("--skip-search", action="store_true")
    parser.add_argument("--skip-neighborhood", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    global SETTINGS
    given = {
        "common_term_docs": args.common_term_docs,
        "common_term_candidates": args.common_term_candidates,
    }
    SETTINGS = SearchSettings(**{k: v for k, v in given.items() if v is not None})
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
