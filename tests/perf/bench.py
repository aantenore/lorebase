"""Run the read-path performance baseline and write `docs/performance.md`.

    uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md

Postgres comes from `LOREBASE_TEST_DATABASE_URL` or a disposable container
(`lorebase_store.testing`); the run uses a scratch database that is dropped afterwards.
Small scales (`--scale 5`) finish in seconds and are what `pytest -m perf` runs.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
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
- Both roles run the defaults: search, incoming Links, the neighborhood and attribute queries
  go through the security definer functions of revision `0004` (`lb_search`, `lb_links_in`,
  `lb_neighborhood`, `lb_query_by_attribute`; store schema notes). The `rls` role gets the
  owner's index plans inside them, and every read of both roles is within its budget.
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
- Search ranks at most `store.reads.search_candidates` matches, so very common terms cost a
  bounded amount. Typo-tolerant titles (trigram similarity, Keeper's lexical search) add the
  matches of the trigram index on titles to the candidates.
- Times come from a laptop shared with other work; repeated runs differ by a few milliseconds
  at p95, up to ten for typo-tolerant search.
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
