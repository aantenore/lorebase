"""Run the read-path performance baseline and write `docs/performance.md`.

    uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md

Postgres comes from `LOREBASE_TEST_DATABASE_URL` or a disposable container
(`lorebase_store.testing`); the run uses a scratch database that is dropped afterwards.
Small scales (`--scale 5`) finish in seconds and are what `pytest -m perf` runs.

`--seed-database NAME` keeps the seeded dataset in database NAME of that server: the first run
seeds it, later runs at the same scale copy it (`CREATE DATABASE ... TEMPLATE`) instead of
loading it again, then apply the migrations missing from the copy. At 1M Records loading takes
far longer than measuring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness

from lorebase_store import migrations, testing
from lorebase_store.db import async_url, create_engine
from lorebase_store.layout import BranchLayout
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

DOC_TITLE = "# Performance baseline"
ANALYSIS = """\
- Both roles run the defaults: search, incoming Links, the neighborhood and attribute queries
  go through the security definer functions of revision `0005` (`lb_search`, `lb_links_in`,
  `lb_neighborhood`, `lb_query_by_attribute`; store schema notes), on an overlay their
  `_overlay` variants. The `rls` role gets the owner's index plans inside them, and every read
  of both roles is within its budget.
- With plain SQL (`store.reads.use_definer_functions: false`), a role subject to RLS cannot use
  conditions built from functions Postgres does not mark leakproof (`->>`, casts, `@@`, `%`)
  as index conditions: the policy is applied first. The plain SQL baseline of 2026-10-20
  measured, at p95 for that role, 28.06 ms for Links of a Record (inbound foreign-key Links
  filter every purchase order), 136.73 ms for the neighborhood and 68.14 ms for search (a scan
  of the Domain's partition).
- Key, page and stored-Link reads compare text columns only; text equality and ordering are
  leakproof, so they keep their index plans as plain SQL under row-level security.
- The functions build each statement with the Domain, branch and Kind as literals, so every
  call is planned with them (partial index predicates match) and runs without JIT; the
  planning time is part of the numbers above.
- Search reads the GIN index on `(domain_id, branch, search)` (revision `0011`,
  `btree_gin`): the Domain and branch are index conditions, so a search reads its branch's
  matches only, however many branches share the partition (here `main`, a copy and an
  overlay). It ranks at most `store.reads.search_candidates` matches, so very common terms
  cost a bounded amount. Typo-tolerant search (revision `0018`, Keeper's lexical search)
  queries a misspelled title word: the word is looked up in the branch's word lexicon
  (`mat_lexicon`, and the base's for an overlay), its closest words come from the trigram
  index on the lexicon, and the search runs with `(word | correction ...)`.
- Times come from a laptop shared with other work; repeated runs differ by a few milliseconds
  at p95, up to ten for typo-tolerant search.
- A copy branch of `main` is created as an overlay (the request) and copied by its background
  job: the Records, Links and history rows of `main` (about twice the Records and Links) in
  batches of `store.materialize.job_batch_rows`, each its own transaction, then the switch to a
  full copy, which queues the per-Attribute indexes. Its deletion hides it (the request) and
  its job deletes the rows in batches. Every transaction runs under the default
  `database.statement_timeout_ms`.
- An overlay branch (AC-16, store schema notes) is created by inserting its ref, copying the
  compiled schema row and queuing the builds of its per-Attribute indexes (`store.index`
  jobs, one per index): its cost does not depend on the size of the Domain. The job worker
  builds them concurrently; this run builds them before the reads below.
- Overlay reads resolve the overlay's own rows over the base rows as of the fork point in one
  statement: an index scan of each branch (the overlay's own per-Attribute indexes for its
  half), a primary-key probe of the overlay per base row read (search hashes the overlay's
  keys instead) and a one-row check that the ref is still that overlay. Keyset pages and
  attribute queries are cut per half; the neighborhood looks Links and Records up by key with
  one index scan over both branches (revision `0012`). `test_overlay_reads_keep_index_plans`
  checks the plans.
- Much of the overlay overhead left is planning: an overlay statement plans two or three
  scans of the partition where the copy plans one, and the planner checks each scan against
  every partial index of the partition (the per-Attribute indexes of `main`, the copy and the
  overlay: 42 here), about 0.2 ms more per statement. Search, attribute queries, Links of a
  Record and the neighborhood stay within 30% of the copy at p50, and every read far within
  its budget of design 8.7; `tests/perf/test_perf.py` asserts that search on the overlay
  stays within 25% of the copy at p50 at a small scale. Before revisions `0011` and `0012`
  and the overlay's own indexes, search cost +93 to +101% at p50 (+73 to +132% at p95) and
  the neighborhood +50%.
- The overlay and the copy below got the same commits; `main` then moved, and its commit
  copied the previous state of the rows it changed into the overlay (copy on write).
"""


@dataclass
class Options:
    scale: int
    extra_links: int
    iterations: int
    warmup: int
    branch_iterations: int
    commit_iterations: int
    commit_changes: int
    overlay_iterations: int
    overlay_changes: int
    seed_database: str | None
    out: Path | None


@dataclass
class Report:
    options: Options
    seeded: harness.SeedReport
    owner: dict[str, harness.Measurement]
    reader: dict[str, harness.Measurement]
    branch: harness.BranchJobs
    overlay_branch: harness.Measurement
    commits: harness.Measurement
    pair: harness.OverlayPair
    copy_reads: dict[str, dict[str, harness.Measurement]]
    overlay_reads: dict[str, dict[str, harness.Measurement]]
    plans: dict[str, dict[str, list[str]]]
    hardware: dict[str, str]
    server: dict[str, str]


def parse(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", type=int, default=540, help="generator scale (540: ~100k)")
    parser.add_argument("--extra-links", type=int, default=4, help="extra Links per Part")
    parser.add_argument("--iterations", type=int, default=300, help="timed calls per read")
    parser.add_argument("--warmup", type=int, default=30, help="untimed calls per read")
    parser.add_argument("--branch-iterations", type=int, default=3)
    parser.add_argument("--commit-iterations", type=int, default=5)
    parser.add_argument("--commit-changes", type=int, default=1000)
    parser.add_argument("--overlay-iterations", type=int, default=20)
    parser.add_argument(
        "--overlay-changes", type=int, default=1000, help="Part updates on each side of the fork"
    )
    parser.add_argument(
        "--seed-database",
        default=None,
        help="keep the seeded dataset in this database and reuse it (see module doc)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the markdown report here")
    args = parser.parse_args(argv)
    return Options(
        args.scale,
        args.extra_links,
        args.iterations,
        args.warmup,
        args.branch_iterations,
        args.commit_iterations,
        args.commit_changes,
        args.overlay_iterations,
        args.overlay_changes,
        args.seed_database,
        args.out,
    )


async def plans_of(
    engine: AsyncEngine,
    operations: dict[str, harness.Operation],
    role: str | None,
) -> dict[str, list[str]]:
    """Scan nodes of each read's statements (`node type [index]`), including the statements
    run inside the security definer functions, as `auto_explain` logs them for one call."""
    found: dict[str, list[str]] = {}
    async with async_sessionmaker(engine)() as session:
        for name, operation in operations.items():
            async with session.begin():
                async with testing.nested_plans(session) as plans:
                    if role is not None:
                        await session.execute(text(f"SET LOCAL ROLE {role}"))
                    await scope_to_domain(session, harness.DOMAIN)
                    await operation(session, random.Random(3))
                found[name] = sorted({node for plan in plans for node in _scans(plan)})
    return found


def _scans(plan: dict[str, Any]) -> list[str]:
    nodes = []
    if "Relation Name" in plan or "Index Name" in plan:
        target = plan.get("Index Name") or plan.get("Relation Name")
        nodes.append(plan["Node Type"] + (f" {target}" if target else ""))
    for child in plan.get("Plans", []):
        nodes.extend(_scans(child))
    return nodes


def _engine(url: str) -> AsyncEngine:
    """Without a statement timeout: loading the dataset commits 10,000 changes at a time."""
    return create_engine(DatabaseSettings(), url=url, statement_timeout_ms=0)


def _timed_engine(url: str) -> AsyncEngine:
    """With the default `database.statement_timeout_ms`, as Keeper and Scribe run: branch
    copies and deletions must fit in it."""
    return create_engine(DatabaseSettings(), url=url)


async def seed_only(options: Options, url: str) -> harness.SeedReport:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = _engine(url)
    try:
        return await harness.seed(
            engine, runtime, harness.Scale(options.scale, options.extra_links)
        )
    finally:
        await engine.dispose()


async def benchmark(options: Options, url: str, seeded: harness.SeedReport | None) -> Report:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = _engine(url)
    try:
        if seeded is None:
            scale = harness.Scale(options.scale, options.extra_links)
            seeded = await harness.seed(engine, runtime, scale)
        work = await harness.workload(engine, runtime)
        main = BranchLayout(harness.DOMAIN, "main")
        operations = harness.read_operations(work, runtime, main)
        await harness.create_reader(engine)
        common = {"iterations": options.iterations, "warmup": options.warmup}
        owner = await harness.measure_reads(engine, operations, role=None, **common)
        reader = await harness.measure_reads(engine, operations, role=harness.READER_ROLE, **common)
        plans = {
            "owner": await plans_of(engine, operations, None),
            "rls": await plans_of(engine, operations, harness.READER_ROLE),
        }
        timed = _timed_engine(url)
        try:
            branch = await harness.measure_branch_copy(
                timed, runtime, iterations=options.branch_iterations
            )
        finally:
            await timed.dispose()
        commits = await harness.measure_commits(
            engine,
            runtime,
            work,
            changes=options.commit_changes,
            iterations=options.commit_iterations,
        )
        overlay_branch = await harness.measure_branch_overlay(
            engine, runtime, iterations=options.overlay_iterations
        )
        pair = await harness.overlay_pair(engine, runtime, work, changes=options.overlay_changes)
        copy_reads: dict[str, dict[str, harness.Measurement]] = {}
        overlay_reads: dict[str, dict[str, harness.Measurement]] = {}
        copy_ops = harness.read_operations(work, runtime, pair.copy)
        overlay_ops = harness.read_operations(work, runtime, pair.overlay)
        for label, role in (("owner", None), ("rls", harness.READER_ROLE)):
            # Alternating call by call, with the same keys, so both branches see the same
            # machine load.
            copy_reads[label], overlay_reads[label] = {}, {}
            for name in copy_ops:
                (
                    copy_reads[label][name],
                    overlay_reads[label][name],
                ) = await harness.measure_alternating(
                    engine, name, [copy_ops[name], overlay_ops[name]], role=role, **common
                )
        server = await harness.server_notes(engine)
        await harness.drop_reader(engine)
    finally:
        await engine.dispose()
    by_op: dict[str, dict[str, list[str]]] = {
        name: {role: plans[role][name] for role in plans} for name in operations
    }
    return Report(
        options,
        seeded,
        owner,
        reader,
        branch,
        overlay_branch,
        commits,
        pair,
        copy_reads,
        overlay_reads,
        by_op,
        harness.hardware(),
        server,
    )


def _ms(value: float) -> str:
    return f"{value:.2f}"


def render(report: Report) -> str:
    o = report.options
    lines = [
        DOC_TITLE,
        "",
        'Latency of the agent read paths on a materialized head (design 8, "Agent read '
        'budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that '
        "scopes row-level security to the Domain and runs one store primitive "
        "(`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a "
        "local socket, so they include the driver and the RLS scoping round trip.",
        "",
        "## Dataset",
        "",
        f"- `supply` Domain from `examples/supply/scripts/make_supply_data.py --scale {o.scale}`"
        f", plus {o.extra_links} extra `supplied_by` and `stocked_at` Links per Part.",
        f"- {report.seeded.records:,} Records and {report.seeded.links:,} Links on `main`, "
        f"loaded through the commit pipeline in {report.seeded.commits} commits "
        f"({report.seeded.seconds:.0f} s, then `VACUUM ANALYZE`).",
        f"- {o.iterations} timed calls per read after {o.warmup} warm-up calls, random keys "
        "from the dataset.",
        "",
        "## Environment",
        "",
        *(f"- {k}: {v}" for k, v in {**report.hardware, **report.server}.items()),
        f"- run: {datetime.now(UTC).strftime('%Y-%m-%d')}, Postgres in Docker on the same "
        "machine, container defaults except the settings above.",
        "",
        "## Reads (milliseconds)",
        "",
        "`owner`: a role not subject to row-level security (superuser in the container). "
        "`rls`: a role subject to the `domain_isolation` policies, like the application role "
        "in production.",
        "",
        "| Operation | Target p95 | owner p50 | owner p95 | owner p99 | rls p50 | rls p95 "
        "| rls p99 |",
        "|-----------|-----------:|----------:|----------:|----------:|--------:|--------:"
        "|--------:|",
    ]
    for name, target in harness.TARGETS_MS.items():
        a, b = report.owner[name], report.reader[name]
        mark = "\\*" if name in harness.PROPOSED_TARGETS else ""
        lines.append(
            f"| {name} | {_ms(target)}{mark} | {_ms(a.p50)} | {_ms(a.p95)} | {_ms(a.p99)} "
            f"| {_ms(b.p50)} | {_ms(b.p95)} | {_ms(b.p99)} |"
        )
    lines += [
        "",
        "\\* No budget in design 8.7; held to the budget of Links of a Record, the other "
        "per-Record index range read.",
    ]
    lines += [
        "",
        "## Writes (milliseconds)",
        "",
        "| Operation | Runs | p50 | p95 | p99 | max |",
        "|-----------|-----:|----:|----:|----:|----:|",
        *(
            f"| {m.name} | {len(m.samples_ms)} | {_ms(m.p50)} | {_ms(m.p95)} | {_ms(m.p99)} "
            f"| {_ms(max(m.samples_ms))} |"
            for m in (*report.branch.all(), report.overlay_branch, report.commits)
        ),
        "",
        "A copy branch of `main` answers at once as an overlay; its background job copies "
        "the rows in batches of `store.materialize.job_batch_rows` "
        "(each its own transaction, under the default `database.statement_timeout_ms`), then "
        "switches it to a full copy. Deleting it hides it at once; its job deletes the rows in "
        "batches. Runs is the number of copies, or of batch transactions.",
        "",
        *_overlay_section(report),
        "## Plans",
        "",
        "Scan nodes of the statements of one call of each read, statements run inside the "
        "read functions included (`auto_explain`).",
        "",
        "| Operation | owner | rls |",
        "|-----------|-------|-----|",
        *(
            f"| {name} | {'<br>'.join(plans['owner'])} | {'<br>'.join(plans['rls'])} |"
            for name, plans in report.plans.items()
        ),
        "",
        "## Reading the results",
        "",
        *ANALYSIS.splitlines(),
        "",
        "Regenerate with:",
        "",
        "```sh",
        f"uv run python tests/perf/bench.py --scale {o.scale} --extra-links {o.extra_links} "
        "--out docs/performance.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def _overhead(copy: float, overlay: float) -> str:
    return f"{(overlay / copy - 1) * 100:+.0f}%"


def _overlay_section(report: Report) -> list[str]:
    o, pair = report.options, report.pair
    lines = [
        "## Overlay branches (milliseconds)",
        "",
        f"`{harness.OVERLAY_BRANCH}` (overlay) and `{harness.COPY_BRANCH}` (copy) forked from "
        f"`main` and got the same commit of {o.overlay_changes} Part updates; `main` then "
        f"committed {o.overlay_changes} other Part updates. The overlay stores "
        f"{pair.overlay_rows:,} rows (its changes, its derived and copied-on-write rows), the "
        f"copy {pair.copy_rows:,}. Reads alternate between the two branches call by call, "
        f"with the same random keys, {o.iterations} timed calls each.",
        "",
        "| Operation | Role | copy p50 | overlay p50 | copy p95 | overlay p95 | copy p99 "
        "| overlay p99 | overhead p50 | overhead p95 |",
        "|-----------|------|---------:|------------:|---------:|------------:|---------:"
        "|------------:|-------------:|-------------:|",
    ]
    for name in harness.TARGETS_MS:
        for role in ("owner", "rls"):
            c, v = report.copy_reads[role][name], report.overlay_reads[role][name]
            lines.append(
                f"| {name} | {role} | {_ms(c.p50)} | {_ms(v.p50)} | {_ms(c.p95)} | {_ms(v.p95)} "
                f"| {_ms(c.p99)} | {_ms(v.p99)} "
                f"| {_overhead(c.p50, v.p50)} | {_overhead(c.p95, v.p95)} |"
            )
    return [*lines, ""]


async def _admin(server_url: str, statement: str, **params: Any) -> Any:
    engine = create_async_engine(
        async_url(server_url), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), params)
            return result.scalar() if result.returns_rows else None
    finally:
        await engine.dispose()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _seeded_database(options: Options, server: str, name: str) -> harness.SeedReport:
    """The seed report of database `name`, seeding it first when it does not exist."""
    found = asyncio.run(
        _admin(
            server,
            "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :n",
            n=name,
        )
    )
    wanted = {"scale": options.scale, "extra_links": options.extra_links}
    if found is not None:
        kept = json.loads(found)
        if kept["scale"] != wanted:
            raise SystemExit(f"database {name} holds {kept['scale']}, not {wanted}")
        return harness.SeedReport(**kept["seeded"])
    asyncio.run(_admin(server, f"CREATE DATABASE {_quoted(name)}"))
    url = async_url(server).set(database=name).render_as_string(hide_password=False)
    migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
    seeded = asyncio.run(seed_only(options, url))
    note = json.dumps({"scale": wanted, "seeded": asdict(seeded)}).replace("'", "''")
    asyncio.run(_admin(server, f"COMMENT ON DATABASE {_quoted(name)} IS '{note}'"))
    return seeded


@contextmanager
def _copy_of(server: str, template: str) -> Iterator[str]:
    """A scratch copy of database `template`, dropped afterwards."""
    with testing.scratch_database(server) as url:
        name = async_url(url).database
        assert name is not None
        asyncio.run(_admin(server, f"DROP DATABASE {_quoted(name)}"))
        asyncio.run(_admin(server, f"CREATE DATABASE {_quoted(name)} TEMPLATE {_quoted(template)}"))
        yield url


def main(argv: Sequence[str] | None = None) -> Report:
    options = parse(argv)
    with testing.postgres_server() as server:
        seeded = None
        if options.seed_database is not None:
            seeded = _seeded_database(options, server, options.seed_database)
            database = _copy_of(server, options.seed_database)
        else:
            database = testing.scratch_database(server)
        with database as url:
            migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
            report = asyncio.run(benchmark(options, url, seeded))
    markdown = render(report)
    print(markdown)
    if options.out is not None:
        options.out.write_text(markdown, encoding="utf-8")
    return report


if __name__ == "__main__":
    main()
