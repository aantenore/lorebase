"""Run Guide: `lorebase-guide` or `python -m lorebase_guide`.

Host, port and everything else come from the `guide` and `inference` settings.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from lorebase_guide.app import create_app
from lorebase_guide.settings import guide_settings
from lorebase_inference import inference_settings
from lorebase_platform.logging import configure_logging
from lorebase_platform.settings import ConfigError, load_settings
from lorebase_platform.telemetry import setup_telemetry

SERVICE_NAME = "guide"


def main() -> None:
    """Load settings, configure logging and telemetry, and serve until stopped."""
    try:
        settings = load_settings()
        guide = guide_settings(settings)
        inference = inference_settings(settings)
    except ConfigError as exc:
        sys.exit(str(exc))
    configure_logging(settings.logging, service=SERVICE_NAME)
    # httpx logs every request URL at INFO; keep it quiet so record keys stay out of the logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    telemetry = setup_telemetry(settings.telemetry)
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
