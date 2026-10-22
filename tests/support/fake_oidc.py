"""A fake OpenID Connect provider and browser helpers for Keeper's sign-in tests.

`OidcServer` serves discovery, signing keys, the token endpoint (authorization code with PKCE
S256, refresh tokens with rotation) and names an end-session endpoint, over an httpx mock
transport handed to Keeper as its HTTP client. The helpers play the browser: they pass cookies
by hand so that each test controls exactly what is sent.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://idp.example.test"
AUDIENCE = "lorebase-api"
CLIENT_ID = "lorebase-web"
CLIENT_SECRET = "client-secret-0123456789"
APP = "https://app.example.test"
REDIRECT = f"{APP}/api/v1/auth/callback"
ENCRYPTION_KEY = "session-key-0123456789-0123456789-abcdef"
LOGIN = "/api/v1/auth/login"
CALLBACK = "/api/v1/auth/callback"
SESSION = "/api/v1/auth/session"
LOGOUT = "/api/v1/auth/logout"
WHOAMI = "/api/v1/auth/whoami"
SESSION_COOKIE = "__Host-lorebase_session"
LOGIN_COOKIE = "__Host-lorebase_login"


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class OidcServer:
    """A provider with a code flow, PKCE, refresh token rotation and RP-initiated logout."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.reset()

    def reset(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, str] = {}
        self.token_requests: list[dict[str, str]] = []
        self.token_failure: int | None = None
        self.expires_in = 300
        self.groups: list[str] = []
        self.refresh_id_token_subject: str | None = None
        self.refresh_id_token_nonce: str | None = None
        self.last_nonce: str | None = None
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize?tenant=t1",
                    "token_endpoint": f"{ISSUER}/token",
                    "end_session_endpoint": f"{ISSUER}/logout",
                    "jwks_uri": f"{ISSUER}/keys",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/keys":
            jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig"}]})
        if path == "/token":
            return self._token(request)
        return httpx.Response(404)

    def id_token(self, subject: str, nonce: str | None = None, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": subject,
            "name": "Alice",
            "groups": self.groups,
            "iat": now,
            "exp": now + 600,
            "nonce": nonce,
            **overrides,
        }
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def authorize(
        self, location: str, *, subject: str = "alice-subject", **entry: Any
    ) -> tuple[str, str]:
        """What the provider does after the user signs in: a code bound to the request."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/authorize"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert query["tenant"] == "t1"  # the endpoint's own query is kept
        assert query["response_type"] == "code"
        assert query["client_id"] == CLIENT_ID
        assert query["redirect_uri"] == REDIRECT
        assert query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        code = secrets.token_urlsafe(16)
        self.last_nonce = query["nonce"]
        self.codes[code] = {
            "subject": subject,
            "nonce": query["nonce"],
            "challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
            **entry,
        }
        return code, query["state"]

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if self.token_failure is not None:
            return httpx.Response(self.token_failure, json={"error": "server_error"})
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.pop(form.get("code", ""), None)
            if (
                entry is None
                or form.get("redirect_uri") != entry["redirect_uri"]
                or s256(form.get("code_verifier", "")) != entry["challenge"]
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            subject = entry["subject"]
            id_token = entry.get("id_token") or self.id_token(subject, entry["nonce"])
        elif form.get("grant_type") == "refresh_token":
            found = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
            if found is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            subject = found
            id_token = self.id_token(
                self.refresh_id_token_subject or subject, self.refresh_id_token_nonce
            )
        else:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        refresh_token = secrets.token_urlsafe(16)
        self.refresh_tokens[refresh_token] = subject
        return httpx.Response(
            200,
            json={
                "access_token": "opaque-access-token",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "id_token": id_token,
                "refresh_token": refresh_token,
            },
        )


def session_auth(*, mode: str = "oidc", **sessions: Any) -> dict[str, Any]:
    return {
        "mode": mode,
        "oidc": {"issuer": ISSUER, "audience": AUDIENCE},
        "api_keys": {"pepper": "test-pepper-0123456789-0123456789-abcdef"},
        "sessions": {
            "enabled": True,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT,
            "app_url": f"{APP}/",
            "encryption_key": ENCRYPTION_KEY,
            **sessions,
        },
    }


def keeper_client(app: FastAPI) -> httpx.AsyncClient:
    """A client that keeps no cookies: each test sends the ones it means to send."""

    async def forget(_: httpx.Response) -> None:
        client.cookies.clear()

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://keeper.test",
        event_hooks={"response": [forget]},
    )
    return client


def set_cookies(response: httpx.Response) -> dict[str, Any]:
    """The cookies a response sets, by name, with their attributes."""
    found: dict[str, Any] = {}
    for header in response.headers.get_list("set-cookie"):
        jar: SimpleCookie = SimpleCookie()
        jar.load(header)
        for name, morsel in jar.items():
            found[name] = morsel
    return found


def cookie_header(**cookies: str) -> dict[str, str]:
    return {"cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}


async def start(http: httpx.AsyncClient, return_to: str | None = "/acme/explorer") -> Any:
    response = await http.get(LOGIN, params={} if return_to is None else {"return_to": return_to})
    assert response.status_code == 302, response.text
    return response


async def sign_in(
    http: httpx.AsyncClient,
    server: OidcServer,
    *,
    return_to: str | None = "/acme/explorer",
    extra_cookies: dict[str, str] | None = None,
    **authorize: Any,
) -> httpx.Response:
    """Login, provider sign-in, callback: the callback's response."""
    begun = await start(http, return_to)
    state_cookie = set_cookies(begun)[LOGIN_COOKIE].value
    code, state = server.authorize(begun.headers["location"], **authorize)
    return await http.get(
        CALLBACK,
        params={"code": code, "state": state},
        headers=cookie_header(**{LOGIN_COOKIE: state_cookie, **(extra_cookies or {})}),
    )


async def session_cookie(http: httpx.AsyncClient, server: OidcServer, **kwargs: Any) -> str:
    response = await sign_in(http, server, **kwargs)
    assert response.status_code == 302, response.text
    return str(set_cookies(response)[SESSION_COOKIE].value)


def with_session(session_id: str, **headers: str) -> dict[str, str]:
    return {**cookie_header(**{SESSION_COOKIE: session_id}), **headers}


async def csrf_headers(http: httpx.AsyncClient, session_id: str) -> dict[str, str]:
    info = (await http.get(SESSION, headers=with_session(session_id))).json()
    return with_session(session_id, **{info["csrf_header"]: info["csrf_token"]})
