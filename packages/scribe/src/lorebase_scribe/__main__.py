"""Run Scribe: `lorebase-scribe` (or `python -m lorebase_scribe`).

lorebase-scribe                      work and schedule until SIGTERM or SIGINT
lorebase-scribe once [--limit N]     run the due jobs, then exit
lorebase-scribe sync DOMAIN SOURCE [--dry-run]
                                     synchronize one Source now and print the report
lorebase-scribe embeddings DOMAIN [--branch B]... [--reindex] [--prune]
                                     compute missing embeddings now (backfill) and print
                                     the report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence

from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, Settings, load_settings
from lorebase_platform.telemetry import setup_telemetry
from lorebase_scribe.service import open_scribe
from lorebase_scribe.sources import SourceSync

SERVICE_NAME = "scribe"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lorebase-scribe", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="Work and schedule until stopped (default).")
    once = commands.add_parser("once", help="Run the due jobs, then exit.")
    once.add_argument("--limit", type=int, default=None, help="Stop after this many jobs.")
    sync = commands.add_parser("sync", help="Synchronize one Source now.")
    sync.add_argument("domain")
    sync.add_argument("source", help="Source id or api name.")
    sync.add_argument("--dry-run", action="store_true", help="Compute the delta only.")
    embed = commands.add_parser(
        "embeddings", help="Embed the Records that lack a vector of the configured model now."
    )
    embed.add_argument("domain")
    embed.add_argument(
        "--branch",
        action="append",
        dest="branches",
        default=None,
        help="Only this branch (repeatable); default: every materialized branch.",
    )
    embed.add_argument(
        "--reindex",
        action="store_true",
        help="Render every text again and re-embed those that changed.",
    )
    embed.add_argument(
        "--prune",
        action="store_true",
        help="After a complete pass, delete the vectors of every other model.",
    )
    return parser


async def _serve(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    async with open_scribe(settings) as scribe:
        await scribe.serve(stop)


async def _once(settings: Settings, limit: int | None) -> int:
    async with open_scribe(settings) as scribe:
        return await scribe.worker.run_once(limit=limit)


async def _sync(settings: Settings, domain: str, source: str, dry_run: bool) -> dict[str, object]:
    async with open_scribe(settings) as scribe:
        context = scribe.context
        sync = SourceSync(
            context.sessions,
            context.runtime,
            context.settings.sources,
            principal=context.settings.worker.principal,
            connectors=context.connectors,
        )
        return (await sync.run(domain, source, dry_run=dry_run)).as_dict()


async def _embeddings(
    settings: Settings,
    domain: str,
    branches: list[str] | None,
    reindex: bool,
    prune: bool,
) -> dict[str, object]:
    """Run passes until the Domain is complete (each pass bounded like a job)."""
    async with open_scribe(settings) as scribe:
        embedding = scribe.context.embedding_pass()
        reports: list[dict[str, object]] = []
        resume: tuple[str, str, str] | None = None
        while True:
            report = await embedding.run(
                domain, branches=branches, reindex=reindex, resume=resume, prune=prune
            )
            reports.append(report.as_dict())
            if report.busy or report.resume is None:
                break
            resume = report.resume
        return {"passes": reports, "complete": report.complete}


def main(argv: Sequence[str] | None = None) -> int:
    """Load settings, configure logging and telemetry, then run the command."""
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.exit(str(exc))
    configure_logging(settings.logging, service=SERVICE_NAME)
    telemetry = setup_telemetry(settings.telemetry)
    try:
        if args.command == "once":
            ran = asyncio.run(_once(settings, args.limit))
            sys.stdout.write(json.dumps({"jobs_run": ran}) + "\n")
        elif args.command == "embeddings":
            result = asyncio.run(
                _embeddings(settings, args.domain, args.branches, args.reindex, args.prune)
            )
            sys.stdout.write(json.dumps(result, default=str) + "\n")
            return 0 if result["complete"] else 1
        elif args.command == "sync":
            report = asyncio.run(_sync(settings, args.domain, args.source, args.dry_run))
            sys.stdout.write(json.dumps(report, default=str) + "\n")
        else:
            asyncio.run(_serve(settings))
    finally:
        telemetry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
