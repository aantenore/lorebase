"""Keeper application factory."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from lorebase_keeper import __version__
from lorebase_keeper.errors import Problem, ProblemRenderer, install_error_handlers
from lorebase_keeper.health import router as health_router
from lorebase_keeper.middleware import BodyLimitMiddleware, RequestContextMiddleware
from lorebase_keeper.readiness import MigrationHeads, ReadinessCheck, default_checks
from lorebase_keeper.settings import KeeperSettings, keeper_settings
from lorebase_platform.settings import ConfigError, Settings, load_settings
from lorebase_platform.telemetry import Telemetry

_DEFAULT_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {
        "model": Problem,
        "description": "Problem details (RFC 9457), media type application/problem+json.",
    }
}


def create_app(
    settings: Settings | None = None,
    *,
    telemetry: Telemetry | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
    migration_heads: MigrationHeads | None = None,
) -> FastAPI:
    """Build the Keeper ASGI application.

    `settings` default to `load_settings()`. `telemetry` supplies the providers used for
    spans and metrics (none: the global OpenTelemetry API). `readiness_checks` replaces the
    default checks, which probe the database named by the settings; `migration_heads` tells
    that probe which Alembic revisions the code expects.
    """
    settings = settings or load_settings()
    keeper = keeper_settings(settings)
    _require_supported_auth(keeper)
    renderer = ProblemRenderer(keeper.problem_type_base)
    checks = (
        list(readiness_checks)
        if readiness_checks is not None
        else default_checks(
            settings,
            section=keeper.readiness.database_section,
            dsn_key=keeper.readiness.database_dsn_key,
            timeout_s=keeper.readiness.timeout_s,
            expected_heads=migration_heads,
        )
    )

    docs = keeper.openapi.docs_enabled
    app = FastAPI(
        title=keeper.openapi.title,
        description=keeper.openapi.description,
        version=__version__,
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        openapi_url=f"{keeper.api_prefix}/openapi.json" if docs else None,
        docs_url=f"{keeper.api_prefix}/docs" if docs else None,
        redoc_url=None,
        responses=_DEFAULT_RESPONSES,
    )
    app.state.settings = settings
    app.state.keeper_settings = keeper
    app.state.readiness_checks = checks

    install_error_handlers(app, renderer)
    app.include_router(health_router)

    # Innermost first: the last middleware added is the outermost one.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=keeper.limits.max_request_body_bytes, renderer=renderer
    )
    if keeper.cors.allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(keeper.cors.allow_origins),
            allow_methods=list(keeper.cors.allow_methods),
            allow_headers=[*keeper.cors.allow_headers, keeper.request_id_header],
            expose_headers=[keeper.request_id_header],
            allow_credentials=keeper.cors.allow_credentials,
            max_age=keeper.cors.max_age_s,
        )
    app.add_middleware(
        RequestContextMiddleware,
        header=keeper.request_id_header,
        renderer=renderer,
        tracer_provider=telemetry.tracer_provider if telemetry else None,
        meter_provider=telemetry.meter_provider if telemetry else None,
    )
    return app


def _require_supported_auth(keeper: KeeperSettings) -> None:
    if keeper.auth.mode != "none":
        raise ConfigError(
            [f"keeper.auth.mode: {keeper.auth.mode!r} is not available yet, only 'none' is"]
        )
