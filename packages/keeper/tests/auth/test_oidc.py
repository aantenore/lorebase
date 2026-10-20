"""OIDC bearer token validation against a fake provider (an RSA key generated in the test)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from lorebase_keeper.auth.config import OidcSettings
from lorebase_keeper.auth.oidc import (
    InvalidTokenError,
    JwksCache,
    OidcVerifier,
    ProviderUnavailableError,
)
from lorebase_store.models.enums import PrincipalType

ISSUER = "https://idp.example.test"
AUDIENCE = "lorebase-api"
PROBE = "/api/v1/auth/whoami"

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


def oidc_auth(**oidc: Any) -> dict[str, Any]:
    return {"mode": "oidc", "oidc": {"issuer": ISSUER, "audience": AUDIENCE, **oidc}}


def forge(header: dict[str, Any], claims: dict[str, Any], *, signature: bytes) -> str:
    """A compact JWT with the given header and signature, bypassing the library's safeguards."""

    def encode(part: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    return (
        f"{encode(header)}.{encode(claims)}."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def get(app: FastAPI, client_for: ClientFor, token: str, path: str = PROBE) -> httpx.Response:
    async with client_for(app) as http:
        return await http.get(path, headers=bearer(token))


@pytest.fixture
def alice(store: Any) -> str:
    """Alice already exists."""
    store.add_principal("usr_alice", PrincipalType.USER, subject="alice-subject")
    return "usr_alice"


async def test_valid_token_authenticates_and_discovers_keys_once(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    app = auth_app(oidc_auth())
    async with client_for(app) as http:
        for _ in range(3):
            response = await http.get(PROBE, headers=bearer(provider.token()))
            assert response.status_code == 200
    assert provider.count("/.well-known/openid-configuration") == 1
    assert provider.count("/keys") == 1


async def test_explicit_jwks_uri_skips_discovery(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    app = auth_app(oidc_auth(jwks_uri=f"{ISSUER}/keys"))
    response = await get(app, client_for, provider.token())
    assert response.status_code == 200
    assert provider.count("/.well-known/openid-configuration") == 0


async def test_expired_token_is_rejected(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    token = provider.token(exp=int(time.time()) - 3600)
    response = await get(auth_app(oidc_auth()), client_for, token)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


async def test_clock_skew_is_tolerated_up_to_the_setting(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    just_expired = provider.token(exp=int(time.time()) - 30)
    long_expired = provider.token(exp=int(time.time()) - 120)
    app = auth_app(oidc_auth(clock_skew_s=60))
    assert (await get(app, client_for, just_expired)).status_code == 200
    assert (await get(app, client_for, long_expired)).status_code == 401
    strict = auth_app(oidc_auth(clock_skew_s=0))
    assert (await get(strict, client_for, just_expired)).status_code == 401


async def test_token_not_yet_valid_is_rejected(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    token = provider.token(nbf=int(time.time()) + 600)
    assert (await get(auth_app(oidc_auth()), client_for, token)).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "another-api"},
        {"aud": ["x", "y"]},
        {"iss": "https://evil.example.test"},
        {"aud": None},
        {"exp": None},
        {"sub": None},
    ],
    ids=["wrong-audience", "audience-list", "wrong-issuer", "no-aud", "no-exp", "no-sub"],
)
async def test_claims_that_do_not_match_are_rejected(
    auth_app: Callable[..., FastAPI],
    client_for: ClientFor,
    provider: Any,
    alice: str,
    overrides: dict[str, Any],
) -> None:
    response = await get(auth_app(oidc_auth()), client_for, provider.token(**overrides))
    assert response.status_code == 401


async def test_any_configured_audience_is_accepted(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    app = auth_app(oidc_auth(audience=["other", AUDIENCE]))
    assert (await get(app, client_for, provider.token())).status_code == 200
    assert (await get(app, client_for, provider.token(aud=["x", "other"]))).status_code == 200


async def test_token_signed_by_another_key_is_rejected(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    forged = provider.token(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    assert (await get(auth_app(oidc_auth()), client_for, forged)).status_code == 401


async def test_unsigned_and_symmetric_tokens_are_rejected(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice-subject", "exp": int(time.time()) + 60}
    unsigned = forge({"alg": "none", "kid": provider.kid}, claims, signature=b"")
    confused = forge(
        {"alg": "HS256", "kid": provider.kid},
        claims,
        signature=hmac.new(
            json.dumps(provider.jwk()).encode(), b"signing-input", hashlib.sha256
        ).digest(),
    )
    app = auth_app(oidc_auth())
    assert (await get(app, client_for, unsigned)).status_code == 401
    assert (await get(app, client_for, confused)).status_code == 401


async def test_garbage_token_is_rejected(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any
) -> None:
    assert (await get(auth_app(oidc_auth()), client_for, "not.a.jwt")).status_code == 401


async def test_unknown_key_id_triggers_a_refresh_after_rotation(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    app = auth_app(oidc_auth(jwks_min_refresh_s=0))
    async with client_for(app) as http:
        assert (await http.get(PROBE, headers=bearer(provider.token()))).status_code == 200
        provider.rotate("key-2")
        assert (await http.get(PROBE, headers=bearer(provider.token()))).status_code == 200
    assert provider.count("/keys") == 2


async def test_unknown_key_ids_do_not_cause_a_fetch_storm(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    app = auth_app(oidc_auth(jwks_min_refresh_s=3600))
    async with client_for(app) as http:
        await http.get(PROBE, headers=bearer(provider.token()))
        for n in range(5):
            response = await http.get(PROBE, headers=bearer(provider.token(kid=f"unknown-{n}")))
            assert response.status_code == 401
    assert provider.count("/keys") == 1


async def test_provider_outage_without_cached_keys_is_503(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    provider.fail = True
    response = await get(auth_app(oidc_auth()), client_for, provider.token())
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


async def test_cached_keys_survive_a_provider_outage(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, alice: str
) -> None:
    app = auth_app(oidc_auth(jwks_cache_ttl_s=0.001, jwks_min_refresh_s=0))
    async with client_for(app) as http:
        assert (await http.get(PROBE, headers=bearer(provider.token()))).status_code == 200
        provider.fail = True
        await asyncio.sleep(0.01)
        assert (await http.get(PROBE, headers=bearer(provider.token()))).status_code == 200


async def test_first_use_provisions_the_user(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, store: Any
) -> None:
    app = auth_app(
        oidc_auth(),
    )
    token = provider.token(sub="new-person", name="Nina Newcomer")
    async with client_for(app) as http:
        first = await http.get(PROBE, headers=bearer(token))
        second = await http.get(PROBE, headers=bearer(token))
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    created = [p for p in store.principal_rows.values() if p.external_subject == "new-person"]
    assert len(created) == 1
    assert created[0].type is PrincipalType.USER
    assert created[0].display_name == "Nina Newcomer"
    assert created[0].id.startswith("usr_")


async def test_display_name_falls_back_to_other_claims_then_subject(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, store: Any
) -> None:
    app = auth_app(oidc_auth())
    await get(app, client_for, provider.token(sub="s1", name=None, preferred_username="nina"))
    await get(app, client_for, provider.token(sub="s2", name=None))
    names = {p.external_subject: p.display_name for p in store.principal_rows.values()}
    assert names == {"s1": "nina", "s2": "s2"}


async def test_unknown_users_are_refused_when_provisioning_is_off(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, store: Any, alice: str
) -> None:
    app = auth_app(oidc_auth(auto_provision=False))
    stranger = await get(app, client_for, provider.token(sub="stranger"))
    known = await get(app, client_for, provider.token())
    assert stranger.status_code == 403
    assert known.status_code == 200
    assert not [p for p in store.principal_rows.values() if p.external_subject == "stranger"]


async def test_provider_groups_become_subjects_of_the_caller(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, store: Any
) -> None:
    store.group_rows["grp_admins"] = "idp-admins"
    store.group_rows["grp_other"] = "idp-other"
    app = auth_app(oidc_auth())
    member = await get(app, client_for, provider.token(sub="m", groups=["idp-admins"]))
    outsider = await get(app, client_for, provider.token(sub="o", groups=["unmapped"]))
    assert "grp_admins" in member.json()["subjects"]
    assert "grp_other" not in member.json()["subjects"]
    assert len(outsider.json()["subjects"]) == 1


async def test_groups_claim_name_is_configurable(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, store: Any
) -> None:
    store.group_rows["grp_admins"] = "idp-admins"
    app = auth_app(oidc_auth(groups_claim="roles"))
    right = await get(app, client_for, provider.token(sub="a", roles=["idp-admins"]))
    wrong = await get(app, client_for, provider.token(sub="b", groups=["idp-admins"]))
    assert "grp_admins" in right.json()["subjects"]
    assert "grp_admins" not in wrong.json()["subjects"]


async def test_installation_admins_are_matched_by_provider_subject(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any
) -> None:
    app = auth_app({**oidc_auth(), "installation_admins": ["ops-subject"]})
    ops = await get(app, client_for, provider.token(sub="ops-subject"))
    other = await get(app, client_for, provider.token(sub="someone"))
    assert ops.json()["installation_admin"] is True
    assert other.json()["installation_admin"] is False


async def test_bearer_that_looks_like_an_api_key_is_not_a_jwt(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any
) -> None:
    response = await get(auth_app(oidc_auth()), client_for, "lbk_abcdefghij_secretsecret")
    assert response.status_code == 401
    assert provider.requests == []


def verifier(provider: Any, clock: Callable[[], float], **overrides: Any) -> OidcVerifier:
    settings = OidcSettings(issuer=ISSUER, audience=(AUDIENCE,), **overrides)
    return OidcVerifier(settings, JwksCache(settings, provider.http, clock))


async def test_verifier_returns_claims(provider: Any) -> None:
    claims = await verifier(provider, time.monotonic).verify(
        provider.token(groups=["a", "b"], email="alice@example.test", name=None)
    )
    assert claims.subject == "alice-subject"
    assert claims.display_name == "alice@example.test"
    assert claims.groups == ("a", "b")


async def test_jwks_are_refetched_after_the_time_to_live(provider: Any) -> None:
    now = [1000.0]
    subject = verifier(provider, lambda: now[0], jwks_cache_ttl_s=300, jwks_min_refresh_s=0)
    await subject.verify(provider.token())
    now[0] += 299
    await subject.verify(provider.token())
    assert provider.count("/keys") == 1
    now[0] += 2
    await subject.verify(provider.token())
    assert provider.count("/keys") == 2


async def test_discovery_must_name_the_configured_issuer(provider: Any) -> None:
    settings = OidcSettings(issuer="https://other.example.test", audience=(AUDIENCE,))
    subject = OidcVerifier(settings, JwksCache(settings, provider.http))
    with pytest.raises((ProviderUnavailableError, InvalidTokenError)):
        await subject.verify(provider.token())


def test_symmetric_algorithms_cannot_be_enabled() -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        OidcSettings(issuer=ISSUER, audience=(AUDIENCE,), algorithms=("HS256",))
