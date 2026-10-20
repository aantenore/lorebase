"""FastAPI dependencies for authentication: `PrincipalDep` is the authenticated caller."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPBearer

from lorebase_keeper.auth.principals import Principal
from lorebase_keeper.auth.runtime import AuthRuntime

_PRINCIPAL_KEY = "principal"
_bearer_scheme = HTTPBearer(auto_error=False, description="API key (lbk_...) or OIDC access token.")


def get_auth(request: Request) -> AuthRuntime:
    """The application's auth components."""
    return cast(AuthRuntime, request.app.state.auth)


AuthDep = Annotated[AuthRuntime, Depends(get_auth)]


async def current_principal(
    request: Request,
    auth: AuthDep,
    _credentials: Annotated[object, Depends(_bearer_scheme)] = None,
) -> Principal:
    """Authenticate the request (once) or answer 401."""
    state = request.scope.setdefault("state", {})
    principal = state.get(_PRINCIPAL_KEY)
    if principal is None:
        principal = await auth.authenticator.authenticate(request.headers)
        state[_PRINCIPAL_KEY] = principal
    return cast(Principal, principal)


PrincipalDep = Annotated[Principal, Depends(current_principal)]
