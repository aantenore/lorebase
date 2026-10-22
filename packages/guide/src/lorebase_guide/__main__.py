"""Run Guide: `lorebase-guide` or `python -m lorebase_guide`.

Host, port and everything else come from the `guide` and `inference` settings.

lorebase-guide                 serve until stopped
lorebase-guide modes clear     forget the structured output modes remembered per chat endpoint
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from lorebase_guide.app import create_app
from lorebase_guide.modes import FileModeMemory, mode_memory_path
from lorebase_guide.settings import guide_settings
from lorebase_inference import inference_settings
from lorebase_inference.chat import use_mode_memory
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, load_settings
from lorebase_platform.telemetry import setup_telemetry

SERVICE_NAME = "guide"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lorebase-guide", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="Serve until stopped (default).")
    modes = commands.add_parser("modes", help="Structured output modes of the chat endpoints.")
    modes.add_argument("action", choices=["clear"], help="Forget every remembered mode.")
    return parser


def _clear(path: Path) -> int:
    try:
        return FileModeMemory(path).clear()
    except OSError as exc:
        sys.exit(f"cannot clear {path}: {exc}")


def main(argv: Sequence[str] | None = None) -> None:
    """Load settings, configure logging and telemetry, and serve until stopped (or run the
    command)."""
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
        guide = guide_settings(settings)
        inference = inference_settings(settings)
    except ConfigError as exc:
        sys.exit(str(exc))
    if args.command == "modes":
        path = mode_memory_path(guide.data_dir, guide.mode_memory_file)
        cleared = 0 if path is None else _clear(path)
        sys.stdout.write(json.dumps({"cleared": cleared}) + "\n")
        return
    configure_logging(settings.logging, service=SERVICE_NAME)
    # httpx logs every request URL at INFO; keep it quiet so record keys stay out of the logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    telemetry = setup_telemetry(settings.telemetry)
    modes = mode_memory_path(guide.data_dir, guide.mode_memory_file)
    if modes is not None:
        use_mode_memory(
            FileModeMemory(modes),
            secret=(
                None
                if inference.mode_memory_key is None
                else inference.mode_memory_key.get_secret_value()
            ),
            max_age_s=inference.mode_memory_ttl_s,
        )
    try:
        uvicorn.run(
            create_app(guide, inference),
            host=guide.host,
            port=guide.port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
        )
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
