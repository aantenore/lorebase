"""`python -m lorebase_keeper bootstrap` and `principal create` over the in-memory store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings, AuthSettings
from lorebase_keeper.bootstrap import (
    BOOTSTRAP_ACTION,
    PRINCIPAL_ACTION,
    BootstrapError,
    bootstrap,
    bootstrap_expiry,
    create_service_principal,
)
from lorebase_store.models.enums import PrincipalType

PEPPER = "test-pepper-0123456789-0123456789-abcdef"
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
ADMIN = "svc_admin"


def auth(**overrides: Any) -> AuthSettings:
    values: dict[str, Any] = {
        "mode": "api_key",
        "installation_admins": (ADMIN, "grp_ops"),
        "api_keys": ApiKeySettings(pepper=PEPPER),
    }
    values.update(overrides)
    return AuthSettings(**values)


async def test_creates_the_admin_principal_and_a_key_that_verifies(store: Any) -> None:
    created = await bootstrap(store, auth(), now=NOW, display_name="ops admin")

    assert created.principal_id == ADMIN
    assert created.principal_created
    principal = store.principal_rows[ADMIN]
    assert (principal.type, principal.display_name) == (PrincipalType.SERVICE, "ops admin")
    key = store.key_rows[created.key_id]
    codec = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
    parsed = codec.parse(created.token)
    assert parsed is not None
    assert parsed.prefix == key.prefix == created.prefix
    assert codec.matches(parsed.secret, key.hash)
    assert key.scopes == ()
    assert key.expires_at == NOW + timedelta(days=365)
    entry = store.audit_rows[-1]
    assert (entry.action, entry.target, entry.domain_id) == (BOOTSTRAP_ACTION, key.id, None)
    assert created.token not in str(entry.details)


async def test_refuses_a_second_key_unless_forced(store: Any) -> None:
    first = await bootstrap(store, auth(), now=NOW)

    with pytest.raises(BootstrapError, match="already holds a usable API key"):
        await bootstrap(store, auth(), now=NOW)
    assert len(store.key_rows) == 1

    second = await bootstrap(store, auth(), now=NOW, force=True)
    assert second.key_id != first.key_id
    assert not second.principal_created
    assert all(k.revoked_at is None for k in store.key_rows.values())
    assert store.audit_rows[-1].details["forced"] is True


async def test_a_revoked_or_expired_admin_key_does_not_block(store: Any) -> None:
    store.add_principal(ADMIN, PrincipalType.SERVICE)
    store.put_key(ADMIN, "revoked000", b"h", (), None, NOW)
    store.put_key(ADMIN, "expired000", b"h", (), NOW - timedelta(days=1), None)

    created = await bootstrap(store, auth(), now=NOW)

    assert not created.principal_created


async def test_names_another_listed_administrator(store: Any) -> None:
    created = await bootstrap(store, auth(), now=NOW, principal_id="grp_ops")
    assert created.principal_id == "grp_ops"


@pytest.mark.parametrize(
    ("settings", "principal_id", "message"),
    [
        (lambda: auth(mode="none"), None, "API keys are not accepted"),
        (lambda: auth(installation_admins=()), None, "installation_admins is empty"),
        (lambda: auth(), "svc_other", "is not listed"),
    ],
)
async def test_refuses_without_a_usable_configuration(
    store: Any, settings: Any, principal_id: str | None, message: str
) -> None:
    with pytest.raises(BootstrapError, match=message):
        await bootstrap(store, settings(), now=NOW, principal_id=principal_id)
    assert not store.key_rows
    assert not store.principal_rows


async def test_refuses_a_user_principal(store: Any) -> None:
    store.add_principal(ADMIN, PrincipalType.USER)
    with pytest.raises(BootstrapError, match="user principal"):
        await bootstrap(store, auth(), now=NOW)


def test_expiry_is_the_default_key_lifetime() -> None:
    assert bootstrap_expiry(ApiKeySettings(pepper=PEPPER, default_ttl_days=None), NOW) is None
    short = ApiKeySettings(pepper=PEPPER, default_ttl_days=30, max_ttl_days=90)
    assert bootstrap_expiry(short, NOW) == NOW + timedelta(days=30)


async def test_creates_a_service_principal_once(store: Any) -> None:
    assert await create_service_principal(store, "agent-risk", "risk agent", now=NOW)
    assert not await create_service_principal(store, "agent-risk", "risk agent", now=NOW)

    assert store.principal_rows["agent-risk"].type is PrincipalType.SERVICE
    assert [e.action for e in store.audit_rows] == [PRINCIPAL_ACTION]


async def test_a_user_with_the_same_id_is_refused(store: Any) -> None:
    store.add_principal("agent-risk", PrincipalType.USER)
    with pytest.raises(BootstrapError, match="user principal"):
        await create_service_principal(store, "agent-risk", "risk agent", now=NOW)
