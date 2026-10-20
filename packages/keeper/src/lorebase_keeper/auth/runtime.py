"""The auth components of one Keeper application, built from settings."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import AuthSettings
from lorebase_keeper.auth.oidc import JwksCache, OidcVerifier
from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.auth.principals import Authenticator

logger = logging.getLogger("lorebase.keeper.auth")


@dataclass
class AuthRuntime:
    """Authenticator, key codec and store of an application."""

    settings: AuthSettings
    store: AuthStore | None
    authenticator: Authenticator
    codec: ApiKeyCodec | None
    clock: Callable[[], datetime]
    http: httpx.AsyncClient | None = None
    owns_http: bool = False

    async def aclose(self) -> None:
        """Release the HTTP client when this runtime created it."""
        if self.http is not None and self.owns_http:
            await self.http.aclose()


def build_runtime(
    settings: AuthSettings,
    *,
    store: AuthStore | None,
    http_client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AuthRuntime:
    """Wire the auth components for `settings`.

    `store` is required for every mode but `none`.
    `http_client` is used for identity provider requests (tests inject a mock transport); when
    absent one is created with the configured timeout.
    """
    if settings.mode == "none":
        logger.warning(
            "authentication is disabled (keeper.auth.mode: none): every request is accepted "
            "as an administrator. Use this for local development only."
        )
    elif store is None:
        raise ValueError(f"keeper.auth.mode {settings.mode!r} needs a database")

    codec = ApiKeyCodec(settings.api_keys) if settings.uses_api_keys else None
    http = http_client
    owns_http = False
    verifier: OidcVerifier | None = None
    if settings.uses_oidc:
        if http is None:
            http = httpx.AsyncClient(timeout=settings.oidc.http_timeout_s)
            owns_http = True
        verifier = OidcVerifier(settings.oidc, JwksCache(settings.oidc, http))

    return AuthRuntime(
        settings=settings,
        store=store,
        authenticator=Authenticator(settings, store=store, codec=codec, oidc=verifier, clock=clock),
        codec=codec,
        clock=clock,
        http=http,
        owns_http=owns_http,
    )
