"""FastAPI dependencies for authentication and authorization.

Routes depend on `require(permission)` (a Domain permission, the Domain taken from the path) or
`require_installation(permission)`; both authenticate first and return what they decided on:

    @router.get("/domains/{domain_id}/grants")
    async def list_grants(access: Annotated[Access, Depends(require(Permission.GRANT_MANAGE))]): ...

Kind, Operation and branch scoped grants are evaluated when the route names the path parameter
that carries them (`kind_param`, `operation_param`, `branch_param`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPBearer

from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.rbac import NO_CONTEXT, AccessContext, Authorizer
from lorebase_keeper.auth.runtime import AuthRuntime
from lorebase_keeper.client_address import client_address
from lorebase_keeper.errors import FORBIDDEN, NOT_FOUND, UNAVAILABLE, ProblemError

_PRINCIPAL_KEY = "principal"
_bearer_scheme = HTTPBearer(auto_error=False, description="API key (lbk_...) or OIDC access token.")


def get_auth(request: Request) -> AuthRuntime:
    """The application's auth components."""
    return cast(AuthRuntime, request.app.state.auth)


AuthDep = Annotated[AuthRuntime, Depends(get_auth)]


def require_store(auth: AuthRuntime) -> None:
    """Raise 503 when no database is configured (administration needs one)."""
    if auth.store is None:
        raise ProblemError(UNAVAILABLE, "This operation needs a configured database.")


async def current_principal(
    request: Request,
    auth: AuthDep,
    _credentials: Annotated[object, Depends(_bearer_scheme)] = None,
) -> Principal:
    """Authenticate the request (once) or answer 401."""
    state = request.scope.setdefault("state", {})
    principal = state.get(_PRINCIPAL_KEY)
    if principal is None:
        principal = await auth.authenticator.authenticate(
            request.headers, client_address(request), request.method
        )
        state[_PRINCIPAL_KEY] = principal
    annotate(request, actor=principal.id, principal_type=principal.type.value)
    return cast(Principal, principal)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


@dataclass(frozen=True)
class Access:
    """An authorized request: who, in which Domain (none for installation-level), what for."""

    principal: Principal
    domain_id: str | None
    permission: Permission


def require(
    permission: Permission,
    *,
    domain_param: str = "domain_id",
    kind_param: str | None = None,
    operation_param: str | None = None,
    branch_param: str | None = None,
) -> Callable[..., Awaitable[Access]]:
    """Dependency: the caller holds `permission` in the Domain named by the path.

    Answers 401 without a valid credential and 403, naming the missing permission, otherwise.
    """

    async def dependency(request: Request, principal: PrincipalDep, auth: AuthDep) -> Access:
        params = request.path_params
        context = AccessContext(
            kind=_param(params, kind_param),
            operation=_param(params, operation_param),
            branch=_param(params, branch_param),
        )
        return await authorize(
            request, auth, principal, permission, str(params[domain_param]), context
        )

    return dependency


async def authorize(
    request: Request,
    auth: AuthRuntime,
    principal: Principal,
    permission: Permission,
    domain_id: str,
    context: AccessContext = NO_CONTEXT,
) -> Access:
    """Check `permission` in a Domain for a context known only inside the route (a branch or
    Operation named in the body). Answers 403 naming the missing permission."""
    annotate(request, domain_id=domain_id, permission=permission.value)
    if not await auth.authorizer.allows(principal, permission, domain_id, context):
        raise await refusal(auth.authorizer, principal, permission, domain_id)
    return Access(principal=principal, domain_id=domain_id, permission=permission)


async def refusal(
    authorizer: Authorizer, principal: Principal, permission: Permission, domain_id: str
) -> ProblemError:
    """The problem for a caller who lacks `permission` in a Domain: 404 when the Domain does not
    exist and the caller may be told so (see `Authorizer.reveals_missing_domain`), else 403."""
    if await authorizer.reveals_missing_domain(principal, domain_id):
        return ProblemError(NOT_FOUND, f"Domain {domain_id!r} not found.")
    return missing_permission(permission, domain_id)


def missing_permission(permission: Permission, domain_id: str) -> ProblemError:
    """The 403 problem naming the permission a caller lacks in a Domain."""
    return ProblemError(
        FORBIDDEN,
        f"Missing permission {permission.value} in Domain {domain_id}.",
        extensions={"permission": permission.value, "domain": domain_id},
    )


def require_installation(permission: Permission) -> Callable[..., Awaitable[Access]]:
    """Dependency: the caller holds an installation-level `permission`."""

    async def dependency(request: Request, principal: PrincipalDep, auth: AuthDep) -> Access:
        annotate(request, permission=permission.value)
        if not await auth.authorizer.allows_installation(principal, permission):
            raise ProblemError(
                FORBIDDEN,
                f"Missing permission {permission.value}.",
                extensions={"permission": permission.value},
            )
        return Access(principal=principal, domain_id=None, permission=permission)

    return dependency


def _param(params: dict[str, object], name: str | None) -> str | None:
    value = params.get(name) if name else None
    return None if value is None else str(value)
