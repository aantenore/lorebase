"""First installation administrator and service principals, written with the database settings.

In `api_key` mode no request is accepted without a key, and only an administrator can issue
one, so the first key comes from the database side: `python -m lorebase_keeper bootstrap`
(design 13.2). It reads the same settings as Keeper (database, `keeper.auth`, the pepper).

- `bootstrap` creates the service principal named in `keeper.auth.installation_admins` (the
  first entry unless `--principal` names another one) and a key for it. It refuses when an
  installation administrator already holds a usable key, unless `force` is given; then one more
  key is added and the existing ones stay valid. The new token is returned once and never stored.
- `create_service_principal` adds a service principal with a chosen id, so that a schema can
  name it in Operation permissions before any key exists. The API creates service principals
  only with generated ids.

Both write an audit entry on the installation-wide chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from lorebase_keeper.auth.api_keys import ApiKeyCodec, issue_api_key
from lorebase_keeper.auth.config import ApiKeySettings, AuthSettings
from lorebase_keeper.auth.ports import AuthStore
from lorebase_store.auth import NewAuditEntry
from lorebase_store.models.enums import PrincipalType

BOOTSTRAP_ACTOR = "system:bootstrap"
BOOTSTRAP_ACTION = "apikey.bootstrap"
PRINCIPAL_ACTION = "principal.create"
DEFAULT_ADMIN_NAME = "installation administrator"


class BootstrapError(Exception):
    """The request cannot be carried out; the message says why and what to change."""


@dataclass(frozen=True)
class BootstrapKey:
    """The key created by `bootstrap`. `token` exists only here."""

    principal_id: str
    principal_created: bool
    key_id: str
    prefix: str
    token: str
    expires_at: datetime | None


def bootstrap_expiry(settings: ApiKeySettings, now: datetime) -> datetime | None:
    """Expiry of a bootstrap key: the default lifetime of new keys (within `max_ttl_days`)."""
    days = settings.default_ttl_days
    return None if days is None else now + timedelta(days=days)


async def bootstrap(
    store: AuthStore,
    settings: AuthSettings,
    *,
    now: datetime,
    principal_id: str | None = None,
    display_name: str = DEFAULT_ADMIN_NAME,
    force: bool = False,
) -> BootstrapKey:
    """Create the first installation administrator key (see the module documentation)."""
    if not settings.uses_api_keys:
        raise BootstrapError(
            f"keeper.auth.mode is {settings.mode!r}: API keys are not accepted, so there is no "
            "key to bootstrap (use api_key or api_key+oidc)"
        )
    admins = settings.installation_admins
    if not admins:
        raise BootstrapError(
            "keeper.auth.installation_admins is empty: name the administrator principal id there"
        )
    target = principal_id or admins[0]
    if target not in admins:
        raise BootstrapError(
            f"{target!r} is not listed in keeper.auth.installation_admins ({', '.join(admins)})"
        )
    codec = ApiKeyCodec(settings.api_keys)
    expires_at = bootstrap_expiry(settings.api_keys, now)
    async with store.unit() as unit:
        usable = [k for k in await unit.api_keys.list_for_principals(admins) if k.is_usable(now)]
        if usable and not force:
            held = ", ".join(f"{k.principal_id} ({k.prefix}...)" for k in usable)
            raise BootstrapError(
                f"an installation administrator already holds a usable API key: {held}. "
                "Use that key to issue others, or pass --force to add one more"
            )
        principal = await unit.principals.get(target)
        created = principal is None
        if principal is None:
            principal = await unit.principals.add(
                PrincipalType.SERVICE, display_name, principal_id=target
            )
        elif principal.type is not PrincipalType.SERVICE:
            raise BootstrapError(
                f"{target!r} is a {principal.type.value} principal; API keys belong to service "
                "principals"
            )
        token, key = await issue_api_key(codec, unit.api_keys, principal.id, expires_at=expires_at)
        await unit.audit.append(
            NewAuditEntry(
                actor=BOOTSTRAP_ACTOR,
                action=BOOTSTRAP_ACTION,
                target=key.id,
                details={
                    "principal_id": principal.id,
                    "principal_created": created,
                    "prefix": key.prefix,
                    "forced": bool(usable),
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
                at=now,
            )
        )
    return BootstrapKey(
        principal_id=principal.id,
        principal_created=created,
        key_id=key.id,
        prefix=key.prefix,
        token=token,
        expires_at=expires_at,
    )


async def create_service_principal(
    store: AuthStore, principal_id: str, display_name: str, *, now: datetime
) -> bool:
    """Add a service principal with this id; false when it already exists as one."""
    async with store.unit() as unit:
        existing = await unit.principals.get(principal_id)
        if existing is not None:
            if existing.type is not PrincipalType.SERVICE:
                raise BootstrapError(
                    f"{principal_id!r} already exists as a {existing.type.value} principal"
                )
            return False
        await unit.principals.add(PrincipalType.SERVICE, display_name, principal_id=principal_id)
        await unit.audit.append(
            NewAuditEntry(
                actor=BOOTSTRAP_ACTOR,
                action=PRINCIPAL_ACTION,
                target=principal_id,
                details={"type": PrincipalType.SERVICE.value, "display_name": display_name},
                at=now,
            )
        )
    return True
