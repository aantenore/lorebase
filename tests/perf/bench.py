"""Run the read-path performance baseline and write `docs/performance.md`.

    uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md

Postgres comes from `LOREBASE_TEST_DATABASE_URL` or a disposable container
(`lorebase_store.testing`); the run uses a scratch database that is dropped afterwards.
Small scales (`--scale 5`) finish in seconds and are what `pytest -m perf` runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness

from lorebase_store import migrations, testing
from lorebase_store.db import create_engine
from lorebase_store.repositories import scope_to_domain
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

DOC_TITLE = "# Performance baseline"
ANALYSIS = """\
- With the owner role every read is within its budget, and every plan is an index scan.
- Key, page and stored-Link reads compare text columns only; text equality and ordering are
  leakproof, so they keep their index plans under row-level security.
- Per-Attribute, foreign-key and full-text conditions call functions Postgres does not mark
  leakproof (`->>`, casts, `@@`). With a role subject to RLS the planner applies the policy
  first and cannot use them as index conditions: attribute queries filter the Kind's rows,
  search scans the Domain's partition, and inbound foreign-key Links (`issued_to`: the
  purchase orders of a supplier) filter every purchase order. That is what pushes Links of a
  Record, the neighborhood and search over budget in the `rls` columns. Design 8.7 ("Agent
  read budgets") lists the options.
- Search ranks at most `store.reads.search_candidates` matches, so very common terms cost a
  bounded amount.
- Branch creation copies the Records, Links and history rows of `main` (about twice the Records
  and Links) and builds the new branch's per-Attribute indexes in the same transaction.
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
    out: Path | None


@dataclass
class Report:
    options: Options
    seeded: harness.SeedReport
    owner: dict[str, harness.Measurement]
    reader: dict[str, harness.Measurement]
    branch: harness.Measurement
    commits: harness.Measurement
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
        args.out,
    )


async def plans_of(
    engine: AsyncEngine,
    operations: dict[str, harness.Operation],
    role: str | None,
) -> dict[str, list[str]]:
    """Scan nodes of each read's statements (`node type [index]`), from EXPLAIN."""
    statements: list[tuple[str, Any]] = []

    def record(_c: Any, _cur: Any, statement: str, parameters: Any, *_: Any) -> None:
        statements.append((statement, parameters))

    found: dict[str, list[str]] = {}
    async with async_sessionmaker(engine)() as session:
        for name, operation in operations.items():
            statements.clear()
            async with session.begin():
                if role is not None:
                    await session.execute(text(f"SET LOCAL ROLE {role}"))
                await scope_to_domain(session, harness.DOMAIN)
                event.listen(engine.sync_engine, "before_cursor_execute", record)
                try:
                    await operation(session, random.Random(3))
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", record)
                connection = await session.connection()
                nodes: list[str] = []
                for sql, params in statements:
                    if "set_config" in sql or sql.startswith("SET "):
                        continue
                    raw = (
                        await connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {sql}", params)
                    ).scalar_one()
                    plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
                    nodes.extend(_scans(plan))
                found[name] = sorted(set(nodes))
    return found


def _scans(plan: dict[str, Any]) -> list[str]:
    nodes = []
    if "Relation Name" in plan or "Index Name" in plan:
        index = plan.get("Index Name")
        nodes.append(plan["Node Type"] + (f" {index}" if index else ""))
    for child in plan.get("Plans", []):
        nodes.extend(_scans(child))
    return nodes


async def benchmark(options: Options, url: str) -> Report:
    runtime = StoreRuntime.from_settings(StoreSettings())
    engine = create_engine(DatabaseSettings(), url=url)
    try:
        scale = harness.Scale(options.scale, options.extra_links)
        seeded = await harness.seed(engine, runtime, scale)
        work = await harness.workload(engine, runtime)
        operations = harness.read_operations(work, runtime)
        await harness.create_reader(engine)
        common = {"iterations": options.iterations, "warmup": options.warmup}
        owner = await harness.measure_reads(engine, operations, role=None, **common)
        reader = await harness.measure_reads(engine, operations, role=harness.READER_ROLE, **common)
        plans = {
            "owner": await plans_of(engine, operations, None),
            "rls": await plans_of(engine, operations, harness.READER_ROLE),
        }
        branch = await harness.measure_branch_copy(
            engine, runtime, iterations=options.branch_iterations
        )
        commits = await harness.measure_commits(
            engine,
            runtime,
            work,
            changes=options.commit_changes,
            iterations=options.commit_iterations,
        )
        server = await harness.server_notes(engine)
        await harness.drop_reader(engine)
    finally:
        await engine.dispose()
    by_op: dict[str, dict[str, list[str]]] = {
        name: {role: plans[role][name] for role in plans} for name in operations
    }
    return Report(
        options, seeded, owner, reader, branch, commits, by_op, harness.hardware(), server
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
        "machine, default container settings.",
        "",
        "## Reads (milliseconds)",
        "",
        "`owner`: a role not subject to row-level security (superuser in the container). "
        "`rls`: a role subject to the `domain_isolation` policies, like the application role "
        "in production.",
        "",
        "| Operation | Target p95 | owner p50 | owner p95 | rls p50 | rls p95 |",
        "|-----------|-----------:|----------:|----------:|--------:|--------:|",
    ]
    for name, target in harness.TARGETS_MS.items():
        a, b = report.owner[name], report.reader[name]
        lines.append(
            f"| {name} | {_ms(target)} | {_ms(a.p50)} | {_ms(a.p95)} | {_ms(b.p50)} "
            f"| {_ms(b.p95)} |"
        )
    lines += [
        "",
        "## Writes (milliseconds)",
        "",
        "| Operation | Runs | p50 | p95 |",
        "|-----------|-----:|----:|----:|",
        *(
            f"| {m.name} | {len(m.samples_ms)} | {_ms(m.p50)} | {_ms(m.p95)} |"
            for m in (report.branch, report.commits)
        ),
        "",
        "## Plans",
        "",
        "Scan nodes of each read's statements (EXPLAIN with the parameters of one call).",
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


def main(argv: Sequence[str] | None = None) -> Report:
    options = parse(argv)
    with testing.postgres_server() as server, testing.scratch_database(server) as url:
        migrations.upgrade(database=DatabaseSettings(), store=StoreSettings(), url=url)
        report = asyncio.run(benchmark(options, url))
    markdown = render(report)
    print(markdown)
    if options.out is not None:
        options.out.write_text(markdown, encoding="utf-8")
    return report


if __name__ == "__main__":
    main()
