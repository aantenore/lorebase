"""The caller's credential: read from the request, checked by Keeper, forwarded unchanged.

Guide has no Keeper credential of its own and never substitutes one. A caller authenticates
in one of three ways:

- `bearer`: the credential header (`Authorization: Bearer <OIDC token or API key>`, a bare API
  key is accepted too). Sent to Keeper as is.
- `session`: Keeper's session cookie (`guide.http.session_cookie`) with its CSRF token in
  `guide.http.csrf_header`, when Guide is served under the web app's origin. Only that one
  cookie and the token are forwarded; Guide asks Keeper for the session's token
  (`GET /auth/session`) and refuses the request unless the two match, so a cross-site page
  cannot spend the user's model budget with the cookie alone.
- `anonymous`: no credential, only with `guide.http.allow_anonymous` (Keeper in auth mode none).

Before any model call, `verify` asks Keeper who the caller is: an unknown or expired
credential is refused before it costs tokens. A request carrying an `Origin` that is not in
`guide.http.allowed_origins`, or marked cross-site by the browser, is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any, Literal

from lorebase_client import AsyncLorebaseClient
from lorebase_client.core import Call, parse_json
from lorebase_client.errors import LorebaseError
from lorebase_guide.settings import HttpSettings

CredentialKind = Literal["bearer", "session", "anonymous"]
_MAX_CREDENTIAL_CHARS = 8_192
_CROSS_SITE = "cross-site"
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{32,128}")
"""Shape of Keeper's session ids; any other cookie value is refused, never forwarded."""
_CSRF_TOKEN = re.compile(r"[A-Za-z0-9_.~+/=-]{1,256}")


class AuthRefusedError(Exception):
    """The request is refused before any work: `status` is 401 or 403."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Caller:
    """The credential of one request. Its repr never shows the secret parts."""

    kind: CredentialKind
    authorization: str | None = None
    session_id: str | None = None
    csrf_token: str | None = None
    principal: str | None = None
    """Principal id reported by Keeper once verified."""

    def __repr__(self) -> str:
        return f"Caller(kind={self.kind!r}, principal={self.principal!r})"

    @property
    def identity(self) -> str:
        """Key of the per-caller limits: the verified principal, else the credential's
        fingerprint (anonymous callers share one key)."""
        return f"principal:{self.principal}" if self.principal else self.fingerprint

    @property
    def fingerprint(self) -> str:
        """Stable, non-reversible id of the credential, used for per-caller limits."""
        secret = self.authorization or self.session_id or ""
        if not secret:
            return "anonymous"
        return hashlib.sha256(f"{self.kind}\x00{secret}".encode()).hexdigest()[:32]

    def keeper_headers(self, http: HttpSettings) -> dict[str, str]:
        """Headers that carry this credential to Keeper, and nothing else of the request."""
        if self.kind == "bearer" and self.authorization:
            return {"Authorization": self.authorization}
        if self.kind == "session" and self.session_id and self.csrf_token:
            return {
                "Cookie": f"{http.session_cookie}={self.session_id}",
                http.csrf_header: self.csrf_token,
            }
        return {}


def _single_line(value: str) -> bool:
    return "\r" not in value and "\n" not in value and len(value) <= _MAX_CREDENTIAL_CHARS


def _cookie(headers: Mapping[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw or not name:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None and morsel.value else None


def check_origin(headers: Mapping[str, str], http: HttpSettings) -> None:
    """Refuse browser requests from origins that are not configured, and cross-site ones."""
    origin = headers.get("origin")
    if origin is not None and origin.rstrip("/").lower() not in http.allowed_origins:
        raise AuthRefusedError(
            403, "origin_not_allowed", "requests from this origin are not allowed"
        )
    site = headers.get("sec-fetch-site")
    if site is not None and site.lower() == _CROSS_SITE and origin is None:
        raise AuthRefusedError(403, "cross_site", "cross-site requests are not allowed")


def read_caller(headers: Mapping[str, str], http: HttpSettings) -> Caller:
    """The credential of a request (lower-case header names), not yet verified."""
    check_origin(headers, http)
    authorization = headers.get(http.credential_header.lower())
    if authorization is not None and authorization.strip():
        value = authorization.strip()
        if not _single_line(value):
            raise AuthRefusedError(401, "unauthorized", "malformed credential")
        scheme, _, rest = value.partition(" ")
        if scheme.lower() == "bearer":
            if not rest.strip():
                raise AuthRefusedError(401, "unauthorized", "malformed credential")
            value = f"Bearer {rest.strip()}"
        elif rest:
            raise AuthRefusedError(401, "unauthorized", "only Bearer credentials are accepted")
        else:
            value = f"Bearer {value}"
        return Caller(kind="bearer", authorization=value)
    session_id = _cookie(headers, http.session_cookie)
    if session_id is not None:
        token = (headers.get(http.csrf_header.lower()) or "").strip()
        if not _SESSION_ID.fullmatch(session_id):
            raise AuthRefusedError(401, "unauthorized", "malformed session cookie")
        if not _CSRF_TOKEN.fullmatch(token):
            raise AuthRefusedError(
                403, "csrf", f"a session cookie needs its CSRF token in {http.csrf_header}"
            )
        return Caller(kind="session", session_id=session_id, csrf_token=token)
    if http.allow_anonymous:
        return Caller(kind="anonymous")
    raise AuthRefusedError(
        401,
        "unauthorized",
        f"send `{http.credential_header}: Bearer <token or API key>` or the session cookie",
    )


async def verify(caller: Caller, client: AsyncLorebaseClient) -> Caller:
    """Ask Keeper who the caller is, with the caller's own credential; for a session, also
    check the CSRF token against the session's. Returns the caller with its principal."""
    try:
        who = await client.whoami()
        if caller.kind == "session":
            session: Any = await client.send(Call("GET", "/auth/session", parse_json, safe=True))
            expected = str(session.get("csrf_token") or "") if isinstance(session, dict) else ""
            given = caller.csrf_token or ""
            if not expected or not hmac.compare_digest(expected.encode(), given.encode()):
                raise AuthRefusedError(403, "csrf", "the CSRF token does not match the session")
    except LorebaseError as exc:
        if exc.status in (401, 403):
            raise AuthRefusedError(
                exc.status, "unauthorized", "Keeper refused the credential"
            ) from None
        raise
    return Caller(
        kind=caller.kind,
        authorization=caller.authorization,
        session_id=caller.session_id,
        csrf_token=caller.csrf_token,
        principal=who.id or None,
    )
