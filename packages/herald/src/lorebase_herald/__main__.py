"""Run Herald: `lorebase-herald` or `python -m lorebase_herald`.

The transport (stdio or Streamable HTTP) and everything else come from the `herald` settings.
Logs go to stderr: on stdio, stdout carries the MCP protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from lorebase_herald.keeper_http import KeeperHttp
from lorebase_herald.server import serve
from lorebase_herald.settings import herald_settings
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, load_settings

SERVICE_NAME = "herald"


def main() -> None:
    """Load settings, configure logging and serve until the host disconnects."""
    try:
        settings = load_settings()
        herald = herald_settings(settings)
    except ConfigError as exc:
        sys.exit(str(exc))
    configure_logging(settings.logging, service=SERVICE_NAME, stream=sys.stderr)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(herald, KeeperHttp(herald.keeper)))


if __name__ == "__main__":
    main()
