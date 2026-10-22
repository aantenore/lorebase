"""Browser sign-in with server-side sessions (backend-for-frontend) against a fake provider.

The provider (`tests/support/fake_oidc.py`) implements discovery, signing keys, the token
endpoint (authorization code with PKCE S256, refresh tokens with rotation) and an end-session
endpoint. Cookies are passed by hand so that each test controls what the browser sends.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fake_oidc import (
    APP,
    AUDIENCE,
    CALLBACK,
    CLIENT_ID,
    ENCRYPTION_KEY,
    ISSUER,
    LOGIN,
    LOGIN_COOKIE,
    LOGOUT,
    REDIRECT,
    SESSION,
    SESSION_COOKIE,
    WHOAMI,
    OidcServer,
    cookie_header,
    csrf_headers,
    keeper_client,
    s256,
    session_auth,
    session_cookie,
    set_cookies,
    sign_in,
    start,
    with_session,
)
from fastapi import FastAPI
from starlette.requests import Request

from lorebase_keeper.auth.config import AuthSettings, is_local_path
from lorebase_keeper.auth.keyring import Keyring
from lorebase_keeper.ratelimit.enforce import route_path
from lorebase_keeper.settings import keeper_settings
from lorebase_platform.settings import ConfigError, Settings
from lorebase_store.models.enums import PrincipalType


@pytest.fixture(scope="module")
def _module_server() -> OidcServer:
    return OidcServer()


@pytest.fixture
def server(_module_server: OidcServer) -> OidcServer:
    _module_server.reset()
    return _module_server


@pytest.fixture
def build(auth_app: Callable[..., FastAPI], server: OidcServer) -> Callable[..., FastAPI]:
    def make(**sessions: Any) -> FastAPI:
        return auth_app(session_auth(**sessions), http_client=server.http)

    return make


@pytest.fixture
async def http(build: Callable[..., FastAPI]) -> AsyncIterator[httpx.AsyncClient]:
    async with keeper_client(build()) as client:
        yield client


# Sign-in ---------------------------------------------------------------------------------------


async def test_sign_in_creates_a_session_that_maps_to_the_bearer_principal(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    store.add_principal("usr_alice", PrincipalType.USER, subject="alice-subject")
    response = await sign_in(http, server)
    assert response.status_code == 302
    assert response.headers["location"] == f"{APP}/acme/explorer"
    assert response.headers["cache-control"] == "no-store"
    session_id = set_cookies(response)[SESSION_COOKIE].value
    who = (await http.get(WHOAMI, headers=with_session(session_id))).json()
    assert who["id"] == "usr_alice"
    assert who["method"] == "session"
    # PKCE: the verifier sent matched the challenge (the fake provider checks it), with the
    # configured redirect URI and client authentication.
    exchange = server.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["redirect_uri"] == REDIRECT
    assert "client_secret" not in exchange  # client_secret_basic


async def test_session_cookie_flags(http: httpx.AsyncClient, server: OidcServer) -> None:
    response = await sign_in(http, server)
    cookies = set_cookies(response)
    session = cookies[SESSION_COOKIE]
    assert session["httponly"] is True
    assert session["secure"] is True
    assert session["samesite"] == "lax"
    assert session["path"] == "/"
    assert session["domain"] == ""
    assert session["max-age"] == "43200"
    assert len(session.value) >= 43
    assert cookies[LOGIN_COOKIE]["max-age"] == "0"  # the sign-in state is cleared


async def test_login_cookie_flags_and_insecure_development_names(
    build: Callable[..., FastAPI], server: OidcServer
) -> None:
    async with keeper_client(build(cookie_secure=False)) as http:
        begun = await start(http)
        cookies = set_cookies(begun)
        assert set(cookies) == {"lorebase_login"}
        assert cookies["lorebase_login"]["secure"] == ""
        assert cookies["lorebase_login"]["httponly"] is True
        assert cookies["lorebase_login"]["samesite"] == "lax"
        code, state = server.authorize(begun.headers["location"])
        done = await http.get(
            CALLBACK,
            params={"code": code, "state": state},
            headers=cookie_header(lorebase_login=cookies["lorebase_login"].value),
        )
        assert "lorebase_session" in set_cookies(done)


async def test_strict_same_site_applies_to_the_session_cookie_only(
    build: Callable[..., FastAPI], server: OidcServer
) -> None:
    async with keeper_client(build(same_site="strict")) as http:
        begun = await start(http)
        assert set_cookies(begun)[LOGIN_COOKIE]["samesite"] == "lax"
        done = await sign_in(http, server)
        assert set_cookies(done)[SESSION_COOKIE]["samesite"] == "strict"


async def test_groups_from_the_id_token_map_to_lorebase_groups(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    store.group_rows["grp_analysts"] = "analysts"
    server.groups = ["analysts"]
    session_id = await session_cookie(http, server)
    who = (await http.get(WHOAMI, headers=with_session(session_id))).json()
    assert "grp_analysts" in who["subjects"]


async def test_session_requests_use_the_rbac_of_the_principal(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    store.add_principal("usr_alice", PrincipalType.USER, subject="alice-subject")
    store.add_principal("usr_bob", PrincipalType.USER, subject="bob-subject")
    store.add_grant("acme", "usr_alice", "admin")
    grant = {"subject": "usr_bob", "role": "viewer"}
    alice = await csrf_headers(http, await session_cookie(http, server))
    created = await http.post("/api/v1/domains/acme/grants", json=grant, headers=alice)
    assert created.status_code == 201
    assert created.json()["created_by"] == "usr_alice"
    bob = await csrf_headers(http, await session_cookie(http, server, subject="bob-subject"))
    denied = await http.post("/api/v1/domains/acme/grants", json=grant, headers=bob)
    assert denied.status_code == 403
    assert denied.json()["permission"] == "grant.manage"


async def test_no_account_without_auto_provisioning(
    auth_app: Callable[..., FastAPI], server: OidcServer
) -> None:
    auth = session_auth()
    auth["oidc"]["auto_provision"] = False
    async with keeper_client(auth_app(auth, http_client=server.http)) as http:
        response = await sign_in(http, server)
    assert response.headers["location"] == f"{APP}/login?error=no_account"
    assert SESSION_COOKIE not in set_cookies(response)


async def test_return_to_defaults_to_the_app_path(
    build: Callable[..., FastAPI], server: OidcServer
) -> None:
    async with keeper_client(build(app_url=f"{APP}/lorebase/")) as http:
        response = await sign_in(http, server, return_to=None)
    assert response.headers["location"] == f"{APP}/lorebase/"


# Failure cases of the callback -----------------------------------------------------------------


async def test_state_mismatch_is_refused_before_the_code_is_redeemed(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    begun = await start(http)
    code, _ = server.authorize(begun.headers["location"])
    response = await http.get(
        CALLBACK,
        params={"code": code, "state": "forged-state"},
        headers=cookie_header(**{LOGIN_COOKIE: set_cookies(begun)[LOGIN_COOKIE].value}),
    )
    assert response.status_code == 302
    assert response.headers["location"] == f"{APP}/login?error=login_failed"
    assert SESSION_COOKIE not in set_cookies(response)
    assert server.token_requests == []


async def test_callback_without_the_state_cookie_is_refused(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    begun = await start(http)
    code, state = server.authorize(begun.headers["location"])
    response = await http.get(CALLBACK, params={"code": code, "state": state})
    assert response.headers["location"] == f"{APP}/login?error=login_expired"
    assert server.token_requests == []


async def test_tampered_state_cookie_is_refused(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    begun = await start(http)
    value = set_cookies(begun)[LOGIN_COOKIE].value
    tampered = value[:-2] + ("A" if value[-2] != "A" else "B") + value[-1]
    code, state = server.authorize(begun.headers["location"])
    response = await http.get(
        CALLBACK,
        params={"code": code, "state": state},
        headers=cookie_header(**{LOGIN_COOKIE: tampered}),
    )
    assert response.headers["location"] == f"{APP}/login?error=login_expired"


async def test_state_cookie_expires_after_the_login_timeout(
    http: httpx.AsyncClient, server: OidcServer, clock: Any
) -> None:
    begun = await start(http)
    code, state = server.authorize(begun.headers["location"])
    clock.advance(seconds=601)
    response = await http.get(
        CALLBACK,
        params={"code": code, "state": state},
        headers=cookie_header(**{LOGIN_COOKIE: set_cookies(begun)[LOGIN_COOKIE].value}),
    )
    assert response.headers["location"] == f"{APP}/login?error=login_expired"


async def test_a_code_bound_to_another_pkce_challenge_is_refused(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    # A code obtained for another sign-in (another challenge) is useless with this verifier.
    response = await sign_in(http, server, challenge=s256("another-verifier"))
    assert response.headers["location"] == f"{APP}/login?error=login_failed"
    assert SESSION_COOKIE not in set_cookies(response)


async def test_nonce_mismatch_is_refused(http: httpx.AsyncClient, server: OidcServer) -> None:
    response = await sign_in(
        http, server, id_token=server.id_token("alice-subject", nonce="replayed")
    )
    assert response.headers["location"] == f"{APP}/login?error=login_failed"


async def test_id_token_for_another_client_is_refused(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    begun = await start(http)
    nonce = parse_qs(urlsplit(begun.headers["location"]).query)["nonce"][0]
    code, state = server.authorize(
        begun.headers["location"],
        id_token=server.id_token("alice-subject", nonce=nonce, aud="other-client"),
    )
    response = await http.get(
        CALLBACK,
        params={"code": code, "state": state},
        headers=cookie_header(**{LOGIN_COOKIE: set_cookies(begun)[LOGIN_COOKIE].value}),
    )
    assert response.headers["location"] == f"{APP}/login?error=login_failed"


async def test_provider_error_is_reported(http: httpx.AsyncClient) -> None:
    begun = await start(http)
    state = parse_qs(urlsplit(begun.headers["location"]).query)["state"][0]
    response = await http.get(
        CALLBACK,
        params={"error": "access_denied", "state": state},
        headers=cookie_header(**{LOGIN_COOKIE: set_cookies(begun)[LOGIN_COOKIE].value}),
    )
    assert response.headers["location"] == f"{APP}/login?error=access_denied"


async def test_repeated_callback_parameters_are_refused(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    begun = await start(http)
    code, state = server.authorize(begun.headers["location"])
    response = await http.get(
        f"{CALLBACK}?code={code}&state={state}&state=other",
        headers=cookie_header(**{LOGIN_COOKIE: set_cookies(begun)[LOGIN_COOKIE].value}),
    )
    assert response.headers["location"] == f"{APP}/login?error=login_failed"


async def test_token_endpoint_failure_reports_the_provider_unavailable(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    server.token_failure = 503
    response = await sign_in(http, server)
    assert response.headers["location"] == f"{APP}/login?error=provider_unavailable"


# Open redirects --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "return_to",
    [
        "//evil.example/",
        "https://evil.example/",
        "/\\evil.example",
        "\\\\evil.example",
        "evil.example",
        "/ok\r\nLocation: https://evil.example",
        "/\tevil",
        "javascript:alert(1)",
    ],
)
async def test_return_to_outside_the_app_is_refused(
    http: httpx.AsyncClient, return_to: str
) -> None:
    response = await http.get(LOGIN, params={"return_to": return_to})
    assert response.status_code == 400
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    ("value", "local"),
    [
        ("/", True),
        ("/acme/explorer?branch=main#x", True),
        ("/a/../b", True),
        ("//evil.example", False),
        ("/\\evil.example", False),
        ("https://evil.example", False),
        ("", False),
        ("/" + "a" * 2048, False),
        ("/\x7f", False),
    ],
)
def test_is_local_path(value: str, local: bool) -> None:
    assert is_local_path(value) is local


# CSRF ------------------------------------------------------------------------------------------


async def test_unsafe_session_requests_need_the_csrf_token(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    store.add_principal("usr_alice", PrincipalType.USER, subject="alice-subject")
    store.add_principal("usr_bob", PrincipalType.USER, subject="bob-subject")
    store.add_grant("acme", "usr_alice", "admin")
    session_id = await session_cookie(http, server)
    grant = {"subject": "usr_bob", "role": "viewer"}
    url = "/api/v1/domains/acme/grants"
    good = await csrf_headers(http, session_id)
    header = next(name for name in good if name.lower() == "x-csrf-token")
    cases = {
        "no token": with_session(session_id),
        "wrong token": with_session(session_id, **{header: "x" * 43}),
        "evil origin": {**good, "Origin": "https://evil.example"},
        "cross-site": {**good, "Sec-Fetch-Site": "cross-site"},
    }
    for case, headers in cases.items():
        response = await http.post(url, json=grant, headers=headers)
        assert response.status_code == 403, case
        assert response.json()["reason"] == "csrf", case
    assert not [g for g in store.grant_rows if g.subject == "usr_bob"]
    allowed = {**good, "Origin": APP, "Sec-Fetch-Site": "same-origin"}
    assert (await http.post(url, json=grant, headers=allowed)).status_code == 201


async def test_csrf_token_of_another_session_is_refused(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    first = await session_cookie(http, server)
    second = await session_cookie(http, server)
    stolen = await csrf_headers(http, first)
    headers = {**stolen, **with_session(second)}
    response = await http.post(LOGOUT, headers=headers)
    assert response.status_code == 403


async def test_bearer_requests_need_no_csrf_token_and_ignore_the_cookie(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    store.add_principal("usr_carol", PrincipalType.USER, subject="carol-subject")
    session_id = await session_cookie(http, server)
    bearer = {"Authorization": f"Bearer {server.id_token('carol-subject', aud=AUDIENCE)}"}
    who = await http.get(WHOAMI, headers={**bearer, **with_session(session_id)})
    assert who.json()["id"] == "usr_carol"
    assert who.json()["method"] == "oidc"


# Lifetime --------------------------------------------------------------------------------------


async def test_idle_session_expires(
    http: httpx.AsyncClient, server: OidcServer, clock: Any, store: Any
) -> None:
    session_id = await session_cookie(http, server)
    clock.advance(seconds=3599)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    clock.advance(seconds=3599)  # the request above counted as activity
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    clock.advance(seconds=3601)
    response = await http.get(WHOAMI, headers=with_session(session_id))
    assert response.status_code == 401
    assert store.session_rows == {}


async def test_absolute_lifetime_ends_an_active_session(
    build: Callable[..., FastAPI], server: OidcServer, clock: Any
) -> None:
    async with keeper_client(build(refresh_tokens=False)) as http:
        session_id = await session_cookie(http, server)
        for _ in range(12):
            clock.advance(seconds=3500)
            await http.get(WHOAMI, headers=with_session(session_id))
        clock.advance(seconds=3000)  # 45 000 s after sign-in
        assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 401


async def test_unknown_and_malformed_session_ids_are_refused(http: httpx.AsyncClient) -> None:
    for value in ("x" * 43, "short", "bad value with spaces"):
        response = await http.get(WHOAMI, headers=cookie_header(**{SESSION_COOKIE: value}))
        assert response.status_code == 401


async def test_the_stored_session_holds_no_secret_in_clear(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    session_id = await session_cookie(http, server)
    [row] = store.session_rows.values()
    assert row.id_hash == hashlib.sha256(session_id.encode()).digest()
    refresh_token = next(iter(server.refresh_tokens))
    assert row.tokens is not None
    assert refresh_token.encode() not in row.tokens


async def test_sign_in_rotates_the_session_id(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    first = await session_cookie(http, server)
    second = await session_cookie(http, server, extra_cookies={SESSION_COOKIE: first})
    assert first != second
    assert (await http.get(WHOAMI, headers=with_session(first))).status_code == 401
    assert (await http.get(WHOAMI, headers=with_session(second))).status_code == 200
    assert len(store.session_rows) == 1


async def test_sign_in_with_a_stale_cookie_works_while_limits_are_on(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    response = await sign_in(http, server, extra_cookies={SESSION_COOKIE: "y" * 43})
    assert response.status_code == 302
    assert SESSION_COOKIE in set_cookies(response)


# Refresh ---------------------------------------------------------------------------------------


async def test_expired_access_token_is_refreshed_with_rotation(
    http: httpx.AsyncClient, server: OidcServer, clock: Any, store: Any
) -> None:
    server.expires_in = 120
    session_id = await session_cookie(http, server)
    first_refresh = next(iter(server.refresh_tokens))
    clock.advance(seconds=121)
    server.groups = ["analysts"]
    store.group_rows["grp_analysts"] = "analysts"
    who = await http.get(WHOAMI, headers=with_session(session_id))
    assert who.status_code == 200
    assert "grp_analysts" in who.json()["subjects"]
    assert server.token_requests[-1] == {
        "grant_type": "refresh_token",
        "refresh_token": first_refresh,
    }
    clock.advance(seconds=121)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    assert [r["grant_type"] for r in server.token_requests].count("refresh_token") == 2


async def test_refused_refresh_ends_the_session(
    http: httpx.AsyncClient, server: OidcServer, clock: Any, store: Any
) -> None:
    session_id = await session_cookie(http, server)
    server.refresh_tokens.clear()  # the user was disabled at the provider
    clock.advance(seconds=301)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 401
    assert store.session_rows == {}


async def test_refreshed_id_token_for_another_subject_ends_the_session(
    http: httpx.AsyncClient, server: OidcServer, clock: Any
) -> None:
    session_id = await session_cookie(http, server)
    server.refresh_id_token_subject = "mallory-subject"
    clock.advance(seconds=301)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 401


async def test_unavailable_provider_postpones_the_refresh(
    http: httpx.AsyncClient, server: OidcServer, clock: Any
) -> None:
    session_id = await session_cookie(http, server)
    server.token_failure = 502
    clock.advance(seconds=301)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    calls = len(server.token_requests)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    assert len(server.token_requests) == calls  # leased: no retry before refresh_lease_s
    server.token_failure = None
    clock.advance(seconds=31)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    assert len(server.token_requests) == calls + 1


async def test_a_changed_encryption_key_ends_sessions_at_their_refresh(
    auth_app: Callable[..., FastAPI], server: OidcServer, clock: Any
) -> None:
    async with keeper_client(auth_app(session_auth(), http_client=server.http)) as http:
        session_id = await session_cookie(http, server)
    rotated = session_auth(encryption_key="another-key-0123456789-0123456789-abc")
    async with keeper_client(auth_app(rotated, http_client=server.http)) as http:
        assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
        clock.advance(seconds=301)
        assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 401


# Session info and sign-out ---------------------------------------------------------------------


async def test_session_info(http: httpx.AsyncClient, server: OidcServer, clock: Any) -> None:
    session_id = await session_cookie(http, server)
    response = await http.get(SESSION, headers=with_session(session_id))
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["display_name"] == "Alice"
    assert body["csrf_header"] == "X-CSRF-Token"
    assert len(body["csrf_token"]) >= 43
    assert body["idle_timeout_s"] == 3600
    assert body["expires_at"].startswith("2026-10-20T21:00")  # sign-in + 12 h
    assert (await http.get(SESSION)).status_code == 401


async def test_session_info_needs_a_session_not_a_bearer_token(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    store.add_principal("usr_carol", PrincipalType.USER, subject="carol-subject")
    bearer = {"Authorization": f"Bearer {server.id_token('carol-subject', aud=AUDIENCE)}"}
    assert (await http.get(SESSION, headers=bearer)).status_code == 401


async def test_logout_revokes_the_session_and_returns_the_provider_logout_url(
    http: httpx.AsyncClient, server: OidcServer, store: Any
) -> None:
    session_id = await session_cookie(http, server)
    headers = await csrf_headers(http, session_id)
    response = await http.post(LOGOUT, headers=headers)
    assert response.status_code == 200
    url = urlsplit(response.json()["logout_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/logout"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["post_logout_redirect_uri"] == f"{APP}/"
    assert query["id_token_hint"].count(".") == 2
    assert set_cookies(response)[SESSION_COOKIE]["max-age"] == "0"
    assert store.session_rows == {}
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 401
    audit = [entry.action for entry in store.audit_rows]
    assert "auth.session.create" in audit
    assert "auth.session.delete" in audit


async def test_logout_without_the_csrf_token_keeps_the_session(
    http: httpx.AsyncClient, server: OidcServer
) -> None:
    session_id = await session_cookie(http, server)
    response = await http.post(LOGOUT, headers=with_session(session_id))
    assert response.status_code == 403
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200


async def test_logout_without_a_session_only_clears_the_cookie(http: httpx.AsyncClient) -> None:
    response = await http.post(LOGOUT)
    assert response.status_code == 200
    assert response.json() == {"logout_url": None}


# Configuration ---------------------------------------------------------------------------------


async def test_routes_answer_404_while_sessions_are_disabled(
    auth_app: Callable[..., FastAPI], client_for: Any
) -> None:
    async with client_for(auth_app({})) as http:
        assert (await http.get(LOGIN)).status_code == 404
        assert (await http.get(CALLBACK)).status_code == 404
        assert (await http.post(LOGOUT)).status_code == 404


async def test_auth_mode_none_ignores_session_cookies(
    auth_app: Callable[..., FastAPI], client_for: Any
) -> None:
    async with client_for(auth_app({})) as http:
        response = await http.get(WHOAMI, headers=cookie_header(**{SESSION_COOKIE: "z" * 43}))
    assert response.json()["method"] == "none"


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"mode": "api_key"}, "sessions need mode oidc"),
        ({"sessions": {"client_id": None}}, "sessions.client_id is required"),
        ({"sessions": {"encryption_key": "short"}}, "sessions.encryption_key is required"),
        ({"sessions": {"client_secret": None}}, "sessions.client_secret is required"),
        ({"sessions": {"scopes": ["profile"]}}, "must include openid"),
        ({"sessions": {"redirect_uri": "http://app.example.test/cb"}}, "must use https"),
        ({"sessions": {"idle_timeout_s": 50000}}, "cannot exceed absolute_timeout_s"),
        ({"sessions": {"cookie_name": "__Host-x"}}, "cookie_name"),
        ({"sessions": {"error_path": "//evil"}}, "error_path"),
        ({"sessions": {"allowed_origins": ["https://a.example/path"]}}, "allowed_origins"),
    ],
)
def test_invalid_session_settings_are_refused(change: dict[str, Any], needle: str) -> None:
    auth = session_auth()
    auth["mode"] = change.get("mode", auth["mode"])
    auth["sessions"] = {**auth["sessions"], **change.get("sessions", {})}
    with pytest.raises(ConfigError) as error:
        keeper_settings(Settings(keeper={"auth": auth}))
    assert needle in str(error.value)


def test_public_client_needs_no_secret_and_loopback_may_use_http() -> None:
    auth = session_auth(
        client_secret=None,
        token_endpoint_auth_method="none",
        redirect_uri="http://127.0.0.1:8080/api/v1/auth/callback",
        app_url="http://localhost:5173/",
        cookie_secure=False,
    )
    settings = AuthSettings.model_validate(auth)
    assert settings.sessions.trusted_origins() == {
        "http://127.0.0.1:8080",
        "http://localhost:5173",
    }


def test_keyring_separates_purposes_and_detects_tampering() -> None:
    keys = Keyring(ENCRYPTION_KEY)
    sealed = keys.seal("a", b"secret")
    assert keys.open("a", sealed) == b"secret"
    assert keys.open("b", sealed) is None
    assert Keyring("another-key-0123456789-0123456789-abc").open("a", sealed) is None
    assert keys.open("a", sealed[:-1] + bytes([sealed[-1] ^ 1])) is None
    assert keys.open("a", b"") is None
    assert keys.mac("a", b"m") != keys.mac("b", b"m")


async def test_sign_in_routes_are_limited_per_client_address(
    make_app: Callable[..., FastAPI], store: Any, server: OidcServer, clock: Any
) -> None:
    keeper = {
        "auth": session_auth(),
        "limits": {"rate": {"reads": {"requests_per_minute": 0.001, "burst": 2}}},
    }
    app = make_app(keeper, auth_store=store, http_client=server.http, clock=clock)
    async with keeper_client(app) as http:
        statuses = [(await http.get(LOGIN)).status_code for _ in range(2)]
        stale = cookie_header(**{SESSION_COOKIE: "w" * 43})
        limited = await http.get(LOGIN, headers=stale)
    assert statuses == [302, 302]
    assert limited.status_code == 429  # refused by address, before any session lookup
    assert "retry-after" in limited.headers


def test_route_path_drops_the_api_prefix() -> None:
    state = SimpleNamespace(keeper_settings=SimpleNamespace(api_prefix="/api/v1"))
    app = SimpleNamespace(state=state)

    def request(template: str) -> Request:
        route = SimpleNamespace(path=template)
        return Request({"type": "http", "path": template, "route": route, "app": app})

    assert route_path(request("/api/v1/auth/login")) == "/auth/login"
    assert route_path(request("/auth/login")) == "/auth/login"
    assert route_path(request("/api/v10/x")) == "/api/v10/x"
    assert route_path(request("/healthz")) == "/healthz"


async def test_forged_requests_never_reach_the_session(
    http: httpx.AsyncClient, server: OidcServer, clock: Any, store: Any
) -> None:
    session_id = await session_cookie(http, server)
    [before] = store.session_rows.values()
    calls = len(server.token_requests)
    clock.advance(seconds=301)  # touch and refresh would both be due
    forged = await http.post(LOGOUT, headers=with_session(session_id))
    elsewhere = await http.post(
        "/api/v1/domains/acme/grants",
        json={"subject": "usr_x", "role": "viewer"},
        headers=with_session(session_id),
    )
    assert (forged.status_code, elsewhere.status_code) == (403, 403)
    assert elsewhere.json()["reason"] == "csrf"
    assert store.session_rows[before.id_hash] == before
    assert len(server.token_requests) == calls


async def test_refreshed_id_token_must_repeat_the_sign_in_nonce(
    http: httpx.AsyncClient, server: OidcServer, clock: Any
) -> None:
    session_id = await session_cookie(http, server)
    server.refresh_id_token_nonce = server.last_nonce
    clock.advance(seconds=301)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 200
    server.refresh_id_token_nonce = "another-nonce"
    clock.advance(seconds=301)
    assert (await http.get(WHOAMI, headers=with_session(session_id))).status_code == 401
