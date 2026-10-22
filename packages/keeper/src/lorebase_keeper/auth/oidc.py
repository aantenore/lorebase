"""Bearer token validation against any OpenID Connect provider (design 13.2).

Tokens are JWTs checked against the provider's published signing keys (JWKS): signature,
issuer, audience, expiry and not-before, with the configured clock skew. Keys are fetched with
httpx and cached; an unknown key id triggers at most one refresh per `jwks_min_refresh_s`.

PyJWT (with `cryptography`) does the token work. python-jose was not chosen: it is barely
maintained and has had several security advisories; PyJWT is maintained, small and parses JWKS
natively. No provider SDK is involved: discovery and key download are plain HTTP.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from lorebase_keeper.auth.config import OidcSettings, insecure_url_problem

logger = logging.getLogger("lorebase.keeper.auth.oidc")

DISCOVERY_PATH = "/.well-known/openid-configuration"


class InvalidTokenError(Exception):
    """The token is not acceptable. The message is for logs, never for the client."""


class ProviderUnavailableError(Exception):
    """The provider's keys cannot be obtained, so no token can be judged."""


@dataclass(frozen=True)
class OidcClaims:
    """What Keeper needs from a validated token."""

    subject: str
    display_name: str
    groups: tuple[str, ...]


class JwksCache:
    """Signing keys of the provider, refreshed after a time to live or for an unknown key id."""

    def __init__(
        self,
        settings: OidcSettings,
        http: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._jwks_uri = settings.jwks_uri
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str | None) -> PyJWK:
        """The signing key with this id (the only key when the token names none)."""
        if self._is_stale():
            await self._refresh()
        key = self._find(kid)
        if key is None:
            await self._refresh()  # the provider may have rotated its keys
            key = self._find(kid)
        if key is None:
            if not self._keys:
                raise ProviderUnavailableError("signing keys unavailable")
            raise InvalidTokenError(f"no signing key {kid!r}")
        return key

    def _find(self, kid: str | None) -> PyJWK | None:
        if kid is not None:
            return self._keys.get(kid)
        return next(iter(self._keys.values())) if len(self._keys) == 1 else None

    def _is_stale(self) -> bool:
        return (
            self._fetched_at is None
            or self._clock() - self._fetched_at >= self._settings.jwks_cache_ttl_s
        )

    async def _refresh(self) -> None:
        """Fetch the keys, unless an attempt was made less than `jwks_min_refresh_s` ago.

        On failure the previous keys stay in use and the next attempt waits for the same delay,
        so an unreachable provider is not hammered.
        """
        async with self._lock:
            now = self._clock()
            if (
                self._attempted_at is not None
                and now - self._attempted_at < self._settings.jwks_min_refresh_s
            ):
                return
            self._attempted_at = now
            try:
                document = await self._fetch_jwks()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("cannot fetch signing keys: %s", type(exc).__name__)
                return
            self._keys = _parse_keys(document)
            self._fetched_at = now

    async def _fetch_jwks(self) -> Mapping[str, Any]:
        if self._jwks_uri is None:
            self._jwks_uri = await self._discover()
        response = await self._http.get(self._jwks_uri)
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise ValueError("malformed JWKS document")
        return document

    async def _discover(self) -> str:
        issuer = self._settings.issuer or ""
        response = await self._http.get(issuer.rstrip("/") + DISCOVERY_PATH)
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict) or document.get("issuer") != issuer:
            raise ValueError("discovery document does not match the configured issuer")
        uri = document.get("jwks_uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("discovery document has no jwks_uri")
        problem = insecure_url_problem(uri, self._settings.allow_insecure)
        if problem:
            logger.warning("discovered jwks_uri %r refused: %s", uri, problem)
            raise ValueError("discovered jwks_uri is not acceptable")
        return uri


def _parse_keys(document: Mapping[str, Any]) -> dict[str, PyJWK]:
    keys: dict[str, PyJWK] = {}
    for index, entry in enumerate(document["keys"]):
        if not isinstance(entry, dict) or entry.get("use", "sig") != "sig":
            continue
        try:
            key = PyJWK(entry)
        except jwt.PyJWTError:
            logger.info("skipping unsupported signing key %s", entry.get("kid", index))
            continue
        keys[str(entry.get("kid", f"#{index}"))] = key
    return keys


class OidcVerifier:
    """Validates bearer tokens and extracts the claims Keeper uses."""

    def __init__(self, settings: OidcSettings, jwks: JwksCache) -> None:
        self._settings = settings
        self._jwks = jwks

    async def verify(self, token: str) -> OidcClaims:
        """Claims of a valid token; `InvalidTokenError` or `ProviderUnavailableError` otherwise."""
        claims = await self._decode(token, self._settings.audience, ("exp", "iss", "aud"))
        return self._claims(claims)

    async def verify_id_token(
        self, token: str, *, client_id: str, nonce: str | None = None
    ) -> OidcClaims:
        """Claims of a valid ID token issued to `client_id` (OpenID Connect Core 3.1.3.7).

        The audience must contain the client id and, with several audiences, `azp` must name it.
        `nonce`, when given, must equal the token's (sign-in); refreshed ID tokens carry none
        to compare.
        """
        claims = await self._decode(token, (client_id,), ("exp", "iat", "iss", "aud"))
        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != client_id:
            raise InvalidTokenError("azp does not name this client")
        if nonce is not None:
            received = claims.get("nonce")
            if not isinstance(received, str) or not hmac.compare_digest(received, nonce):
                raise InvalidTokenError("nonce mismatch")
        return self._claims(claims)

    async def _decode(
        self, token: str, audience: Sequence[str], required: Sequence[str]
    ) -> dict[str, Any]:
        settings = self._settings
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in settings.algorithms:
                raise InvalidTokenError(f"algorithm {algorithm!r} is not accepted")
            key = await self._jwks.key_for(header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=list(audience),
                issuer=settings.issuer,
                leeway=settings.clock_skew_s,
                options={"require": [*required, settings.subject_claim]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(f"{type(exc).__name__}: {exc}") from None
        return claims

    def _claims(self, claims: Mapping[str, Any]) -> OidcClaims:
        settings = self._settings
        subject = claims.get(settings.subject_claim)
        if not isinstance(subject, str) or not subject:
            raise InvalidTokenError("subject claim is not a non-empty string")
        return OidcClaims(
            subject=subject,
            display_name=self._display_name(claims, subject),
            groups=_groups(claims.get(settings.groups_claim)),
        )

    def _display_name(self, claims: Mapping[str, Any], subject: str) -> str:
        for name in self._settings.name_claims:
            value = claims.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return subject


def _groups(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value if isinstance(item, str))
    return ()
