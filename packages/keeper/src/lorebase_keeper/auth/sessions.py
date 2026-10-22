"""Browser sessions: Keeper as backend-for-frontend of the web app (design 13.2).

Sign-in runs the authorization code flow with PKCE. `begin_login` returns the provider URL and
an encrypted, short-lived cookie holding state, nonce, PKCE verifier and return path;
`finish_login` checks the callback against it (state, then the ID token's signature, issuer,
audience and nonce) and `create` stores a session. The browser only ever holds an opaque,
random session id in an `HttpOnly` cookie; the database holds its SHA-256, the user's
principal and provider groups, and the provider's refresh and ID tokens encrypted with a key
derived from `sessions.encryption_key`.

Each request with the cookie is resolved by `resolve`: unknown, idle or expired sessions are
refused (and deleted); when the provider's access token has expired, one request uses the
refresh token, and a refusal ends the session. Unsafe methods must pass `check_request`: a
CSRF token (an HMAC of the session id, given to the app by `GET /auth/session`) in a header,
an acceptable `Origin` and no cross-site `Sec-Fetch-Site`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from starlette.requests import cookie_parser

from lorebase_keeper.auth.config import SessionSettings, is_local_path
from lorebase_keeper.auth.keyring import Keyring
from lorebase_keeper.auth.oidc import (
    InvalidTokenError,
    OidcClaims,
    OidcVerifier,
    ProviderUnavailableError,
)
from lorebase_keeper.auth.ports import AuthStore
from lorebase_keeper.auth.provider import (
    ProviderClient,
    TokenRefusedError,
    TokenResponse,
    pkce_pair,
    random_token,
)
from lorebase_keeper.errors import FORBIDDEN, ProblemError
from lorebase_store.auth import SessionRecord

logger = logging.getLogger("lorebase.keeper.auth.sessions")

LoginErrorCode = Literal[
    "login_expired", "login_failed", "access_denied", "no_account", "provider_unavailable"
]
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "same-site", "none"})
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LOGIN_PURPOSE = "login-state"
_TOKENS_PURPOSE = "session-tokens"
_CSRF_PURPOSE = "csrf"
_MAX_PARAM_LENGTH = 4096

LOGIN_PATH = "/auth/login"
CALLBACK_PATH = "/auth/callback"
LOGOUT_PATH = "/auth/logout"
PUBLIC_PATHS = frozenset({LOGIN_PATH, CALLBACK_PATH, LOGOUT_PATH})
"""Routes (without the API prefix) that work without a valid credential: the limits dependency
does not authenticate them, so a stale cookie never blocks signing in or out."""


class LoginError(Exception):
    """A sign-in that cannot complete. `code` is shown to the user; the message is for logs."""

    def __init__(self, code: LoginErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: LoginErrorCode = code


@dataclass(frozen=True)
class LoginStart:
    """Where to send the browser, and the cookie that must come back with the callback."""

    url: str
    state_cookie: str


@dataclass(frozen=True)
class LoginResult:
    """A verified sign-in: the user's claims, the provider's tokens and the return path."""

    claims: OidcClaims
    tokens: TokenResponse
    return_to: str


@dataclass(frozen=True)
class NewSession:
    """A session just created: the id goes into the cookie, never into storage."""

    session_id: str
    record: SessionRecord


@dataclass(frozen=True)
class SignOut:
    """An ended session: whose it was and the provider's logout URL (None when unknown)."""

    principal_id: str | None
    logout_url: str | None


def digest(session_id: str) -> bytes:
    """The stored form of a session id."""
    return hashlib.sha256(session_id.encode("ascii")).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes | None:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def cookie_value(headers: Mapping[str, str], name: str) -> str | None:
    """The value of cookie `name` in the request headers (lower-case names)."""
    header = headers.get("cookie")
    if not header:
        return None
    return cookie_parser(header).get(name) or None


