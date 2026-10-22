"""The OpenID Connect provider as seen by Keeper's sign-in flow (design 13.2).

Provider metadata (discovery), the authorization request with PKCE (RFC 7636, S256 only), the
token endpoint for the code exchange and refreshes (RFC 6749), and the RP-initiated logout URL
(OpenID Connect RP-Initiated Logout 1.0). Plain HTTP with httpx, as for the signing keys in
`oidc.py`: no provider SDK and no new dependency.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from lorebase_keeper.auth.config import OidcSettings, SessionSettings, insecure_url_problem
from lorebase_keeper.auth.oidc import DISCOVERY_PATH, ProviderUnavailableError

logger = logging.getLogger("lorebase.keeper.auth.provider")

PKCE_METHOD = "S256"
_VERIFIER_BYTES = 48  # 64 characters of base64url, within RFC 7636's 43 to 128


class TokenRefusedError(Exception):
    """The token endpoint refused the grant (expired or revoked code or refresh token, or a
    client misconfiguration). The message is for logs only."""


def random_token(nbytes: int = 32) -> str:
    """A URL-safe random string of `nbytes` bytes of entropy."""
    return secrets.token_urlsafe(nbytes)


def pkce_pair() -> tuple[str, str]:
    """A PKCE code verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class ProviderMetadata:
    """The endpoints the sign-in flow uses."""

    authorization_endpoint: str
    token_endpoint: str
    end_session_endpoint: str | None


@dataclass(frozen=True)
class TokenResponse:
    """What the token endpoint returned that Keeper keeps or checks."""

    id_token: str | None
    refresh_token: str | None
    expires_in: int | None


class ProviderClient:
    """Metadata, authorization URL, token requests and logout URL of the configured provider."""

    def __init__(
        self,
        settings: SessionSettings,
        oidc: OidcSettings,
        http: httpx.AsyncClient,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._oidc = oidc
        self._http = http
        self._monotonic = monotonic
        self._metadata: ProviderMetadata | None = None
        self._fetched_at = 0.0

    @property
    def client_id(self) -> str:
        """The client id registered at the provider."""
        return self._settings.client_id or ""

    async def metadata(self) -> ProviderMetadata:
        """The provider's endpoints, cached for `metadata_cache_ttl_s`.

        `ProviderUnavailableError` when the document cannot be fetched or is not acceptable:
        another issuer, endpoints over plain http off loopback, or no S256 PKCE support.
        """
        now = self._monotonic()
        if self._metadata and now - self._fetched_at < self._settings.metadata_cache_ttl_s:
            return self._metadata
        url = self._settings.discovery_url or (self._oidc.issuer or "").rstrip("/") + DISCOVERY_PATH
        try:
            response = await self._http.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("cannot fetch provider metadata: %s", type(exc).__name__)
            raise ProviderUnavailableError("provider metadata unavailable") from None
        self._metadata = self._parse(document)
        self._fetched_at = now
        return self._metadata

    def _parse(self, document: object) -> ProviderMetadata:
        if not isinstance(document, dict) or document.get("issuer") != self._oidc.issuer:
            logger.warning("provider metadata refused: issuer differs from oidc.issuer")
            raise ProviderUnavailableError("provider metadata does not match the issuer")
        methods = document.get("code_challenge_methods_supported")
        if isinstance(methods, list) and PKCE_METHOD not in methods:
            logger.warning("provider metadata refused: no S256 PKCE support")
            raise ProviderUnavailableError("provider does not support S256 PKCE")
        endpoints: dict[str, str | None] = {}
        for name in ("authorization_endpoint", "token_endpoint", "end_session_endpoint"):
            value = document.get(name)
            if value is None and name == "end_session_endpoint":
                endpoints[name] = None
                continue
            problem = (
                insecure_url_problem(value, self._oidc.allow_insecure)
                if isinstance(value, str)
                else "is missing"
            )
            if problem:
                logger.warning("provider metadata refused: %s %s", name, problem)
                raise ProviderUnavailableError(f"provider {name} is not acceptable")
            endpoints[name] = value
        return ProviderMetadata(
            authorization_endpoint=endpoints["authorization_endpoint"] or "",
            token_endpoint=endpoints["token_endpoint"] or "",
            end_session_endpoint=endpoints["end_session_endpoint"],
        )

    def authorization_url(
        self, metadata: ProviderMetadata, *, state: str, nonce: str, challenge: str
    ) -> str:
        """The authorization request: code flow, PKCE S256, the configured redirect URI."""
        settings = self._settings
        return _with_query(
            metadata.authorization_endpoint,
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": settings.redirect_uri or "",
                "scope": " ".join(settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": PKCE_METHOD,
            },
        )

    async def exchange_code(self, code: str, verifier: str) -> TokenResponse:
        """Redeem an authorization code with its PKCE verifier."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri or "",
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Use a refresh token; `TokenRefusedError` when the provider refuses it."""
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    def end_session_url(self, metadata: ProviderMetadata, id_token: str | None) -> str | None:
        """Where to send the browser to sign out at the provider; None without an endpoint."""
        if metadata.end_session_endpoint is None:
            return None
        settings = self._settings
        params = {
            "client_id": self.client_id,
            "post_logout_redirect_uri": settings.post_logout_redirect_uri or settings.app_url or "",
        }
        if id_token:
            params["id_token_hint"] = id_token
        return _with_query(metadata.end_session_endpoint, params)

    async def _token_request(self, form: dict[str, str]) -> TokenResponse:
        metadata = await self.metadata()
        settings = self._settings
        headers = {"Accept": "application/json"}
        secret = settings.client_secret.get_secret_value() if settings.client_secret else ""
        if settings.token_endpoint_auth_method == "client_secret_basic":
            # RFC 6749 2.3.1: both parts form-encoded before base64.
            pair = f"{quote(self.client_id, safe='')}:{quote(secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form["client_id"] = self.client_id
            if settings.token_endpoint_auth_method == "client_secret_post":
                form["client_secret"] = secret
        try:
            response = await self._http.post(metadata.token_endpoint, data=form, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("token endpoint unreachable: %s", type(exc).__name__)
            raise ProviderUnavailableError("token endpoint unreachable") from None
        if 400 <= response.status_code < 500:
            raise TokenRefusedError(
                f"{form['grant_type']} refused with {response.status_code} "
                f"({_error_code(response)})"
            )
        if response.status_code != 200:
            logger.warning("token endpoint answered %s", response.status_code)
            raise ProviderUnavailableError("token endpoint failed")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ProviderUnavailableError("token endpoint answered no JSON object")
        return _token_response(body)


def _token_response(body: Mapping[str, Any]) -> TokenResponse:
    id_token = body.get("id_token")
    refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if isinstance(expires_in, str) and expires_in.isdecimal():
        expires_in = int(expires_in)
    return TokenResponse(
        id_token=id_token if isinstance(id_token, str) and id_token else None,
        refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        expires_in=expires_in if isinstance(expires_in, int) and expires_in > 0 else None,
    )


def _error_code(response: httpx.Response) -> str:
    """The OAuth error code of a refusal, for logs; never the description."""
    try:
        body = response.json()
    except ValueError:
        return "no JSON body"
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and code.isascii() and len(code) <= 64 else "unknown"


def _with_query(url: str, params: Mapping[str, str]) -> str:
    """`url` with `params` added to the query it already has."""
    parts = urlsplit(url)
    query = [*parse_qsl(parts.query, keep_blank_values=True), *params.items()]
    return urlunsplit(parts._replace(query=urlencode(query)))
