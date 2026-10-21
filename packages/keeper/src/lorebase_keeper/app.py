"""Keeper application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from starlette.middleware.cors import CORSMiddleware

from lorebase_keeper import __version__
from lorebase_keeper.auth.audit import AuditMiddleware
from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.auth.runtime import build_runtime
from lorebase_keeper.client_address import install as install_client_addresses
from lorebase_keeper.db import create_database
from lorebase_keeper.errors import Problem, ProblemRenderer, install_error_handlers
from lorebase_keeper.health import router as health_router
from lorebase_keeper.middleware import BodyLimitMiddleware, RequestContextMiddleware
from lorebase_keeper.ratelimit.enforce import (
    EXPOSED_HEADERS,
    RateLimitHeadersMiddleware,
    build_limits,
    enforce_limits,
)
from lorebase_keeper.readiness import (
    MigrationHeads,
    ReadinessCheck,
    ReplicaCheck,
    default_checks,
    packaged_heads,
)
from lorebase_keeper.routes.admin import router as admin_router
from lorebase_keeper.routes.domain_api import domain_routers
from lorebase_keeper.routes.whoami import router as whoami_router
from lorebase_keeper.settings import keeper_settings
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
    auth_store: AuthStore | None = None,
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Build the Keeper ASGI application.

    `settings` default to `load_settings()`. `telemetry` supplies the providers used for
    spans and metrics (none: the global OpenTelemetry API). `readiness_checks` replaces the
    default checks, which probe the database named by the settings; `migration_heads` tells
    that probe which Alembic revisions the code expects (default: the heads of the store's
    migrations; a callable returning nothing skips the comparison).

    Authentication needs a database: the `database` section (name from
    `keeper.readiness.database_section`), or `auth_store` when given. With `keeper.auth.mode:
    none` the database is optional and only enables the audit log. `http_client` serves
    identity provider requests (default: a client built from the OIDC settings) and `clock` the
    expiry checks.
    """
    settings = settings or load_settings()
    keeper = keeper_settings(settings)
    renderer = ProblemRenderer(keeper.problem_type_base)
    checks = (
        list(readiness_checks)
        if readiness_checks is not None
        else default_checks(
            settings,
            section=keeper.readiness.database_section,
            dsn_key=keeper.readiness.database_dsn_key,
            timeout_s=keeper.readiness.timeout_s,
            expected_heads=migration_heads or packaged_heads,
        )
    )

    database = (
        create_database(
            settings,
            keeper.readiness.database_section,
            meter_provider=telemetry.meter_provider if telemetry else None,
        )
        if auth_store is None
        else None
    )
    replicas = database.reads.replicas if database and database.reads else None
    if readiness_checks is None and database is not None and database.reads and replicas:
        checks.append(
            ReplicaCheck(
                replicas,
                timeout_s=database.reads.probe_timeout_s,
                max_lag_ms=database.reads.max_lag_ms,
                required=keeper.reads.prefer_replica and not database.reads.fallback_to_primary,
            )
        )
    store = auth_store or (database.auth_store if database else None)
    if keeper.auth.mode != "none" and store is None:
        raise ConfigError(
            [
                f"keeper.auth.mode: {keeper.auth.mode!r} needs a database; configure the "
                f"{keeper.readiness.database_section!r} section"
            ]
        )
    auth = build_runtime(
        keeper.auth,
        store=store,
        http_client=http_client,
        clock=clock,
        meter_provider=telemetry.meter_provider if telemetry else None,
    )

    limits = build_limits(
        keeper.limits.rate,
        keeper.limits.quota,
        auth,
        meter_provider=telemetry.meter_provider if telemetry else None,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if limits is not None:
                await limits.aclose()
            await auth.aclose()
            if database is not None:
                await database.dispose()

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
        dependencies=[Depends(enforce_limits)],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.keeper_settings = keeper
    app.state.readiness_checks = checks
    app.state.auth = auth
    app.state.database = database
    app.state.limits = limits
    install_client_addresses(app.state, keeper.trusted_proxies)

    install_error_handlers(app, renderer)
    app.include_router(health_router)
    app.include_router(whoami_router, prefix=keeper.api_prefix)
    app.include_router(admin_router, prefix=keeper.api_prefix)
    for router in domain_routers():  # last: branch routes match hierarchical names
        app.include_router(router, prefix=keeper.api_prefix)

    # Innermost first: the last middleware added is the outermost one.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=keeper.limits.max_request_body_bytes, renderer=renderer
    )
    if limits is not None:
        app.add_middleware(RateLimitHeadersMiddleware)
    if keeper.cors.allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(keeper.cors.allow_origins),
            allow_methods=list(keeper.cors.allow_methods),
            allow_headers=[*keeper.cors.allow_headers, keeper.request_id_header],
            expose_headers=[keeper.request_id_header, *EXPOSED_HEADERS],
            allow_credentials=keeper.cors.allow_credentials,
            max_age=keeper.cors.max_age_s,
        )
    if auth.audit_writer is not None:
        app.add_middleware(
            AuditMiddleware,
            writer=auth.audit_writer,
            settings=keeper.auth.audit,
            api_prefix=keeper.api_prefix,
        )
    app.add_middleware(
        RequestContextMiddleware,
        header=keeper.request_id_header,
        renderer=renderer,
        tracer_provider=telemetry.tracer_provider if telemetry else None,
        meter_provider=telemetry.meter_provider if telemetry else None,
    )
    return app
