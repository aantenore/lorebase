"""Run Keeper: `python -m lorebase_keeper`. Host and port come from the `keeper` settings."""

from __future__ import annotations

import sys

import uvicorn

from lorebase_keeper.app import create_app
from lorebase_keeper.settings import keeper_settings
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, load_settings
from lorebase_platform.telemetry import setup_telemetry

SERVICE_NAME = "keeper"


def main() -> None:
    """Load settings, configure logging and telemetry, and serve until stopped."""
    try:
        settings = load_settings()
        keeper = keeper_settings(settings)
        configure_logging(settings.logging, service=SERVICE_NAME)
        telemetry = setup_telemetry(settings.telemetry)
        app = create_app(settings, telemetry=telemetry)
    except ConfigError as exc:
        sys.exit(str(exc))
    try:
        uvicorn.run(
            app,
            host=keeper.host,
            port=keeper.port,
            log_config=None,
            access_log=False,
            server_header=False,
            # The client address comes from `keeper.trusted_proxies` (client_address.py) only.
            proxy_headers=False,
        )
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