class SessionManager:
    """Sign-in, session lookup and refresh, CSRF checks and sign-out."""

    def __init__(
        self,
        settings: SessionSettings,
        *,
        store: AuthStore,
        provider: ProviderClient,
        verifier: OidcVerifier,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        key = settings.encryption_key
        if key is None:
            raise ValueError("sessions need an encryption key")
        self.settings = settings
        self._store = store
        self._provider = provider
        self._verifier = verifier
        self._keys = Keyring(key.get_secret_value())
        self._clock = clock
        self._origins = settings.trusted_origins()

    # Sign-in -------------------------------------------------------------------------------

    async def begin_login(self, return_to: str) -> LoginStart:
        """Start a sign-in that returns to `return_to` (a local path, checked by the caller).

        `ProviderUnavailableError` when the provider metadata cannot be read.
        """
        metadata = await self._provider.metadata()
        state, nonce = random_token(), random_token()
        verifier, challenge = pkce_pair()
        expires = self._clock() + timedelta(seconds=self.settings.login_timeout_s)
        payload = json.dumps(
            {"s": state, "n": nonce, "v": verifier, "r": return_to, "e": expires.timestamp()},
            separators=(",", ":"),
        ).encode()
        return LoginStart(
            url=self._provider.authorization_url(
                metadata, state=state, nonce=nonce, challenge=challenge
            ),
            state_cookie=_b64encode(self._keys.seal(_LOGIN_PURPOSE, payload)),
        )

    async def finish_login(
        self, params: Mapping[str, str], state_cookie: str | None
    ) -> LoginResult:
        """Verify a callback (`params` is its query) against the sign-in state cookie and redeem
        the code. `LoginError` names what went wrong."""
        pending = self._open_login_state(state_cookie)
        if pending is None:
            raise LoginError("login_expired", "sign-in state cookie missing, altered or expired")
        error = params.get("error")
        if error is not None:
            code: LoginErrorCode = "access_denied" if error == "access_denied" else "login_failed"
            raise LoginError(code, f"provider returned error {error[:64]!r}")
        state, code_param = params.get("state", ""), params.get("code", "")
        if not state or not hmac.compare_digest(state.encode(), pending["s"].encode()):
            raise LoginError("login_failed", "state does not match the sign-in state cookie")
        if not code_param or len(code_param) > _MAX_PARAM_LENGTH:
            raise LoginError("login_failed", "callback without a usable code")
        try:
            tokens = await self._provider.exchange_code(code_param, pending["v"])
        except TokenRefusedError as exc:
            raise LoginError("login_failed", str(exc)) from None
        except ProviderUnavailableError as exc:
            raise LoginError("provider_unavailable", str(exc)) from None
        if tokens.id_token is None:
            raise LoginError("login_failed", "token response without an id_token")
        try:
            claims = await self._verifier.verify_id_token(
                tokens.id_token, client_id=self._provider.client_id, nonce=pending["n"]
            )
        except InvalidTokenError as exc:
            raise LoginError("login_failed", f"id_token rejected: {exc}") from None
        except ProviderUnavailableError as exc:
            raise LoginError("provider_unavailable", str(exc)) from None
        return LoginResult(claims=claims, tokens=tokens, return_to=pending["r"])

    def _open_login_state(self, cookie: str | None) -> dict[str, str] | None:
        if not cookie or len(cookie) > _MAX_PARAM_LENGTH:
            return None
        sealed = _b64decode(cookie)
        plain = self._keys.open(_LOGIN_PURPOSE, sealed) if sealed else None
        if plain is None:
            return None
        try:
            data = json.loads(plain)
        except ValueError:
            return None
        fields = ("s", "n", "v", "r")
        if not isinstance(data, dict) or not all(isinstance(data.get(f), str) for f in fields):
            return None
        expires = data.get("e")
        if not isinstance(expires, int | float) or expires <= self._clock().timestamp():
            return None
        if not is_local_path(data["r"]):
            return None
        return {name: data[name] for name in fields}

    # Sessions ------------------------------------------------------------------------------

    async def create(
        self, principal_id: str, login: LoginResult, *, replaces: str | None = None
    ) -> NewSession:
        """Store a new session for a verified sign-in, ending the session it `replaces` (the
        cookie the browser sent, if any: a new id on every sign-in prevents fixation)."""
        settings = self.settings
        now = self._clock()
        session_id = random_token()
        refresh_token = login.tokens.refresh_token if settings.refresh_tokens else None
        sealed = self._seal_tokens(refresh_token, login.tokens.id_token)
        async with self._store.unit() as unit:
            if replaces and _SESSION_ID.match(replaces):
                await unit.sessions.delete(digest(replaces))
            if settings.purge_batch:
                await unit.sessions.purge_expired(
                    now,
                    now - timedelta(seconds=settings.idle_timeout_s),
                    limit=settings.purge_batch,
                )
            record = await unit.sessions.add(
                id_hash=digest(session_id),
                principal_id=principal_id,
                subject=login.claims.subject,
                external_groups=login.claims.groups,
                tokens=sealed,
                created_at=now,
                expires_at=now + timedelta(seconds=settings.absolute_timeout_s),
                refresh_at=self._next_refresh(now, login.tokens) if refresh_token else None,
            )
        return NewSession(session_id=session_id, record=record)

    async def resolve(self, session_id: str) -> SessionRecord | None:
        """The live session with this id, refreshed at the provider when due; None when it is
        unknown, idle too long, past its absolute lifetime or refused by the provider."""
        if not _SESSION_ID.match(session_id):
            return None
        settings = self.settings
        key = digest(session_id)
        now = self._clock()
        async with self._store.unit() as unit:
            record = await unit.sessions.get(key)
            if record is None:
                return None
            if self._expired(record, now):
                await unit.sessions.delete(key)
                return None
            touch = timedelta(seconds=settings.touch_interval_s)
            if now - record.last_seen_at >= touch:
                await unit.sessions.touch(key, now, min_interval=touch)
                record = replace(record, last_seen_at=now)
        if settings.refresh_tokens and record.refresh_at is not None and record.refresh_at <= now:
            return await self._refresh(record, now)
        return record

    def _expired(self, record: SessionRecord, now: datetime) -> bool:
        idle = timedelta(seconds=self.settings.idle_timeout_s)
        return record.expires_at <= now or record.last_seen_at + idle <= now

    async def _refresh(self, record: SessionRecord, now: datetime) -> SessionRecord | None:
        """Refresh the provider tokens, if this request wins the lease; others go on with the
        session as it is."""
        settings = self.settings
        key = record.id_hash
        lease = now + timedelta(seconds=settings.refresh_lease_s)
        async with self._store.unit() as unit:
            if not await unit.sessions.claim_refresh(key, now, lease):
                return record
        tokens = self._open_tokens(record.tokens) or {}
        refresh_token = tokens.get("refresh_token")
        if refresh_token is None:
            # Sealed under another key (it changed) or nothing to refresh with: no provider
            # check is possible any more, so the session ends.
            logger.info(
                "session ended: no usable refresh token", extra={"principal": record.principal_id}
            )
            await self._delete(key)
            return None
        try:
            response = await self._provider.refresh(refresh_token)
            claims = (
                await self._verifier.verify_id_token(
                    response.id_token, client_id=self._provider.client_id
                )
                if response.id_token
                else None
            )
        except (TokenRefusedError, InvalidTokenError) as exc:
            logger.info(
                "session ended: refresh refused (%s)", exc, extra={"principal": record.principal_id}
            )
            await self._delete(key)
            return None
        except ProviderUnavailableError:
            logger.warning(
                "session refresh postponed: provider unavailable",
                extra={"principal": record.principal_id},
            )
            return record
        if claims is not None and claims.subject != record.subject:
            logger.warning(
                "session ended: refreshed id_token names another subject",
                extra={"principal": record.principal_id},
            )
            await self._delete(key)
            return None
        groups = claims.groups if claims is not None else record.external_groups
        sealed = self._seal_tokens(
            response.refresh_token or refresh_token, response.id_token or tokens.get("id_token")
        )
        refresh_at = self._next_refresh(now, response)
        async with self._store.unit() as unit:
            stored = await unit.sessions.update_tokens(
                key, tokens=sealed, external_groups=groups, refresh_at=refresh_at
            )
        if not stored:
            return None
        return replace(record, tokens=sealed, external_groups=tuple(groups), refresh_at=refresh_at)

    def _next_refresh(self, now: datetime, tokens: TokenResponse) -> datetime:
        settings = self.settings
        interval = max(
            settings.refresh_min_interval_s,
            tokens.expires_in or settings.refresh_default_interval_s,
        )
        return now + timedelta(seconds=interval)

    def _seal_tokens(self, refresh_token: str | None, id_token: str | None) -> bytes | None:
        values = {
            name: value
            for name, value in (("refresh_token", refresh_token), ("id_token", id_token))
            if value
        }
        if not values:
            return None
        return self._keys.seal(_TOKENS_PURPOSE, json.dumps(values).encode())

    def _open_tokens(self, sealed: bytes | None) -> dict[str, str] | None:
        plain = self._keys.open(_TOKENS_PURPOSE, sealed) if sealed else None
        if plain is None:
            return None
        try:
            data = json.loads(plain)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    async def _delete(self, key: bytes) -> None:
        async with self._store.unit() as unit:
            await unit.sessions.delete(key)

    async def end(self, session_id: str) -> SignOut:
        """Sign out: delete the session and give the provider's logout URL (with the ID token
        as hint); nothing when the session is unknown."""
        if not _SESSION_ID.match(session_id):
            return SignOut(None, None)
        key = digest(session_id)
        async with self._store.unit() as unit:
            record = await unit.sessions.get(key)
            if record is None:
                return SignOut(None, None)
            await unit.sessions.delete(key)
        tokens = self._open_tokens(record.tokens) or {}
        try:
            metadata = await self._provider.metadata()
        except ProviderUnavailableError:
            return SignOut(record.principal_id, None)
        url = self._provider.end_session_url(metadata, tokens.get("id_token"))
        return SignOut(record.principal_id, url)

    # CSRF ----------------------------------------------------------------------------------

    def csrf_token(self, session_id: str) -> str:
        """The CSRF token of a session: an HMAC of its id, so nothing more is stored."""
        return _b64encode(self._keys.mac(_CSRF_PURPOSE, session_id.encode("ascii")))

    def check_request(self, session_id: str, method: str, headers: Mapping[str, str]) -> None:
        """Refuse (403, `reason: csrf`) an unsafe request authenticated by the session cookie
        unless it carries the session's CSRF token, comes from a trusted origin when it names
        one, and is not marked cross-site by the browser."""
        if method.upper() in SAFE_METHODS:
            return
        site = headers.get("sec-fetch-site")
        if site is not None and site.lower() not in _ALLOWED_FETCH_SITES:
            raise _csrf_refusal("cross-site request")
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self._origins:
            raise _csrf_refusal("untrusted origin")
        token = headers.get(self.settings.csrf_header.lower())
        expected = self.csrf_token(session_id)
        if not token or not hmac.compare_digest(token.encode(), expected.encode()):
            raise _csrf_refusal("missing or wrong token")


def _csrf_refusal(reason: str) -> ProblemError:
    logger.info("csrf check failed: %s", reason)
    return ProblemError(
        FORBIDDEN,
        "The request was refused by the CSRF check of its session: send the token of "
        "GET /auth/session in the configured header from the web app's origin.",
        extensions={"reason": "csrf"},
    )
