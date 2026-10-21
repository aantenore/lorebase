"""Domain roles, scoped grants, the permission dependency and the grants cache."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from lorebase_keeper.auth.config import AuthzSettings
from lorebase_keeper.auth.deps import Access, require, require_installation
from lorebase_keeper.auth.permissions import BUILTIN_ROLES, Permission, role_catalog
from lorebase_keeper.auth.principals import Principal, anonymous_principal
from lorebase_keeper.auth.rbac import AccessContext, Authorizer
from lorebase_store.models.enums import GrantScope, PrincipalType

P = Permission
ALL = frozenset(Permission)
READ = {P.SCHEMA_READ, P.DATA_READ}
DOCUMENTS = {P.DOCUMENT_READ, P.DOCUMENT_WRITE}

# The matrix of design 10.2, written out independently of the implementation.
EXPECTED: dict[str, set[Permission]] = {
    "viewer": READ,
    "explorer": READ | {P.VIEWS_WRITE},
    "editor": READ
    | {P.BRANCH_CREATE, P.OPERATION_EXECUTE, P.PROPOSAL_CREATE, P.SOURCE_SYNC}
    | DOCUMENTS,
    "operator": READ | {P.OPERATION_EXECUTE_CUSTOM},
    "modeler": READ | {P.SCHEMA_WRITE, P.SOURCE_MAP, P.PROPOSAL_CREATE},
    "reviewer": READ | {P.PROPOSAL_REVIEW, P.CONFLICT_RESOLVE, P.DOCUMENT_READ},
    "maintainer": READ
    | {P.BRANCH_CREATE, P.OPERATION_EXECUTE, P.PROPOSAL_CREATE, P.SOURCE_SYNC}
    | DOCUMENTS
    | {P.PROPOSAL_REVIEW, P.CONFLICT_RESOLVE}
    | {P.PROPOSAL_MERGE, P.SOURCE_MANAGE, P.JOB_MANAGE},
    "admin": set(ALL),
    "agent-reader": READ,
}

ClientFor = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]

ISSUER = "https://idp.example.test"
AUDIENCE = "lorebase-api"


def user(identifier: str = "usr_u", *subjects: str, **overrides: Any) -> Principal:
    return Principal(
        id=identifier,
        type=PrincipalType.USER,
        display_name=identifier,
        method="oidc",
        subjects=(identifier, *subjects),
        **overrides,
    )


def authorizer(store: Any, **settings: Any) -> Authorizer:
    return Authorizer(AuthzSettings(**settings), store=store)


def test_the_expected_matrix_covers_every_builtin_role() -> None:
    assert set(EXPECTED) == set(BUILTIN_ROLES)


@pytest.mark.parametrize("role", sorted(EXPECTED))
@pytest.mark.parametrize("permission", sorted(Permission))
async def test_permission_matrix(role: str, permission: Permission, store: Any) -> None:
    store.add_grant("acme", "usr_u", role)
    allowed = await authorizer(store).allows(user(), permission, "acme")
    assert allowed == (permission in EXPECTED[role]), f"{role} / {permission.value}"


async def test_deny_by_default(store: Any) -> None:
    assert not await authorizer(store).allows(user(), P.DATA_READ, "acme")


async def test_roles_of_lists_the_roles_whose_grants_cover_the_context(store: Any) -> None:
    store.add_grant("acme", "usr_u", "viewer")
    store.add_grant("acme", "grp_review", "reviewer")
    store.add_grant("acme", "usr_u", "editor", GrantScope.BRANCH, "agent/*")
    store.add_grant("acme", "usr_u", "operator", GrantScope.OPERATION, "flag_supplier_risk")
    store.add_grant("globex", "usr_u", "admin")
    subject = authorizer(store)
    caller = user("usr_u", "grp_review")
    assert await subject.roles_of(caller, "acme") == {"viewer", "reviewer"}
    task_branch = AccessContext(branch="agent/usr_u/audit")
    assert await subject.roles_of(caller, "acme", task_branch) == {"viewer", "reviewer", "editor"}
    assert await subject.roles_of(caller, "none-such") == frozenset()


async def test_roles_of_respects_key_scopes_and_authentication_off(store: Any) -> None:
    store.add_grant("acme", "usr_u", "editor")
    subject = authorizer(store)
    assert await subject.roles_of(user(key_scopes=("domain:other",)), "acme") == frozenset()
    assert await subject.roles_of(user(key_scopes=("domain:acme",)), "acme") == {"editor"}
    assert await subject.roles_of(anonymous_principal(), "acme") == subject.role_names


async def test_grants_are_per_domain(store: Any) -> None:
    store.add_grant("acme", "usr_u", "admin")
    subject = authorizer(store)
    assert await subject.allows(user(), P.GRANT_MANAGE, "acme")
    assert not await subject.allows(user(), P.GRANT_MANAGE, "globex")


async def test_roles_combine_across_grants_and_groups(store: Any) -> None:
    store.add_grant("acme", "usr_u", "viewer")
    store.add_grant("acme", "grp_review", "reviewer")
    caller = user("usr_u", "grp_review")
    subject = authorizer(store)
    assert await subject.allows(caller, P.DATA_READ, "acme")
    assert await subject.allows(caller, P.PROPOSAL_REVIEW, "acme")
    assert not await subject.allows(caller, P.PROPOSAL_MERGE, "acme")


async def test_kind_scoped_grant_covers_only_that_kind(store: Any) -> None:
    store.add_grant("acme", "usr_u", "editor", GrantScope.KIND, "Supplier")
    subject = authorizer(store)
    assert await subject.allows(user(), P.BRANCH_CREATE, "acme", AccessContext(kind="Supplier"))
    assert not await subject.allows(user(), P.BRANCH_CREATE, "acme", AccessContext(kind="Part"))
    assert not await subject.allows(user(), P.BRANCH_CREATE, "acme")


async def test_operation_scoped_grant_covers_only_that_operation(store: Any) -> None:
    store.add_grant("acme", "usr_u", "operator", GrantScope.OPERATION, "flag_supplier_risk")
    subject = authorizer(store)
    permission = P.OPERATION_EXECUTE_CUSTOM
    assert await subject.allows(
        user(), permission, "acme", AccessContext(operation="flag_supplier_risk")
    )
    assert not await subject.allows(user(), permission, "acme", AccessContext(operation="other"))
    assert not await subject.allows(user(), P.DATA_READ, "acme")


async def test_branch_pattern_grant(store: Any) -> None:
    store.add_grant("acme", "usr_u", "editor", GrantScope.BRANCH, "agent/*")
    subject = authorizer(store)
    permission = P.OPERATION_EXECUTE
    assert await subject.allows(user(), permission, "acme", AccessContext(branch="agent/run-7"))
    assert not await subject.allows(user(), permission, "acme", AccessContext(branch="main"))
    assert not await subject.allows(user(), permission, "acme", AccessContext(branch="xagent/a"))
    assert not await subject.allows(user(), permission, "acme")


async def test_api_key_scopes_limit_the_domains(store: Any) -> None:
    store.add_grant("acme", "usr_u", "admin")
    store.add_grant("globex", "usr_u", "admin")
    scoped = user(key_scopes=("domain:acme",))
    subject = authorizer(store)
    assert await subject.allows(scoped, P.GRANT_MANAGE, "acme")
    assert not await subject.allows(scoped, P.GRANT_MANAGE, "globex")


async def test_authentication_disabled_allows_everything(store: Any) -> None:
    subject = authorizer(store)
    assert await subject.allows(anonymous_principal(), P.GRANT_MANAGE, "anything")
    assert await subject.allows_installation(anonymous_principal(), P.PRINCIPAL_READ)
    assert store.grant_lookups == 0


async def test_installation_permission_rules(store: Any) -> None:
    store.add_grant("acme", "usr_admin", "admin")
    store.add_grant("acme", "usr_viewer", "viewer")
    store.add_grant("acme", "usr_scoped", "editor", GrantScope.KIND, "Part")
    subject = authorizer(store)
    assert await subject.allows_installation(user("usr_admin"), P.PRINCIPAL_READ)
    assert not await subject.allows_installation(user("usr_viewer"), P.PRINCIPAL_READ)
    assert not await subject.allows_installation(user("usr_nobody"), P.PRINCIPAL_READ)
    ops = user("usr_nobody", installation_admin=True)
    assert await subject.allows_installation(ops, P.PRINCIPAL_READ)
    scoped_key = user("usr_admin", key_scopes=("domain:acme",))
    assert not await subject.allows_installation(scoped_key, P.PRINCIPAL_READ)
    with pytest.raises(ValueError, match="installation"):
        await subject.allows_installation(ops, P.DATA_READ)


async def test_scoped_key_of_an_installation_admin_gets_no_installation_access(
    store: Any,
) -> None:
    ops_key = user("usr_ops", installation_admin=True, key_scopes=("domain:acme",))
    assert not await authorizer(store).allows_installation(ops_key, P.PRINCIPAL_READ)


async def test_installation_admin_gets_no_domain_data_access(store: Any) -> None:
    ops = user("usr_ops", installation_admin=True)
    assert not await authorizer(store).allows(ops, P.DATA_READ, "acme")


async def test_custom_roles_extend_the_catalog(store: Any) -> None:
    store.add_grant("acme", "usr_u", "contributor")
    subject = authorizer(
        store, custom_roles={"contributor": ["schema.read", "data.read", "branch.create"]}
    )
    assert await subject.allows(user(), P.BRANCH_CREATE, "acme")
    assert not await subject.allows(user(), P.OPERATION_EXECUTE, "acme")
    assert "contributor" in subject.role_names
    assert set(BUILTIN_ROLES) <= subject.role_names


def test_custom_roles_are_validated() -> None:
    with pytest.raises(ValueError, match="built in"):
        AuthzSettings(custom_roles={"admin": ["data.read"]})
    with pytest.raises(ValueError, match="unknown permissions"):
        AuthzSettings(custom_roles={"odd": ["data.fly"]})
    with pytest.raises(ValueError, match="role name"):
        AuthzSettings(custom_roles={"Bad Name": ["data.read"]})
    with pytest.raises(ValueError, match="built in"):
        role_catalog({"viewer": []})


async def test_a_grant_for_a_removed_custom_role_gives_nothing(store: Any) -> None:
    store.add_grant("acme", "usr_u", "retired-role")
    assert not await authorizer(store).allows(user(), P.DATA_READ, "acme")


async def test_grants_are_cached_until_the_time_to_live_or_invalidation(store: Any) -> None:
    now = [100.0]
    subject = Authorizer(
        AuthzSettings(grants_cache_ttl_s=15), store=store, monotonic=lambda: now[0]
    )
    store.add_grant("acme", "usr_u", "viewer")
    assert await subject.allows(user(), P.DATA_READ, "acme")
    assert await subject.allows(user(), P.DATA_READ, "acme")
    assert store.grant_lookups == 1

    store.grant_rows.clear()
    assert await subject.allows(user(), P.DATA_READ, "acme")  # still cached
    now[0] += 16
    assert not await subject.allows(user(), P.DATA_READ, "acme")
    assert store.grant_lookups == 2

    store.add_grant("acme", "usr_u", "viewer")
    assert not await subject.allows(user(), P.DATA_READ, "acme")  # cached denial
    subject.invalidate()
    assert await subject.allows(user(), P.DATA_READ, "acme")


async def test_zero_ttl_disables_the_cache(store: Any) -> None:
    subject = authorizer(store, grants_cache_ttl_s=0)
    store.add_grant("acme", "usr_u", "viewer")
    await subject.allows(user(), P.DATA_READ, "acme")
    await subject.allows(user(), P.DATA_READ, "acme")
    assert store.grant_lookups == 2


# the dependencies, through HTTP -----------------------------------------------------------


@pytest.fixture
def routed(auth_app: Callable[..., FastAPI], store: Any, codec: Any) -> Callable[..., FastAPI]:
    """An application with one probe route per dependency form."""

    def build(**auth: Any) -> FastAPI:
        app = auth_app({"mode": "api_key", "api_keys": {"pepper": codec_pepper()}, **auth})
        router = APIRouter()

        @router.get("/api/v1/domains/{domain_id}/probe/data")
        async def data(access: Annotated[Access, Depends(require(P.DATA_READ))]) -> dict[str, str]:
            return {"domain": access.domain_id or "", "permission": access.permission.value}

        @router.get("/api/v1/domains/{domain_id}/kinds/{kind}/probe")
        async def kind(
            access: Annotated[Access, Depends(require(P.BRANCH_CREATE, kind_param="kind"))],
        ) -> dict[str, str]:
            return {"ok": "yes"}

        @router.get("/api/v1/installation/probe")
        async def installation(
            access: Annotated[Access, Depends(require_installation(P.PRINCIPAL_READ))],
        ) -> dict[str, str]:
            return {"ok": "yes"}

        app.include_router(router)
        return app

    return build


def codec_pepper() -> str:
    return "test-pepper-0123456789-0123456789-abcdef"


def auth_header(store: Any, codec: Any, principal: str, **key: Any) -> dict[str, str]:
    token, _ = store.add_key(codec, principal, **key)
    return {"Authorization": f"Bearer {token}"}


async def test_missing_permission_is_a_403_naming_it(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    headers = auth_header(store, codec, "svc_a")
    async with client_for(routed()) as http:
        response = await http.get("/api/v1/domains/acme/probe/data", headers=headers)
    body = response.json()
    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert body["code"] == "forbidden"
    assert body["permission"] == "data.read"
    assert body["domain"] == "acme"
    assert "data.read" in body["detail"]
    assert body["request_id"]


async def test_granted_permission_passes_and_reports_the_domain(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_a", "viewer")
    headers = auth_header(store, codec, "svc_a")
    async with client_for(routed()) as http:
        ok = await http.get("/api/v1/domains/acme/probe/data", headers=headers)
        other = await http.get("/api/v1/domains/globex/probe/data", headers=headers)
    assert ok.json() == {"domain": "acme", "permission": "data.read"}
    assert other.status_code == 403


async def test_kind_parameter_selects_kind_scoped_grants(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_a", "editor", GrantScope.KIND, "Supplier")
    headers = auth_header(store, codec, "svc_a")
    async with client_for(routed()) as http:
        allowed = await http.get("/api/v1/domains/acme/kinds/Supplier/probe", headers=headers)
        denied = await http.get("/api/v1/domains/acme/kinds/Part/probe", headers=headers)
    assert allowed.status_code == 200
    assert denied.status_code == 403


async def test_installation_dependency(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    store.add_principal("svc_b", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_b", "admin")
    first = auth_header(store, codec, "svc_a")
    second = auth_header(store, codec, "svc_b")
    async with client_for(routed()) as http:
        denied = await http.get("/api/v1/installation/probe", headers=first)
        allowed = await http.get("/api/v1/installation/probe", headers=second)
    assert denied.status_code == 403
    assert denied.json()["permission"] == "principal.read"
    assert allowed.status_code == 200


async def test_installation_admins_come_from_settings(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_ops", PrincipalType.SERVICE)
    headers = auth_header(store, codec, "svc_ops")
    async with client_for(routed(installation_admins=["svc_ops"])) as http:
        assert (await http.get("/api/v1/installation/probe", headers=headers)).status_code == 200
        data = await http.get("/api/v1/domains/acme/probe/data", headers=headers)
    assert data.status_code == 403


async def test_scoped_key_of_an_installation_admin_stays_within_its_scope(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_ops", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_ops", "viewer")
    headers = auth_header(store, codec, "svc_ops", scopes=("domain:acme",))
    async with client_for(routed(installation_admins=["svc_ops"])) as http:
        installation = await http.get("/api/v1/installation/probe", headers=headers)
        inside = await http.get("/api/v1/domains/acme/probe/data", headers=headers)
    assert installation.status_code == 403
    assert installation.json()["permission"] == "principal.read"
    assert inside.status_code == 200


async def test_custom_roles_work_through_the_settings(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_a", "contributor")
    headers = auth_header(store, codec, "svc_a")
    app = routed(authz={"custom_roles": {"contributor": ["data.read"]}})
    async with client_for(app) as http:
        assert (
            await http.get("/api/v1/domains/acme/probe/data", headers=headers)
        ).status_code == 200


def test_every_permission_is_held_by_some_role() -> None:
    held = set().union(*BUILTIN_ROLES.values())
    assert held == set(Permission)


async def test_key_scoped_to_another_domain_is_forbidden_here(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_a", "admin")
    store.add_grant("globex", "svc_a", "admin")
    headers = auth_header(store, codec, "svc_a", scopes=["domain:globex"])
    async with client_for(routed()) as http:
        denied = await http.get("/api/v1/domains/acme/probe/data", headers=headers)
        allowed = await http.get("/api/v1/domains/globex/probe/data", headers=headers)
        installation = await http.get("/api/v1/installation/probe", headers=headers)
    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert installation.status_code == 403  # a Domain-limited key is never an installation admin


async def test_provider_groups_grant_access_through_group_grants(
    auth_app: Callable[..., FastAPI], client_for: ClientFor, provider: Any, store: Any
) -> None:
    store.group_rows["grp_admins"] = "idp-admins"
    store.add_grant("acme", "grp_admins", "admin")
    app = auth_app({"mode": "oidc", "oidc": {"issuer": ISSUER, "audience": AUDIENCE}})
    url = "/api/v1/domains/acme/grants"
    member = {"Authorization": "Bearer " + provider.token(sub="m", groups=["idp-admins"])}
    outsider = {"Authorization": "Bearer " + provider.token(sub="o", groups=["other"])}
    async with client_for(app) as http:
        assert (await http.get(url, headers=member)).status_code == 200
        assert (await http.get(url, headers=outsider)).status_code == 403


async def test_granted_kinds_lists_kind_narrowed_grants_only(store: Any) -> None:
    store.add_grant("acme", "usr_u", "viewer", GrantScope.KIND, "Supplier")
    store.add_grant("acme", "usr_u", "viewer", GrantScope.BRANCH, "feature/*")
    store.add_grant("acme", "usr_u", "editor", GrantScope.KIND, "Part")
    subject = authorizer(store)
    assert await subject.granted_kinds(user(), P.DATA_READ, "acme") == {"Supplier", "Part"}
    assert await subject.granted_kinds(user(), P.DATA_READ, "globex") == frozenset()
    assert await subject.granted_kinds(user(), P.GRANT_MANAGE, "acme") == frozenset()


# a missing Domain: 404 only for callers who could see it --------------------------------------


async def test_missing_domain_is_revealed_to_admins_and_domain_wide_holders_only(
    store: Any,
) -> None:
    store.add_grant("ghost", "usr_wide", "viewer")
    store.add_grant("ghost", "usr_kind", "editor", GrantScope.KIND, "Part")
    store.add_grant("ghost", "usr_branch", "editor", GrantScope.BRANCH, "agent/*")
    store.add_grant("acme", "usr_wide", "viewer")
    subject = authorizer(store)
    ops = user("usr_ops", installation_admin=True)

    assert await subject.reveals_missing_domain(ops, "ghost")
    assert await subject.reveals_missing_domain(user("usr_wide"), "ghost")
    assert not await subject.reveals_missing_domain(user("usr_kind"), "ghost")
    assert not await subject.reveals_missing_domain(user("usr_branch"), "ghost")
    assert not await subject.reveals_missing_domain(user("usr_nobody"), "ghost")
    # an existing Domain is never reported missing, whoever asks
    assert not await subject.reveals_missing_domain(ops, "acme")
    assert not await subject.reveals_missing_domain(user("usr_wide"), "acme")


async def test_key_limited_to_other_domains_does_not_reveal_a_missing_domain(store: Any) -> None:
    ops_key = user("usr_ops", installation_admin=True, key_scopes=("domain:acme",))
    subject = authorizer(store)
    assert not await subject.reveals_missing_domain(ops_key, "ghost")
    inside = user("usr_ops", installation_admin=True, key_scopes=("domain:ghost",))
    assert await subject.reveals_missing_domain(inside, "ghost")


async def test_installation_admin_gets_404_for_a_missing_domain_and_403_for_an_existing_one(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_ops", PrincipalType.SERVICE)
    headers = auth_header(store, codec, "svc_ops")
    async with client_for(routed(installation_admins=["svc_ops"])) as http:
        missing = await http.get("/api/v1/domains/ghost/probe/data", headers=headers)
        existing = await http.get("/api/v1/domains/acme/probe/data", headers=headers)
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert missing.json()["code"] == "not-found"
    assert existing.status_code == 403


async def test_domain_wide_grant_gets_404_for_a_missing_domain(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_wide", PrincipalType.SERVICE)
    store.add_grant("ghost", "svc_wide", "viewer")  # lacks branch.create there
    headers = auth_header(store, codec, "svc_wide")
    async with client_for(routed()) as http:
        missing = await http.get("/api/v1/domains/ghost/kinds/Part/probe", headers=headers)
    assert missing.status_code == 404


async def test_narrowed_or_ungranted_callers_get_403_for_a_missing_domain(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_none", PrincipalType.SERVICE)
    store.add_principal("svc_kind", PrincipalType.SERVICE)
    store.add_principal("svc_ops_key", PrincipalType.SERVICE)
    store.add_grant("ghost", "svc_kind", "editor", GrantScope.KIND, "Part")
    nobody = auth_header(store, codec, "svc_none")
    narrowed = auth_header(store, codec, "svc_kind")
    other_scope = auth_header(store, codec, "svc_ops_key", scopes=("domain:acme",))
    app = routed(installation_admins=["svc_ops_key"])
    async with client_for(app) as http:
        for headers in (nobody, narrowed, other_scope):
            response = await http.get("/api/v1/domains/ghost/probe/data", headers=headers)
            assert response.status_code == 403
            assert response.json()["permission"] == "data.read"
            assert response.json()["domain"] == "ghost"
        # the same answer as for a Domain that exists and is not theirs
        existing = await http.get("/api/v1/domains/acme/probe/data", headers=narrowed)
        assert existing.status_code == 403


async def test_a_wildcard_domain_id_is_not_every_domain(store: Any) -> None:
    # "*" is how the authorizer asks for grants in any Domain (installation permissions);
    # a Domain id taken from a request path must never select that lookup.
    store.add_grant("acme", "usr_u", "admin")
    store.add_grant("globex", "usr_u", "editor", GrantScope.KIND, "Supplier")
    subject = authorizer(store)
    assert not await subject.allows(user(), P.GRANT_MANAGE, "*")
    assert not await subject.allows_anywhere(user(), P.GRANT_MANAGE, "*")
    assert await subject.granted_kinds(user(), P.BRANCH_CREATE, "*") == frozenset()
    assert await subject.allows(user(), P.GRANT_MANAGE, "acme")
    assert await subject.allows_installation(user(), P.DOMAIN_CREATE)


async def test_a_wildcard_domain_in_the_path_is_forbidden(
    routed: Callable[..., FastAPI], client_for: ClientFor, store: Any, codec: Any
) -> None:
    store.add_principal("svc_a", PrincipalType.SERVICE)
    store.add_grant("acme", "svc_a", "viewer")
    headers = auth_header(store, codec, "svc_a")
    async with client_for(routed()) as http:
        response = await http.get("/api/v1/domains/*/probe/data", headers=headers)
    assert response.status_code == 403
