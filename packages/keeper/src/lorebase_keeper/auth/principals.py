"""Who is calling: resolving a request's credentials to a principal (design 13.2).

A principal is a user (an OIDC subject, provisioned on first sign-in when settings allow) or a
service principal (owner of API keys, created by an administrator). Every request carries its
credential: an API key, a bearer token or, from the web app, a session cookie
(`auth/sessions.py`). Headers win over the cookie, so API key and bearer requests behave the
same whether sessions are enabled or not.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from opentelemetry import metrics
from opentelemetry.metrics import MeterProvider

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.cache import TtlCache
from lorebase_keeper.auth.config import AuthSettings
from lorebase_keeper.auth.oidc import (
    InvalidTokenError,
    OidcClaims,
    OidcVerifier,
    ProviderUnavailableError,
)
from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.auth.sessions import SessionManager, cookie_value
from lorebase_keeper.client_address import IPAddress, in_networks
from lorebase_keeper.errors import FORBIDDEN, UNAUTHORIZED, UNAVAILABLE, ProblemError
from lorebase_store.auth import ApiKeyRecord
from lorebase_store.models.enums import PrincipalType

logger = logging.getLogger("lorebase.keeper.auth")

AuthMethod = Literal["none", "api_key", "oidc", "session"]

ANONYMOUS_ID = "anonymous"
_MISSING_HASH = bytes(32)
_CHALLENGE = "Bearer"
_INVALID_CHALLENGE = 'Bearer error="invalid_token"'
_REJECTED_KEY_REASONS = {
    "unknown prefix": "unknown_key",
    "wrong secret": "wrong_secret",
    "revoked": "revoked",
    "expired": "expired",
}
"""Metric reason of each API key rejection logged by `_reject_reason`."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a request.

    `subjects` are the ids grants may name: the principal's own id and its groups.
    `key_scopes` limit an API key to some Domains (`domain:<id>`); empty means no extra limit.
    """

    id: str
    type: PrincipalType
    display_name: str
    method: AuthMethod
    subjects: tuple[str, ...]
    key_scopes: tuple[str, ...] = ()
    key_id: str | None = None
    installation_admin: bool = False
    session_expires_at: datetime | None = None
    """End of the absolute lifetime of the browser session (method `session`)."""


def anonymous_principal() -> Principal:
    """The caller when authentication is disabled: an administrator of everything."""
    return Principal(
        id=ANONYMOUS_ID,
        type=PrincipalType.USER,
        display_name="Anonymous (authentication disabled)",
        method="none",
        subjects=(ANONYMOUS_ID,),
        installation_admin=True,
    )


def _unauthorized(detail: str, *, presented: bool) -> ProblemError:
    return ProblemError(
        UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": _INVALID_CHALLENGE if presented else _CHALLENGE},
    )


class Authenticator:
    """Turns request headers into a `Principal` or a 401."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        store: AuthStore | None,
        codec: ApiKeyCodec | None = None,
        oidc: OidcVerifier | None = None,
        sessions: SessionManager | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        meter_provider: MeterProvider | None = None,
    ) -> None:
        if settings.mode != "none" and store is None:
            raise ValueError("authentication needs a store")
        self._settings = settings
        self._store = store
        self._codec = codec
        self._oidc = oidc
        self._sessions = sessions
        self._clock = clock
        self._oidc_cache: TtlCache[tuple[str, tuple[str, ...]], Principal] = TtlCache(
            settings.oidc.principal_cache_ttl_s,
            settings.oidc.principal_cache_max_entries,
            monotonic,
        )
        self._session_cache: TtlCache[tuple[str, tuple[str, ...]], Principal] = TtlCache(
            settings.oidc.principal_cache_ttl_s,
            settings.oidc.principal_cache_max_entries,
            monotonic,
        )
        self._admins = frozenset(settings.installation_admins)
        self._failures = (
            (meter_provider or metrics.get_meter_provider())
            .get_meter("lorebase.keeper")
            .create_counter(
                "lorebase.auth.failures",
                unit="{request}",
                description="Failed authentications, by reason.",
            )
        )

    def _failed(self, reason: str, error: ProblemError) -> ProblemError:
        """Count a failed authentication and return its problem, for the caller to raise."""
        self._failures.add(1, {"reason": reason})
        return error

    async def authenticate(
        self, headers: Mapping[str, str], client: IPAddress | None = None, method: str = "GET"
    ) -> Principal:
        """The principal behind the request headers (lower-case names), sent from `client`
        (the client address; None when unknown) with HTTP `method` (for the CSRF check of
        session requests)."""
        if self._settings.mode == "none":
            return anonymous_principal()
        try:
            bearer, api_key = self._credentials(headers)
        except ProblemError as error:
            raise self._failed("malformed", error) from None
        token = api_key or bearer
        if token is None:
            session_id = self.session_id(headers)
            if session_id is not None:
                return await self._with_session(session_id, headers, method)
            raise self._failed(
                "missing", _unauthorized("Authentication required.", presented=False)
            )
        if api_key is not None or (self._codec and self._codec.looks_like_key(token)):
            if self._codec is None:
                raise self._failed(
                    "method_not_accepted",
                    _unauthorized("API keys are not accepted.", presented=True),
                )
            return await self._with_api_key(token, client)
        if self._oidc is None:
            raise self._failed(
                "method_not_accepted",
                _unauthorized("Bearer tokens are not accepted.", presented=True),
            )
        return await self._with_token(token)

    def presents_credentials(self, headers: Mapping[str, str]) -> bool:
        """Whether the request carries a credential to check (lower-case header names)."""
        if self._settings.mode == "none":
            return False
        return bool(
            headers.get("authorization")
            or headers.get(self._settings.api_keys.header.lower())
            or self.session_id(headers)
        )

    def session_id(self, headers: Mapping[str, str]) -> str | None:
        """The session cookie of the request (lower-case header names), when sessions are
        enabled."""
        if self._sessions is None:
            return None
        return cookie_value(headers, self._sessions.settings.secure_cookie_name)

    def key_prefix(self, headers: Mapping[str, str]) -> str | None:
        """The public prefix of the API key the request presents, when it is shaped like one.
        Nothing is looked up."""
        if self._codec is None:
            return None
        try:
            bearer, api_key = self._credentials(headers)
        except ProblemError:
            return None
        token = api_key or bearer
        parsed = self._codec.parse(token) if token else None
        return parsed.prefix if parsed else None

    def _credentials(self, headers: Mapping[str, str]) -> tuple[str | None, str | None]:
        bearer: str | None = None
        authorization = headers.get("authorization")
        if authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() != "bearer" or not value.strip():
                raise _unauthorized("Unsupported Authorization header.", presented=True)
            bearer = value.strip()
        key = headers.get(self._settings.api_keys.header.lower())
        return bearer, key.strip() if key else None

    async def _with_api_key(self, token: str, client: IPAddress | None) -> Principal:
        assert self._codec is not None
        assert self._store is not None
        parsed = self._codec.parse(token)
        if parsed is None:
            raise self._failed("malformed", _unauthorized("Invalid credentials.", presented=True))
        now = self._clock()
        async with self._store.unit() as unit:
            key = await unit.api_keys.get_by_prefix(parsed.prefix)
            secret_matches = self._codec.matches(parsed.secret, key.hash if key else _MISSING_HASH)
            if key is None or not secret_matches or not key.is_usable(now):
                reason = _reject_reason(key, secret_matches, now)
                logger.info(
                    "api key rejected", extra={"key_prefix": parsed.prefix, "reason": reason}
                )
                raise self._failed(
                    _REJECTED_KEY_REASONS[reason],
                    _unauthorized("Invalid credentials.", presented=True),
                )
            if key.allowed_networks and not in_networks(client, key.allowed_networks):
                # Same answer as a wrong key: the caller learns nothing about the key.
                logger.info(
                    "api key rejected",
                    extra={
                        "key_prefix": parsed.prefix,
                        "reason": "client address not allowed",
                        "client_address": str(client) if client else None,
                    },
                )
                raise self._failed("network", _unauthorized("Invalid credentials.", presented=True))
            record = await unit.principals.get(key.principal_id)
            if record is None:
                raise self._failed(
                    "principal_missing", _unauthorized("Invalid credentials.", presented=True)
                )
            groups = await unit.groups.group_ids_for(record.id)
        await self._record_use(key, now)
        subjects = (record.id, *groups)
        return Principal(
            id=record.id,
            type=record.type,
            display_name=record.display_name,
            method="api_key",
            subjects=subjects,
            key_scopes=key.scopes,
            key_id=key.id,
            installation_admin=self._is_installation_admin(subjects),
        )

    async def _record_use(self, key: ApiKeyRecord, now: datetime) -> None:
        """Update the key's last-used time; a failure never fails the request."""
        assert self._store is not None
        interval = timedelta(seconds=self._settings.api_keys.last_used_interval_s)
        if key.last_used_at is not None and now - key.last_used_at < interval:
            return
        try:
            async with self._store.unit() as unit:
                await unit.api_keys.touch(key.id, now, min_interval=interval)
        except Exception:
            logger.warning("cannot record api key use", exc_info=True)

    async def _with_token(self, token: str) -> Principal:
        assert self._oidc is not None
        assert self._store is not None
        try:
            claims = await self._oidc.verify(token)
        except InvalidTokenError as exc:
            logger.info("bearer token rejected: %s", exc)
            raise self._failed(
                "invalid_token", _unauthorized("Invalid or expired token.", presented=True)
            ) from None
        except ProviderUnavailableError:
            raise ProblemError(
                UNAVAILABLE, "The identity provider's keys are unavailable."
            ) from None
        return await self.user_principal(claims)

    async def user_principal(self, claims: OidcClaims) -> Principal:
        """The user principal of verified provider claims (method `oidc`), provisioned on first
        sign-in when `oidc.auto_provision` allows; 403 when no account exists for them."""
        assert self._store is not None
        cache_key = (claims.subject, claims.groups)
        cached = self._oidc_cache.get(cache_key)
        if cached is not None:
            return cached
        async with self._store.unit() as unit:
            record = await unit.principals.get_by_subject(claims.subject)
            if record is None:
                if not self._settings.oidc.auto_provision:
                    raise self._failed(
                        "no_account",
                        ProblemError(FORBIDDEN, "No account exists for this identity."),
                    )
                record, created = await unit.principals.get_or_create_user(
                    claims.subject, claims.display_name
                )
                if created:
                    logger.info("user provisioned", extra={"principal": record.id})
            groups = await unit.groups.group_ids_for(record.id, claims.groups)
        subjects = (record.id, *groups)
        principal = Principal(
            id=record.id,
            type=record.type,
            display_name=record.display_name,
            method="oidc",
            subjects=subjects,
            installation_admin=self._is_installation_admin((*subjects, claims.subject)),
        )
        self._oidc_cache.set(cache_key, principal)
        return principal

    async def _with_session(
        self, session_id: str, headers: Mapping[str, str], method: str
    ) -> Principal:
        """The user of a browser session: the same principal, groups and grants as the user's
        bearer tokens; unsafe methods must pass the session's CSRF check (403)."""
        assert self._sessions is not None
        assert self._store is not None
        record = await self._sessions.resolve(session_id)
        if record is None:
            raise self._failed(
                "invalid_session", _unauthorized("Session expired or unknown.", presented=True)
            )
        self._sessions.check_request(session_id, method, headers)
        cache_key = (record.principal_id, record.external_groups)
        principal = self._session_cache.get(cache_key)
        if principal is None:
            async with self._store.unit() as unit:
                user = await unit.principals.get(record.principal_id)
                if user is None:
                    await unit.sessions.delete(record.id_hash)
                    raise self._failed(
                        "principal_missing",
                        _unauthorized("Session expired or unknown.", presented=True),
                    )
                groups = await unit.groups.group_ids_for(user.id, record.external_groups)
            subjects = (user.id, *groups)
            principal = Principal(
                id=user.id,
                type=user.type,
                display_name=user.display_name,
                method="session",
                subjects=subjects,
                installation_admin=self._is_installation_admin((*subjects, record.subject)),
            )
            self._session_cache.set(cache_key, principal)
        return replace(principal, session_expires_at=record.expires_at)

    def _is_installation_admin(self, identities: tuple[str, ...]) -> bool:
        return not self._admins.isdisjoint(identities)


def _reject_reason(key: ApiKeyRecord | None, secret_matches: bool, now: datetime) -> str:
    if key is None:
        return "unknown prefix"
    if not secret_matches:
        return "wrong secret"
    return "revoked" if key.revoked_at is not None else "expired"
