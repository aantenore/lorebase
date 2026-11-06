"""Run Herald: `lorebase-herald` or `python -m lorebase_herald`.

lorebase-herald               serve until the host disconnects (stdio) or until stopped
lorebase-herald --version     print the version and exit
lorebase-herald --help        print this text and exit

The transport (stdio or Streamable HTTP) and everything else come from the `herald` settings:
the YAML file named by LOREBASE_CONFIG, `LOREBASE__HERALD__...` variables and these aliases,
which MCP host configurations usually set:

  LOREBASE_URL                  Keeper origin, e.g. http://127.0.0.1:8080 (herald.keeper.base_url)
  LOREBASE_API_KEY              key of the agent's service principal, on stdio; Streamable HTTP
                                forwards each caller's own key (herald.keeper.api_key)
  LOREBASE_DOMAIN               Domain used when a call names none (herald.domain)
  LOREBASE_BRANCH               branch used when a call names none (herald.branch)
  LOREBASE__HERALD__TRANSPORT   stdio (default) or streamable_http (herald.transport)

Logs go to stderr: on stdio, stdout carries the MCP protocol. The first line there names the
transport, the Keeper reached and the default Domain. Documentation:
https://aantenore.github.io/lorebase/mcp/
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Sequence

from lorebase_herald import __version__
from lorebase_herald.keeper import KeeperClients
from lorebase_herald.server import serve
from lorebase_herald.settings import herald_settings
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, load_settings

SERVICE_NAME = "herald"

log = logging.getLogger("lorebase.herald")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lorebase-herald",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"lorebase-herald {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Answer `--help` and `--version`, else load settings, configure logging and serve until
    the host disconnects."""
    _parser().parse_args(argv)
    try:
        settings = load_settings()
        herald = herald_settings(settings)
    except ConfigError as exc:
        sys.exit(str(exc))
    configure_logging(settings.logging, service=SERVICE_NAME, stream=sys.stderr)
    # httpx logs every Keeper request URL at INFO; keep it quiet so record keys stay out of the
    # logs (on stdio they would land in the agent's stderr).
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    endpoint = None
    if herald.transport == "streamable_http":
        endpoint = f"http://{herald.http.host}:{herald.http.port}{herald.http.path}"
    # A host that points Herald at the wrong Keeper otherwise learns it one failed call at a
    # time: say where the calls go before the first one.
    log.info(
        "herald %s starting: %s transport, Keeper at %s, Domain %s",
        __version__,
        herald.transport,
        herald.keeper.base_url,
        herald.domain or "none (each call names one)",
        extra={
            "version": __version__,
            "transport": herald.transport,
            "keeper": herald.keeper.base_url,
            "domain": herald.domain,
            "endpoint": endpoint,
        },
    )
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(herald, KeeperClients(herald.keeper)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
