"""Run Scribe: `lorebase-scribe` (or `python -m lorebase_scribe`).

lorebase-scribe                      work and schedule until SIGTERM or SIGINT
lorebase-scribe once [--limit N]     run the due jobs, then exit
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

SERVICE_NAME = "scribe"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lorebase-scribe", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="Work and schedule until stopped (default).")
    once = commands.add_parser("once", help="Run the due jobs, then exit.")
    once.add_argument("--limit", type=int, default=None, help="Stop after this many jobs.")
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
        else:
            asyncio.run(_serve(settings))
    finally:
        telemetry.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
